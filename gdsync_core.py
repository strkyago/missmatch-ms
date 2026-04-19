#!/usr/bin/env python3
"""
gdsync_core — core logic for aligning drone footage to GoPro's timeline.

This module is importable (no sys.exit calls). The original gdsync.py CLI
entry-point still works by importing from here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable


# ---------- exceptions ----------

class GDSyncError(RuntimeError):
    """Raised instead of sys.exit() so callers can handle errors gracefully."""


# ---------- small helpers ----------

def require_binaries() -> None:
    for b in ("ffmpeg", "ffprobe"):
        if shutil.which(b) is None:
            raise GDSyncError(f"'{b}' not found on PATH. Install ffmpeg and try again.")


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr if capture else "(see above)"
        raise GDSyncError(f"command failed: {' '.join(cmd)}\n{stderr}")
    return result


def probe_duration(path: Path) -> float:
    r = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    return float(r.stdout.strip())


_TIME_RE = re.compile(r"""
    ^\s*
    (?:
        (?P<hms>(?:\d+:)?\d{1,2}:\d{1,2}(?:\.\d+)?)
      | (?P<sec>\d+(?:\.\d+)?)
    )
    \s*$
""", re.VERBOSE)


def parse_time(s: str) -> float:
    """Accept '93.5', '1:33.5', or '01:02:33.5' and return seconds (float)."""
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"not a timestamp: {s!r}")
    if m.group("sec"):
        return float(m.group("sec"))
    parts = [float(p) for p in m.group("hms").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, mn, se = parts
    return h * 3600 + mn * 60 + se


def fmt_time(t: float) -> str:
    h = int(t // 3600)
    mn = int((t % 3600) // 60)
    se = t - h * 3600 - mn * 60
    if h:
        return f"{h}:{mn:02d}:{se:06.3f}"
    return f"{mn}:{se:06.3f}"


# ---------- data model ----------

@dataclass
class FlightSync:
    """Maps GoPro time onto drone time for one flight."""
    drone_file: str
    drone_duration: float
    gopro_marker: float
    drone_marker: float

    @property
    def gopro_flight_start(self) -> float:
        return self.gopro_marker - self.drone_marker

    @property
    def gopro_flight_end(self) -> float:
        return self.gopro_flight_start + self.drone_duration


# ---------- session discovery ----------

def discover_session(folder: Path) -> tuple[Path, list[Path]]:
    gopro = folder / "gopro.mp4"
    if not gopro.is_file():
        candidates = [p for p in folder.iterdir()
                      if p.is_file() and p.stem.lower() == "gopro"
                      and p.suffix.lower() in (".mp4", ".mov")]
        if not candidates:
            raise GDSyncError(f"no gopro.mp4 in {folder}")
        gopro = candidates[0]

    drone_files = sorted(
        p for p in folder.iterdir()
        if p.is_file()
        and p.stem.lower().startswith("drone_")
        and p.suffix.lower() in (".mp4", ".mov")
    )
    if not drone_files:
        raise GDSyncError(f"no drone_*.mp4 files in {folder}")
    return gopro, drone_files


# ---------- sync persistence ----------

def load_syncs(session: Path) -> list[FlightSync]:
    """Load existing sync.json; returns empty list if not found."""
    sync_path = session / "output" / "sync.json"
    if not sync_path.is_file():
        return []
    raw = json.loads(sync_path.read_text())
    return [FlightSync(**entry) for entry in raw]


def save_syncs(session: Path, syncs: list[FlightSync]) -> Path:
    """Persist syncs to output/sync.json and return the path."""
    out_dir = session / "output"
    out_dir.mkdir(exist_ok=True)
    sync_path = out_dir / "sync.json"
    sync_path.write_text(json.dumps([asdict(s) for s in syncs], indent=2))
    return sync_path


# ---------- ffmpeg operations ----------

def _target_wh(height: int) -> tuple[int, int]:
    width = int(round(height * 16 / 9))
    if width % 2:
        width += 1
    return width, height


def _scale_pad_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps=30"
    )


def extract_video_only(src: Path, start: float, duration: float, dst: Path,
                       width: int, height: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-an",
        "-vf", _scale_pad_filter(width, height),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(dst),
    ])


def make_black_clip(duration: float, width: int, height: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={duration:.3f}:r=30",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(dst),
    ])


def concat_video_only(clips: list[Path], dst: Path) -> None:
    listfile = dst.with_suffix(".list.txt")
    listfile.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(listfile),
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(dst),
    ])
    try:
        listfile.unlink()
    except OSError:
        pass


# ---------- timeline building ----------

@dataclass
class _Window:
    drone_file: str
    drone_start: float
    duration: float
    gopro_start: float
    gopro_end: float


def plan_timeline(syncs: list[FlightSync], gopro_duration: float) -> list[_Window]:
    windows: list[_Window] = []
    for s in syncs:
        gstart = s.gopro_flight_start
        gend = s.gopro_flight_end
        clamped_start = max(0.0, gstart)
        clamped_end = min(gopro_duration, gend)
        if clamped_end <= clamped_start + 0.05:
            continue
        windows.append(_Window(
            drone_file=s.drone_file,
            drone_start=clamped_start - gstart,
            duration=clamped_end - clamped_start,
            gopro_start=clamped_start,
            gopro_end=clamped_end,
        ))
    windows.sort(key=lambda w: w.gopro_start)

    for a, b in zip(windows, windows[1:]):
        if a.gopro_end > b.gopro_start:
            trim = a.gopro_end - b.gopro_start
            a.gopro_end = b.gopro_start
            a.duration -= trim
    return windows


def build_aligned_drone(
    session: Path,
    gopro: Path,
    drone_files: list[Path],
    syncs: list[FlightSync],
    *,
    height: int = 720,
    log: Callable[[str], None] = print,
) -> tuple[Path, Path]:
    """
    Produce output/gopro.mp4 (copy) and output/drone.mp4 (aligned).
    Returns (gopro_out, drone_out).
    """
    require_binaries()
    width, height = _target_wh(height)
    gopro_duration = probe_duration(gopro)
    out_dir = session / "output"
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    drone_by_name = {p.name: p for p in drone_files}

    # --- gopro output: stream copy, no re-encode ---
    gopro_out = out_dir / "gopro.mp4"
    log("Copying GoPro file…")
    shutil.copy2(gopro, gopro_out)
    log(f"GoPro copy saved → {gopro_out.name}")

    # --- drone output ---
    windows = plan_timeline(syncs, gopro_duration)
    if not windows:
        raise GDSyncError("no drone windows fall within the gopro recording; check your sync points")

    segments: list[Path] = []
    cursor = 0.0
    total_steps = len(windows) * 2 + 2  # rough progress denominator
    step = 0

    for idx, w in enumerate(windows):
        if w.gopro_start > cursor + 0.01:
            gap = w.gopro_start - cursor
            seg = seg_dir / f"black_{len(segments):03d}.mp4"
            log(f"[{step}/{total_steps}] Black gap {fmt_time(cursor)}–{fmt_time(w.gopro_start)} ({gap:.2f}s)")
            make_black_clip(gap, width, height, seg)
            segments.append(seg)
            cursor = w.gopro_start
            step += 1

        seg = seg_dir / f"flight_{idx+1:02d}.mp4"
        log(f"[{step}/{total_steps}] Encoding flight {idx+1}: {w.drone_file} "
            f"{fmt_time(w.gopro_start)}–{fmt_time(w.gopro_end)}")
        extract_video_only(drone_by_name[w.drone_file], w.drone_start, w.duration,
                           seg, width, height)
        segments.append(seg)
        cursor = w.gopro_end
        step += 1

    if cursor < gopro_duration - 0.01:
        gap = gopro_duration - cursor
        seg = seg_dir / f"black_{len(segments):03d}.mp4"
        log(f"[{step}/{total_steps}] Black trailing gap ({gap:.2f}s)")
        make_black_clip(gap, width, height, seg)
        segments.append(seg)
        step += 1

    drone_out = out_dir / "drone.mp4"
    if len(segments) == 1:
        shutil.copyfile(segments[0], drone_out)
    else:
        log(f"[{step}/{total_steps}] Concatenating {len(segments)} segments…")
        concat_video_only(segments, drone_out)

    return gopro_out, drone_out
