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

import json
import logging
import secrets as _secrets
from datetime import datetime, timedelta

import httpx
import jwt as _jwt
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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


# ── Processor jobs (Gustav contract) ────────────────────────────────────────

_processor_jobs: dict[str, dict] = {}
_processor_jobs_lock = threading.Lock()

# Gandalf sessions
_sessions: dict[str, datetime] = {}

logger = logging.getLogger("missmatch-ms")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

PROCESSOR_SECRET: str = os.environ.get("PROCESSOR_SECRET", "")
UPLOAD_DIR: str = os.environ.get("UPLOAD_DIR", "/app/uploads")
MISSMATCH_MS_URL: str = os.environ.get("MISSMATCH_MS_URL", "http://missmatch-ms:8003")
GANDALF_URL: str = os.environ.get("GANDALF_URL", "https://pass.strikeapp.io").rstrip("/")
GANDALF_JWT_SECRET: str = os.environ.get("GANDALF_JWT_SECRET", "")
SECURE_COOKIES: bool = os.environ.get("SECURE_COOKIES", "false").lower() in {"1", "true", "yes"}
SESSION_TTL_DAYS: int = int(os.environ.get("SESSION_TTL_DAYS", "30"))


def _valid_session(session_token: Optional[str]) -> bool:
    return (
        bool(session_token)
        and session_token in _sessions
        and _sessions[session_token] >= datetime.now()
    )


def _require_session(session_token: Optional[str] = Cookie(None)):
    if GANDALF_JWT_SECRET and not _valid_session(session_token):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _verify_processor_secret(authorization: Optional[str]) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):]
    if not PROCESSOR_SECRET:
        return True  # no secret configured → open
    return _secrets.compare_digest(token, PROCESSOR_SECRET)


def _resolve_input(url: str, job_id: str) -> Path:
    """Resolve internal:// to shared volume path. HTTP not supported — use Gustav uploads."""
    if url.startswith("internal://"):
        path_part = url[len("internal://"):]
        resolved = Path(UPLOAD_DIR) / path_part
        if not resolved.exists():
            raise FileNotFoundError(f"internal:// not found: {resolved}")
        return resolved
    raise ValueError(f"Unsupported URL scheme (only internal:// supported): {url!r}")


