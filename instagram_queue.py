#!/usr/bin/env python3
"""Prepare rendered Timelapser videos for Instagram publishing.

This script does not upload or publish anything. It creates a small queue item
containing the clean Reel MP4 plus caption and metadata files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from post_metadata import write_post_files


DEFAULT_QUEUE_DIR = Path("instagram_queue") / "ready"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_telemetry_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "telemetry.csv"
    if not path.exists():
        return {}

    rows: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return {}

    if not rows:
        return {}

    first, last = rows[0], rows[-1]
    return {
        "frames": len(rows),
        "first_frame": first.get("frame"),
        "last_frame": last.get("frame"),
        "started_at": first.get("time") or first.get("timestamp"),
        "ended_at": last.get("time") or last.get("timestamp"),
        "first_remote_jpg": first.get("remote_jpg"),
        "last_remote_jpg": last.get("remote_jpg"),
    }


def run_mode(run_dir: Path, run_summary: dict[str, Any]) -> str:
    mode = str(run_summary.get("mode") or "").strip().lower()
    if mode in {"sunrise", "sunset", "general"}:
        return mode
    for candidate in ("sunrise", "sunset", "general"):
        if f"_{candidate}_" in run_dir.name:
            return candidate
    return "timelapse"


def title_mode(mode: str) -> str:
    return "time-lapse" if mode == "general" else f"{mode} time-lapse"


def format_clock(iso_text: str | None) -> str:
    if not iso_text:
        return ""
    try:
        return datetime.fromisoformat(str(iso_text)).strftime("%H:%M")
    except Exception:
        return str(iso_text)[:16]


def build_caption(run_dir: Path, metadata: dict[str, Any], custom_caption: str | None = None) -> str:
    if custom_caption:
        return custom_caption.strip()

    post_caption = str(metadata.get("post_caption") or "").strip()
    if post_caption:
        return post_caption

    mode = str(metadata.get("mode") or "timelapse")
    frames = metadata.get("frames")
    started = format_clock(metadata.get("started_at"))
    ended = format_clock(metadata.get("ended_at"))
    date = ""
    try:
        raw = metadata.get("started_at") or metadata.get("written_at")
        if raw:
            date = datetime.fromisoformat(str(raw)).strftime("%Y-%m-%d")
    except Exception:
        pass

    pieces = [f"Kuala Lumpur {title_mode(mode)}"]
    if date:
        pieces.append(date)
    if started and ended:
        pieces.append(f"captured {started}-{ended}")
    if frames:
        pieces.append(f"{frames} frames")
    pieces.append("Olympus E-M5 Mark III")
    pieces.append("Raspberry Pi Timelapser V5")

    tags = "#timelapse #kualalumpur #sunset #sunrise #olympus #raspberrypi #photography"
    return ". ".join(pieces) + ".\n\n" + tags


def ffprobe_video(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate,duration",
        "-show_entries",
        "format=size,duration,format_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return {"available": False, "error": "ffprobe not found"}
    if proc.returncode != 0:
        return {"available": False, "error": proc.stderr.strip() or proc.stdout.strip()}
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return {"available": False, "error": f"ffprobe JSON parse failed: {exc}"}
    data["available"] = True
    return data


def find_clean_reel(run_dir: Path) -> Path:
    candidates = sorted(run_dir.glob(f"{run_dir.name}_instagram-reel_clean.mp4"))
    if not candidates:
        candidates = sorted(run_dir.glob("*instagram-reel_clean.mp4"))
    if not candidates:
        raise FileNotFoundError(f"No clean Instagram Reel MP4 found in {run_dir}")
    return candidates[-1]


def safe_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return cleaned or "timelapser_reel"


def prepare_queue_item(
    run_dir: Path,
    *,
    video: Path | None,
    queue_dir: Path,
    caption: str | None,
    force: bool,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    source_video = (video.expanduser().resolve() if video else find_clean_reel(run_dir))
    if not source_video.exists():
        raise FileNotFoundError(f"Video not found: {source_video}")

    run_summary = read_json(run_dir / "run_summary.json")
    render_summary = read_json(run_dir / "render_summary_v3.json")
    telemetry = read_telemetry_summary(run_dir)
    mode = run_mode(run_dir, run_summary)
    post_metadata = write_post_files(run_dir)

    item_dir = queue_dir.expanduser().resolve() / safe_name(run_dir.name)
    item_dir.mkdir(parents=True, exist_ok=True)
    dest_video = item_dir / source_video.name
    if dest_video.exists() and not force:
        raise FileExistsError(f"Queued video already exists; use --force: {dest_video}")
    shutil.copy2(source_video, dest_video)

    metadata = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "ready",
        "run_dir": str(run_dir),
        "source_video": str(source_video),
        "queued_video": str(dest_video),
        "caption_file": str(item_dir / "caption.txt"),
        "mode": mode,
        "frames": telemetry.get("frames") or run_summary.get("downloaded_frames"),
        "started_at": telemetry.get("started_at"),
        "ended_at": telemetry.get("ended_at"),
        "run_summary": run_summary,
        "render_summary": render_summary,
        "post_metadata": post_metadata,
        "post_title": post_metadata.get("title"),
        "post_caption": post_metadata.get("caption"),
        "video_probe": ffprobe_video(dest_video),
    }
    caption_text = build_caption(run_dir, metadata, caption)

    (item_dir / "caption.txt").write_text(caption_text + "\n", encoding="utf-8")
    (item_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    status = {
        "status": "ready",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "queued_video": str(dest_video),
        "caption_file": str(item_dir / "caption.txt"),
        "publish_target": "instagram:timelapser.118",
    }
    (item_dir / "publish_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return {**metadata, "caption": caption_text, "queue_item_dir": str(item_dir)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a rendered Timelapser video for Instagram publishing.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--video", type=Path, default=None, help="Specific MP4 to queue. Defaults to clean Instagram Reel.")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    parser.add_argument("--caption", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    result = prepare_queue_item(
        args.run_dir,
        video=args.video,
        queue_dir=args.queue_dir,
        caption=args.caption,
        force=args.force,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
