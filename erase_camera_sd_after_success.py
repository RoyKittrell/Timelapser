#!/usr/bin/env python3
"""Erase camera media only after a V6 run has been safely finalized."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import mega4_usb_import
from timelapser_v6_usb_import import validate_jpeg


CAMERA_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".orf", ".mov", ".mp4", ".avi", ".wav",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_completed_run(run_dir: Path) -> dict:
    summary = read_json(run_dir / "postprocess_workflow_summary.json")
    if summary.get("status") != "complete":
        raise RuntimeError("Refusing SD erase: postprocess workflow is not complete")

    frames = sorted((run_dir / "frames_full_jpeg").glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError("Refusing SD erase: no imported full-resolution JPEGs")
    for frame in frames:
        validate_jpeg(frame)

    render_summaries = sorted(run_dir.glob("render_summary_v3*.json"))
    completed_render = None
    for path in render_summaries:
        payload = read_json(path)
        if payload.get("status") == "complete":
            completed_render = payload
            break
    if completed_render is None:
        raise RuntimeError("Refusing SD erase: no completed render summary")

    outputs = completed_render.get("outputs") or []
    if not outputs:
        raise RuntimeError("Refusing SD erase: completed render has no outputs")
    for output in outputs:
        if int(output.get("returncode", 1)) != 0:
            raise RuntimeError("Refusing SD erase: a rendered output failed")
        path = Path(output.get("output") or "")
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Refusing SD erase: rendered output is missing: {path}")

    return {
        "validated_frames": len(frames),
        "validated_outputs": len(outputs),
    }


def erase_dcim_media(sd_root: Path, *, dry_run: bool = False) -> list[str]:
    dcim = sd_root / "DCIM"
    if not dcim.is_dir():
        raise FileNotFoundError(f"No DCIM directory under {sd_root}")
    media = sorted(
        path for path in dcim.rglob("*")
        if path.is_file() and path.suffix.lower() in CAMERA_MEDIA_SUFFIXES
    )
    relative = [path.relative_to(sd_root).as_posix() for path in media]
    if not dry_run:
        for path in media:
            path.unlink()
    return relative


def erase_after_success(run_dir: Path, *, dry_run: bool = False) -> dict:
    run_dir = run_dir.expanduser().resolve()
    verification = verify_completed_run(run_dir)
    device = None
    try:
        mega4_usb_import.set_port("on")
        device = mega4_usb_import.wait_for_device(45)
        sd_root = mega4_usb_import.ensure_mounted(device)
        deleted = erase_dcim_media(sd_root, dry_run=dry_run)
        receipt = {
            "status": "dry_run" if dry_run else "complete",
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_dir": str(run_dir),
            **verification,
            "deleted_media_count": len(deleted),
            "deleted_media": deleted,
        }
        if not dry_run:
            (run_dir / "camera_sd_cleanup.json").write_text(
                json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(receipt, indent=2), flush=True)
        return receipt
    finally:
        mega4_usb_import.cleanup(device)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    erase_after_success(args.run_dir, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
