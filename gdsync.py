#!/usr/bin/env python3
"""
gdsync — align drone footage to the GoPro's timeline.

Takes the raw GoPro recording and the drone flight files from a single session,
and produces one silent drone.mp4 that has exactly the same duration as the
GoPro. Each flight's footage appears at the GoPro timestamp when the drone was
actually flying; moments when the drone was on the ground (battery swap,
before/after the session) are filled with black frames.

Input: a session folder that contains
    gopro.mp4              the raw, full GoPro recording
    drone_01.mp4           first drone flight (chronological order)
    drone_02.mp4           second drone flight
    ...
    drone_NN.mp4

Output (written to <session>/output/)
    sync.json              per-flight sync offsets, persisted between runs
    drone.mp4              single silent file, same length as gopro.mp4

On-field convention
    Every time the drone lands for a battery swap, the player wearing the
    GoPro is called in and holds up a "FLIGHT N" cardboard in view of both
    cameras for a few seconds. The script prompts once per flight for the
    GoPro and drone timestamp of that marker; those offsets are saved to
    sync.json so subsequent runs are non-interactive.

How the editing workflow then works
    1. Run this script on your session folder.
    2. Run the "nice" microservice on gopro.mp4 — it outputs a highlight clip
       plus a timestamps sidecar (e.g. highlights.json).
    3. In CapCut, drop both gopro.mp4 and output/drone.mp4 on two tracks.
       They have identical length, so any cut you make on the GoPro track at
       a "nice" timestamp is already in sync with the drone track.

Requires: python 3.9+, ffmpeg and ffprobe on PATH.

Usage
    python3 gdsync.py /path/to/session_folder
    python3 gdsync.py /path/to/session_folder --resync         # redo sync points
    python3 gdsync.py /path/to/session_folder --height 1080    # output video height
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


# ---------- small helpers ----------

def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


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
        die(f"command failed: {' '.join(cmd)}\n{stderr}")
    return result


def require_binaries() -> None:
    for b in ("ffmpeg", "ffprobe"):
        if shutil.which(b) is None:
            die(f"'{b}' not found on PATH. Install ffmpeg and try again.")


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
        """The GoPro timestamp at which this flight's drone recording began (t=0)."""
        return self.gopro_marker - self.drone_marker

    @property
    def gopro_flight_end(self) -> float:
        """The GoPro timestamp at which this flight's drone recording ended."""
        return self.gopro_flight_start + self.drone_duration


# ---------- session discovery ----------

def discover_session(folder: Path) -> tuple[Path, list[Path]]:
    gopro = folder / "gopro.mp4"
    if not gopro.is_file():
        candidates = [p for p in folder.iterdir()
                      if p.is_file() and p.stem.lower() == "gopro"
                      and p.suffix.lower() in (".mp4", ".mov")]
        if not candidates:
            die(f"no gopro.mp4 in {folder}")
        gopro = candidates[0]

    drone_files = sorted(
        p for p in folder.iterdir()
        if p.is_file()
        and p.stem.lower().startswith("drone_")
        and p.suffix.lower() in (".mp4", ".mov")
    )
    if not drone_files:
        die(f"no drone_*.mp4 files in {folder}")
    return gopro, drone_files


# ---------- sync prompting ----------

def prompt_flight_sync(drone_file: Path, index: int, drone_duration: float) -> FlightSync:
    print()
    print(f"  ── FLIGHT {index}  ──  {drone_file.name}  (duration {fmt_time(drone_duration)})")
    print( "     Find the frame where the 'FLIGHT' cardboard is held up.")
    print( "     Accepted timestamp formats: '93.5', '1:33.5', or '1:02:33.5'.")
    while True:
        try:
            g = parse_time(input(f"     GoPro  time of marker for flight {index}: "))
            d = parse_time(input(f"     Drone  time of marker for flight {index}: "))
        except (ValueError, EOFError) as e:
            print(f"     -> {e}; try again.")
            continue
        if not (0 <= d <= drone_duration):
            print(f"     -> drone time {fmt_time(d)} is outside the clip's duration; try again.")
            continue
        return FlightSync(
            drone_file=drone_file.name,
            drone_duration=drone_duration,
            gopro_marker=g,
            drone_marker=d,
        )


def load_or_capture_syncs(
    session: Path,
    drone_files: list[Path],
    *,
    force: bool,
) -> list[FlightSync]:
    out_dir = session / "output"
    out_dir.mkdir(exist_ok=True)
    sync_path = out_dir / "sync.json"

    existing: dict[str, FlightSync] = {}
    if sync_path.is_file() and not force:
        raw = json.loads(sync_path.read_text())
        for entry in raw:
            fs = FlightSync(**entry)
            existing[fs.drone_file] = fs

    syncs: list[FlightSync] = []
    captured_any_new = False
    for i, df in enumerate(drone_files, start=1):
        if df.name in existing:
            syncs.append(existing[df.name])
            continue
        dur = probe_duration(df)
        syncs.append(prompt_flight_sync(df, i, dur))
        captured_any_new = True

    if captured_any_new or force:
        sync_path.write_text(json.dumps([asdict(s) for s in syncs], indent=2))
        print(f"\n  sync points saved to {sync_path}")
    return syncs


# ---------- ffmpeg operations ----------

def _target_wh(height: int) -> tuple[int, int]:
    """Pick a 16:9 output resolution at the requested height; width kept even."""
    width = int(round(height * 16 / 9))
    if width % 2:
        width += 1
    return width, height


