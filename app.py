#!/usr/bin/env python3
"""
app.py — FastAPI backend for gdsync UI.

Configure the base directory via the BASE_DIR environment variable:
    BASE_DIR=/path/to/my/sessions uvicorn app:app --reload

If BASE_DIR is not set, it defaults to the directory containing this file.
"""

from __future__ import annotations

import os
import shutil
import uuid
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# ---- Augment PATH so uvicorn can find ffmpeg on macOS regardless of how it was launched ----
_EXTRA_PATHS = [
    "/opt/homebrew/bin",          # Homebrew Apple Silicon
    "/usr/local/bin",             # Homebrew Intel / manual installs
    "/opt/local/bin",             # MacPorts
    str(Path.home() / "bin"),     # ~/bin
    "/usr/bin",
]
os.environ["PATH"] = ":".join(_EXTRA_PATHS) + ":" + os.environ.get("PATH", "")

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import gdsync_core as core

# ---------- configuration ----------

BASE_DIR = Path(os.environ.get("BASE_DIR", Path(__file__).parent)).resolve()

# ---------- app ----------

app = FastAPI(title="MissMatch", description="GoPro & Drone footage sync service", version="1.0.0")

# ---------- in-memory job store ----------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "logs": [], "outputs": {}}
    return job_id


def _append_log(job_id: str, msg: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["logs"].append(msg)


def _set_status(job_id: str, status: str, **extra) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = status
        _jobs[job_id].update(extra)


# ---------- API routes ----------

@app.get("/api/health")
def health():
    """Quick diagnostic: confirms ffmpeg/ffprobe are reachable."""
    return {
        "base_dir": str(BASE_DIR),
        "ffmpeg":   shutil.which("ffmpeg"),
        "ffprobe":  shutil.which("ffprobe"),
        "path":     os.environ.get("PATH", ""),
    }


@app.get("/api/sessions")
def list_sessions():
    """List all valid session subfolders under BASE_DIR."""
    sessions = []
    try:
        for entry in sorted(BASE_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            # Must contain gopro + at least one drone file
            has_gopro = any(
                f.stem.lower() == "gopro" and f.suffix.lower() in (".mp4", ".mov")
                for f in entry.iterdir() if f.is_file()
            )
            has_drone = any(
                f.stem.lower().startswith("drone_") and f.suffix.lower() in (".mp4", ".mov")
                for f in entry.iterdir() if f.is_file()
            )
            if has_gopro and has_drone:
                sessions.append(entry.name)
    except PermissionError:
        raise HTTPException(status_code=500, detail="Cannot read BASE_DIR")
    return {"base_dir": str(BASE_DIR), "sessions": sessions}


@app.get("/api/session/{name}")
def get_session(name: str):
    """Return metadata for a session: GoPro duration, drone files, existing syncs."""
    session = BASE_DIR / name
    if not session.is_dir():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        gopro, drone_files = core.discover_session(session)
    except core.GDSyncError as e:
        raise HTTPException(status_code=422, detail=str(e))

    gopro_duration = core.probe_duration(gopro)

    drone_info = []
    for df in drone_files:
        dur = core.probe_duration(df)
        drone_info.append({"name": df.name, "duration": dur, "duration_fmt": core.fmt_time(dur)})

    existing_syncs = {s.drone_file: asdict(s) for s in core.load_syncs(session)}

    outputs = {}
    out_dir = session / "output"
    for key, filename in [("gopro", "gopro.mp4"), ("drone", "drone.mp4")]:
        p = out_dir / filename
        if p.exists():
            outputs[key] = filename

    return {
        "name": name,
        "gopro": {"name": gopro.name, "duration": gopro_duration, "duration_fmt": core.fmt_time(gopro_duration)},
        "drone_files": drone_info,
        "existing_syncs": existing_syncs,
        "outputs": outputs,
    }


# ---------- job submission ----------

class SyncPoint(BaseModel):
    drone_file: str
    gopro_marker: float   # seconds
    drone_marker: float   # seconds


class SyncRequest(BaseModel):
    session: str
    syncs: list[SyncPoint]
    height: int = 720
    resync: bool = False


@app.post("/api/sync")
def submit_sync(req: SyncRequest):
    """Save sync points and start the background encoding job."""
    session = BASE_DIR / req.session
    if not session.is_dir():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        gopro, drone_files = core.discover_session(session)
    except core.GDSyncError as e:
        raise HTTPException(status_code=422, detail=str(e))

    drone_by_name = {p.name: p for p in drone_files}

    # Build FlightSync objects, probing durations
    flight_syncs: list[core.FlightSync] = []
    for sp in req.syncs:
        if sp.drone_file not in drone_by_name:
            raise HTTPException(status_code=422, detail=f"Unknown drone file: {sp.drone_file}")
        dur = core.probe_duration(drone_by_name[sp.drone_file])
        flight_syncs.append(core.FlightSync(
            drone_file=sp.drone_file,
            drone_duration=dur,
            gopro_marker=sp.gopro_marker,
            drone_marker=sp.drone_marker,
        ))

    core.save_syncs(session, flight_syncs)

    job_id = _new_job()

    def _run():
        _set_status(job_id, "running")
        try:
            gopro_out, drone_out = core.build_aligned_drone(
                session, gopro, drone_files, flight_syncs,
                height=req.height,
                log=lambda msg: _append_log(job_id, msg),
            )
            _set_status(job_id, "done", outputs={
                "gopro": f"/api/output/{req.session}/gopro.mp4",
                "drone": f"/api/output/{req.session}/drone.mp4",
            })
        except core.GDSyncError as e:
            _set_status(job_id, "error", error=str(e))
        except Exception as e:
            _set_status(job_id, "error", error=f"Unexpected error: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    """Poll the status of an encoding job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/output/{session}/{filename}")
def download_output(session: str, filename: str):
    """Serve an output file for download."""
    if filename not in ("gopro.mp4", "drone.mp4"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = BASE_DIR / session / "output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)


# ---------- static files (UI) ----------

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