def _notify_callback(
    callback_url: str,
    job_id: str,
    stage: str,
    status: str,
    outputs: list,
    error: str | None = None,
) -> None:
    """Synchronously POST callback to Gustav (runs in background thread)."""
    payload = {
        "job_id": job_id,
        "stage": stage,
        "status": status,
        "processor": "missmatch-ms",
        "outputs": outputs,
        "error": (
            {"code": "processing_failed", "message": error, "retryable": True}
            if error else None
        ),
        "completed_at": datetime.utcnow().isoformat(),
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            client.post(
                callback_url,
                json=payload,
                headers={"Authorization": f"Bearer {PROCESSOR_SECRET}"},
            )
        logger.info("[%s] Notified Gustav: %s", job_id, status)
    except Exception as exc:
        logger.warning("[%s] Callback failed: %s", job_id, exc)


def _run_processor_job(
    job_id: str,
    gopro_urls: list[str],        # one or more GoPro chapter URLs, sorted
    drone_urls: list[str],
    drone_filenames: list[str],
    syncs: list[dict],
    callback_url: str,
    stage: str,
    height: int = 720,
) -> None:
    """Background thread: resolve files, concat chapters, align, build splitscreen, callback."""
    job_dir = Path(f"/tmp/missmatch-ms/{job_id}")
    job_dir.mkdir(parents=True, exist_ok=True)

    def _upd(**kw):
        with _processor_jobs_lock:
            _processor_jobs[job_id].update(kw)

    _upd(status="processing", step="resolving")
    try:
        # 1. Resolve GoPro path(s) — concatenate chapters if more than one
        gopro_resolved = [_resolve_input(url, job_id) for url in gopro_urls]
        if len(gopro_resolved) > 1:
            _upd(step="concat_gopro")
            logger.info("[%s] Concatenating %d GoPro chapter(s)", job_id, len(gopro_resolved))
            gopro_path = core.concat_videos(
                gopro_resolved,
                job_dir / "gopro_full.mp4",
                log=lambda msg: logger.info("[%s] %s", job_id, msg),
            )
        else:
            gopro_path = gopro_resolved[0]

        # 2. Resolve drone paths
        drone_paths: list[Path] = []
        for drone_url, fname in zip(drone_urls, drone_filenames):
            dp = _resolve_input(drone_url, job_id)
            drone_paths.append(dp)

        # 3. Build FlightSync objects
        flight_syncs: list[core.FlightSync] = []
        drone_by_name = {p.name: p for p in drone_paths}
        for s in syncs:
            df_name = s.get("drone_file", "")
            # Match by exact name or fallback to first drone
            dp = drone_by_name.get(df_name) or (drone_paths[0] if drone_paths else None)
            if dp is None:
                raise ValueError(f"Drone file not found: {df_name}")
            dur = core.probe_duration(dp)
            flight_syncs.append(core.FlightSync(
                drone_file=dp.name,
                drone_duration=dur,
                gopro_marker=float(s.get("gopro_marker", 0)),
                drone_marker=float(s.get("drone_marker", 0)),
            ))

        if not flight_syncs and drone_paths:
            # No syncs provided — zero-offset for each drone
            for dp in drone_paths:
                dur = core.probe_duration(dp)
                flight_syncs.append(core.FlightSync(
                    drone_file=dp.name,
                    drone_duration=dur,
                    gopro_marker=0.0,
                    drone_marker=0.0,
                ))

        _upd(step="aligning")
        # 4. Align drone to GoPro timeline
        gopro_out, drone_out = core.build_aligned_drone(
            session=job_dir,
            gopro=gopro_path,
            drone_files=drone_paths,
            syncs=flight_syncs,
            height=height,
            log=lambda msg: logger.info("[%s] %s", job_id, msg),
        )

        _upd(step="compositing")
        # 5. Build split-screen composite
        splitscreen_out = job_dir / "output" / "splitscreen.mp4"
        core.build_splitscreen(
            gopro=gopro_out,
            drone=drone_out,
            dst=splitscreen_out,
            height=height,
            log=lambda msg: logger.info("[%s] %s", job_id, msg),
        )

        # 6. Prepare outputs
        outputs = [
            {
                "type": "video/mp4;profile=split_screen",
                "url": f"{MISSMATCH_MS_URL}/jobs/{job_id}/outputs/splitscreen.mp4",
                "label": "gopro_splitscreen",
            },
            {
                "type": "video/mp4",
                "url": f"{MISSMATCH_MS_URL}/jobs/{job_id}/outputs/gopro.mp4",
                "label": "gopro_full",
            },
        ]

        _upd(status="done", step="complete", outputs=outputs)
        _notify_callback(callback_url, job_id, stage, "done", outputs)

    except Exception as exc:
        logger.exception("[%s] Job failed", job_id)
        _upd(status="failed", error=str(exc))
        _notify_callback(callback_url, job_id, stage, "failed", [], error=str(exc))


# ---------- UI root — Gandalf-gated ----------

@app.get("/")
def index(request: Request, session_token: Optional[str] = Cookie(None)):
    if GANDALF_JWT_SECRET and not _valid_session(session_token):
        scheme = "https" if SECURE_COOKIES else "http"
        host = request.headers.get("host", "localhost")
        from urllib.parse import quote
        callback = quote(f"{scheme}://{host}/auth/callback")
        return RedirectResponse(url=f"{GANDALF_URL}?redirect_uri={callback}", status_code=302)
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


# ---------- API routes ----------

@app.get("/api/health", dependencies=[Depends(_require_session)])
def health():
    """Quick diagnostic: confirms ffmpeg/ffprobe are reachable."""
    return {
        "base_dir": str(BASE_DIR),
        "ffmpeg":   shutil.which("ffmpeg"),
        "ffprobe":  shutil.which("ffprobe"),
        "path":     os.environ.get("PATH", ""),
    }


@app.get("/api/sessions", dependencies=[Depends(_require_session)])
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


@app.get("/api/session/{name}", dependencies=[Depends(_require_session)])
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


@app.post("/api/sync", dependencies=[Depends(_require_session)])
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


@app.get("/api/job/{job_id}", dependencies=[Depends(_require_session)])
def get_job(job_id: str):
    """Poll the status of an encoding job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/output/{session}/{filename}", dependencies=[Depends(_require_session)])
def download_output(session: str, filename: str):
    """Serve an output file for download."""
    if filename not in ("gopro.mp4", "drone.mp4"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = BASE_DIR / session / "output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)


# ── Gustav processor contract ─────────────────────────────────────────────────

class _ProcessorInput(BaseModel):
    type: str
    url: str
    label: Optional[str] = ""


class _ProcessorJobRequest(BaseModel):
    job_id: str
    stage: str = "missmatch"
    inputs: list[_ProcessorInput]
    config: dict = {}
    callback_url: str


@app.get("/health")
def processor_health():
    return {
        "status": "ok",
        "service": "missmatch-ms",
        "version": "1.0.0",
        "ffmpeg": shutil.which("ffmpeg"),
    }


@app.post("/jobs", status_code=202)
def create_processor_job(
    body: _ProcessorJobRequest,
    authorization: Optional[str] = Header(default=None),
):
    if not _verify_processor_secret(authorization):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    if body.job_id in _processor_jobs:
        return JSONResponse(status_code=409, content={"error": "job_id already exists"})

    # Separate GoPro chapters from drone inputs by label
    # GoPro: label is "gopro", "gopro_01", "gopro_02", etc. (sorted = chapter order)
    # Drone: label starts with "drone"
    all_video = [i for i in body.inputs if "video" in i.type]
    drone_inputs = [i for i in all_video if "drone" in (i.label or "").lower()]
    gopro_inputs = sorted(
        [i for i in all_video if i not in drone_inputs],
        key=lambda x: x.label or "",
    )
    # Fallback: if nothing labelled, treat first video as GoPro, rest as drones
    if not gopro_inputs and all_video:
        gopro_inputs = [all_video[0]]
        drone_inputs = all_video[1:]

    if not gopro_inputs:
        raise HTTPException(status_code=422, detail="No GoPro video input found")

    cfg = body.config
    syncs = cfg.get("syncs", [])
    height = int(cfg.get("height", 720))

    # Build drone filename list from labels
    drone_urls: list[str] = []
    drone_filenames: list[str] = []
    for i, d in enumerate(drone_inputs):
        drone_urls.append(d.url)
        label = (d.label or f"drone_{i + 1:02d}").lower()
        fname = label if label.endswith(".mp4") else f"{label}.mp4"
        if not fname.startswith("drone_"):
            fname = f"drone_{fname}"
        drone_filenames.append(fname)

    # Map syncs drone_file to our generated filenames
    mapped_syncs: list[dict] = []
    for s in syncs:
        orig_df = s.get("drone_file", "")
        matched_fname = next(
            (drone_filenames[i] for i, d in enumerate(drone_inputs)
             if d.label and (orig_df == d.label or orig_df == drone_filenames[i])),
            orig_df,
        )
        mapped_syncs.append({**s, "drone_file": matched_fname})

    # Register job
    with _processor_jobs_lock:
        _processor_jobs[body.job_id] = {
            "job_id": body.job_id,
            "status": "queued",
            "stage": body.stage,
            "step": None,
            "callback_url": body.callback_url,
            "outputs": [],
            "error": None,
            "created_at": datetime.utcnow().isoformat(),
        }

    # Start background thread
    t = threading.Thread(
        target=_run_processor_job,
        kwargs={
            "job_id": body.job_id,
            "gopro_urls": [i.url for i in gopro_inputs],  # list — may be multiple chapters
            "drone_urls": drone_urls,
            "drone_filenames": drone_filenames,
            "syncs": mapped_syncs,
            "callback_url": body.callback_url,
            "stage": body.stage,
            "height": height,
        },
        daemon=True,
    )
    t.start()
    return {"accepted": True, "job_id": body.job_id}


@app.get("/jobs/{job_id}")
def get_processor_job(job_id: str):
    with _processor_jobs_lock:
        job = _processor_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/outputs/{filename}")
def get_processor_output(job_id: str, filename: str):
    allowed = {"splitscreen.mp4", "gopro.mp4", "drone.mp4"}
    if filename not in allowed:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(f"/tmp/missmatch-ms/{job_id}/output/{filename}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), media_type="video/mp4")


# ── Gandalf auth routes ───────────────────────────────────────────────────────

@app.get("/auth/config")
def auth_config():
    use_gandalf = bool(GANDALF_URL and GANDALF_JWT_SECRET)
    return {"gandalf_url": GANDALF_URL if use_gandalf else None, "use_gandalf": use_gandalf}


@app.get("/auth/status")
def auth_status(session_token: Optional[str] = Cookie(None)):
    """Check if the current session is authenticated."""
    authenticated = (
        bool(session_token)
        and session_token in _sessions
        and _sessions[session_token] >= datetime.now()
    )
    return {"authenticated": authenticated}


@app.get("/auth/callback")
def auth_callback(token: str):
    if not GANDALF_JWT_SECRET:
        raise HTTPException(status_code=503, detail="Gandalf auth not configured")
    try:
        _jwt.decode(token, GANDALF_JWT_SECRET, algorithms=["HS256"], audience="strike")
    except _jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Login link expired")
    except _jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    session_token = str(uuid.uuid4())
    _sessions[session_token] = datetime.now() + timedelta(days=SESSION_TTL_DAYS)

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        "session_token", session_token,
        httponly=True, samesite="lax",
        max_age=SESSION_TTL_DAYS * 86_400,
        secure=SECURE_COOKIES,
    )
    return resp


@app.post("/auth/logout")
def logout():
    return JSONResponse({"message": "Logged out"})


@app.get("/auth/logout")
def logout_redirect():
    return RedirectResponse(url="/", status_code=302)


# ---------- static files (UI) ----------

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