def _scale_pad_filter(width: int, height: int) -> str:
    """Scale preserving aspect, pad with black to hit the exact target resolution."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps=30"
    )


def extract_video_only(src: Path, start: float, duration: float, dst: Path,
                        width: int, height: int) -> None:
    """Cut [start, start+duration] from src, scaled + padded, video only."""
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
    """Generate a silent black clip (video only) at the target resolution."""
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
    """Concatenate same-spec video-only clips via the concat demuxer."""
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
        pass  # best-effort cleanup


# ---------- timeline building ----------

@dataclass
class _Window:
    """A chunk of drone footage placed on the GoPro timeline."""
    drone_file: str
    drone_start: float     # seconds into the drone file
    duration: float        # how long the chunk lasts
    gopro_start: float     # GoPro timestamp where this chunk starts
    gopro_end: float       # GoPro timestamp where this chunk ends


def plan_timeline(
    syncs: list[FlightSync],
    gopro_duration: float,
) -> list[_Window]:
    """Compute where each flight sits on the GoPro timeline, clamped to [0, gopro_duration]."""
    windows: list[_Window] = []
    for s in syncs:
        gstart = s.gopro_flight_start
        gend = s.gopro_flight_end
        clamped_start = max(0.0, gstart)
        clamped_end = min(gopro_duration, gend)
        if clamped_end <= clamped_start + 0.05:
            print(f"  warn: flight {s.drone_file} window [{fmt_time(gstart)}, {fmt_time(gend)}] "
                  f"is outside the gopro duration; skipping")
            continue
        windows.append(_Window(
            drone_file=s.drone_file,
            drone_start=clamped_start - gstart,
            duration=clamped_end - clamped_start,
            gopro_start=clamped_start,
            gopro_end=clamped_end,
        ))
    windows.sort(key=lambda w: w.gopro_start)

    # Trim overlaps so later flights win the overlap (shouldn't happen if sync is correct).
    for a, b in zip(windows, windows[1:]):
        if a.gopro_end > b.gopro_start:
            print(f"  warn: {a.drone_file} overlaps {b.drone_file} by "
                  f"{a.gopro_end - b.gopro_start:.2f}s; trimming the earlier flight")
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
    height: int,
) -> Path:
    """Produce <session>/output/drone.mp4 that is exactly gopro.mp4's duration."""
    width, height = _target_wh(height)
    gopro_duration = probe_duration(gopro)
    out_dir = session / "output"
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    drone_by_name = {p.name: p for p in drone_files}

    windows = plan_timeline(syncs, gopro_duration)

    segments: list[Path] = []
    cursor = 0.0
    for idx, w in enumerate(windows):
        if w.gopro_start > cursor + 0.01:
            gap = w.gopro_start - cursor
            seg = seg_dir / f"black_{len(segments):03d}.mp4"
            print(f"  {fmt_time(cursor)}–{fmt_time(w.gopro_start)}  black ({gap:.2f}s — drone on ground)")
            make_black_clip(gap, width, height, seg)
            segments.append(seg)
            cursor = w.gopro_start

        seg = seg_dir / f"flight_{idx+1:02d}.mp4"
        print(f"  {fmt_time(w.gopro_start)}–{fmt_time(w.gopro_end)}  "
              f"{w.drone_file}  (from drone t={fmt_time(w.drone_start)}, {w.duration:.2f}s)")
        extract_video_only(drone_by_name[w.drone_file], w.drone_start, w.duration,
                           seg, width, height)
        segments.append(seg)
        cursor = w.gopro_end

    if cursor < gopro_duration - 0.01:
        gap = gopro_duration - cursor
        seg = seg_dir / f"black_{len(segments):03d}.mp4"
        print(f"  {fmt_time(cursor)}–{fmt_time(gopro_duration)}  black ({gap:.2f}s — trailing)")
        make_black_clip(gap, width, height, seg)
        segments.append(seg)

    if not segments:
        die("no drone windows fall within the gopro recording; check your sync points")

    final = out_dir / "drone.mp4"
    if len(segments) == 1:
        shutil.copyfile(segments[0], final)
    else:
        concat_video_only(segments, final)
    return final


# ---------- entry point ----------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("session", type=Path,
                    help="session folder with gopro.mp4 and drone_*.mp4")
    ap.add_argument("--resync", action="store_true",
                    help="re-prompt for every flight's sync point even if sync.json exists")
    ap.add_argument("--height", type=int, default=720,
                    help="output video height in pixels (default 720; width is 16:9)")
    args = ap.parse_args()

    require_binaries()

    session: Path = args.session.expanduser().resolve()
    if not session.is_dir():
        die(f"not a folder: {session}")

    gopro, drone_files = discover_session(session)
    gopro_duration = probe_duration(gopro)

    print(f"session:  {session}")
    print(f"gopro:    {gopro.name}   ({fmt_time(gopro_duration)})")
    print(f"flights:")
    for df in drone_files:
        print(f"  - {df.name}")

    syncs = load_or_capture_syncs(session, drone_files, force=args.resync)

    print("\ntimeline:")
    final = build_aligned_drone(session, gopro, drone_files, syncs, height=args.height)

    final_duration = probe_duration(final)
    print(f"\n  done -> {final}")
    print(f"  gopro {fmt_time(gopro_duration)}   drone {fmt_time(final_duration)}   "
          f"(diff {abs(gopro_duration - final_duration):.3f}s)")


if __name__ == "__main__":
    main()
