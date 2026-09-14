#!/usr/bin/env python3
"""Post-run workflow for Timelapser V5.

Runs only after capture has stopped:
1. recover full-resolution JPEGs from the camera SD card
2. render the clean/brightness/director videos from those full JPEGs
3. optionally copy the videos to Roy's Mac
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

DEFAULT_MAC_VIDEO_DEST = (
    "roy@192.168.100.191:/Users/roy/Documents/ChatGPT/Timelapser/rendered_videos"
)
DEFAULT_TIMELAPSER_PYTHON = "/home/roy/timelapser-venv/bin/python3"


def _log(log_path: Path, message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_step(log_path: Path, name: str, cmd: list[str], cwd: Path) -> dict:
    started = time.monotonic()
    _log(log_path, f"START {name}: " + " ".join(shlex.quote(x) for x in cmd))
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n" + "=" * 72 + f"\n{name}\n" + "=" * 72 + "\n")
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    elapsed = time.monotonic() - started
    _log(log_path, f"END {name}: returncode={proc.returncode} elapsed={elapsed:.1f}s")
    return {
        "name": name,
        "command": cmd,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
    }


def _python_executable() -> str:
    configured = os.environ.get("TIMELAPSER_PYTHON")
    if configured and Path(configured).exists():
        return configured
    if Path(DEFAULT_TIMELAPSER_PYTHON).exists():
        return DEFAULT_TIMELAPSER_PYTHON
    return sys.executable


def _split_remote_dest(dest: str) -> tuple[str, str] | None:
    if ":" not in dest or dest.startswith("/"):
        return None
    host, path = dest.split(":", 1)
    if not host or not path.startswith("/"):
        return None
    return host, path


def _ensure_remote_dir(log_path: Path, dest: str, cwd: Path) -> dict | None:
    parsed = _split_remote_dest(dest)
    if parsed is None:
        return None
    host, path = parsed
    return _run_step(
        log_path,
        "prepare_mac_video_destination",
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            host,
            "mkdir",
            "-p",
            path,
        ],
        cwd,
    )


def _copy_videos_to_mac(log_path: Path, run_dir: Path, dest: str, cwd: Path) -> dict:
    videos = sorted(run_dir.glob(f"{run_dir.name}_*.mp4"))
    if not videos:
        _log(log_path, "No rendered MP4 files found to copy.")
        return {
            "name": "copy_videos_to_mac",
            "returncode": 1,
            "outputs": [],
            "error": "no rendered MP4 files found",
        }

    prepare = _ensure_remote_dir(log_path, dest, cwd)
    if prepare is not None and prepare["returncode"] != 0:
        return {
            "name": "copy_videos_to_mac",
            "returncode": prepare["returncode"],
            "outputs": [str(p) for p in videos],
            "error": "could not prepare remote destination",
        }

    cmd = [
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        *[str(p) for p in videos],
        dest,
    ]
    result = _run_step(log_path, "copy_videos_to_mac", cmd, cwd)
    result["outputs"] = [str(p) for p in videos]
    result["destination"] = dest
    return result


def run_postprocess(
    run_dir: Path,
    *,
    download_fullres: bool,
    render: bool,
    copy_videos_to_mac: bool,
    mac_video_dest: str | None = None,
) -> dict:
    run_dir = Path(run_dir).expanduser().resolve()
    root = Path(__file__).resolve().parent
    log_path = run_dir / "postprocess_workflow.log"
    summary_path = run_dir / "postprocess_workflow_summary.json"
    dest = mac_video_dest or os.environ.get("TIMELAPSER_MAC_VIDEO_DEST") or DEFAULT_MAC_VIDEO_DEST
    python = _python_executable()

    summary = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "download_fullres": bool(download_fullres),
        "render": bool(render),
        "copy_videos_to_mac": bool(copy_videos_to_mac),
        "mac_video_dest": dest,
        "steps": [],
        "status": "not_started",
    }

    try:
        _log(log_path, "=" * 72)
        _log(log_path, "TIMELAPSER V5 POST-RUN WORKFLOW")
        _log(log_path, f"Run directory: {run_dir}")

        if download_fullres:
            step = _run_step(
                log_path,
                "download_fullres_jpegs",
                [
                    python,
                    str(root / "download_run_fullres.py"),
                    str(run_dir),
                    "--attempts-per-frame",
                    "4",
                    "--repair-passes",
                    "5",
                    "--operation-timeout",
                    "120",
                ],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_download_fullres"
                return summary

        if render:
            source = run_dir / "frames_full_jpeg"
            step = _run_step(
                log_path,
                "render_videos",
                [
                    python,
                    str(root / "render_timelapse.py"),
                    str(run_dir),
                    "--source",
                    str(source),
                    "--overwrite",
                ],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_render"
                return summary

        if copy_videos_to_mac:
            step = _copy_videos_to_mac(log_path, run_dir, dest, root)
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_copy_to_mac"
                return summary

        summary["status"] = "complete"
        return summary
    except Exception as exc:
        summary["status"] = "failed_exception"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        _log(log_path, f"FATAL postprocess exception: {summary['error']}")
        return summary
    finally:
        summary["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        _log(log_path, f"Postprocess status: {summary['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Timelapser V5 post-run workflow.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-download-fullres", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--skip-copy-videos-to-mac", action="store_true")
    parser.add_argument("--mac-video-dest", default=None)
    args = parser.parse_args()

    summary = run_postprocess(
        args.run_dir,
        download_fullres=not args.skip_download_fullres,
        render=not args.skip_render,
        copy_videos_to_mac=not args.skip_copy_videos_to_mac,
        mac_video_dest=args.mac_video_dest,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
