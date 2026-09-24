#!/usr/bin/env python3
"""Post-run workflow for Timelapser V5.

Runs only after capture has stopped:
1. recover full-resolution JPEGs from the camera SD card
2. smooth the downloaded JPEG sequence for final video output
3. render the clean/brightness/director videos from the smoothed JPEGs
4. prepare the final clean Reel in an Instagram queue
5. optionally copy the videos to Roy's Mac
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

DEFAULT_MAC_VIDEO_DEST = (
    "roy@192.168.100.191:/Users/roy/Documents/ChatGPT/Timelapser/rendered_videos"
)
DEFAULT_TIMELAPSER_PYTHON = "/home/roy/timelapser-venv/bin/python3"
SMOOTHED_FRAME_DIRNAME = "frames_smoothed_luma_v2"
SMOOTHING_ARGS = [
    "--window",
    "61",
    "--passes",
    "3",
    "--strength",
    "1.0",
    "--max-stops",
    "0.25",
    "--output-max-edge",
    "2400",
]


def _print_stdout(message: str) -> None:
    try:
        print(message, flush=True)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass


def _log(log_path: Path, message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _print_stdout(line)


def _write_live_status(log_path: Path, **changes) -> None:
    path = log_path.parent / "postprocess_live_status.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        current = {}
    completed = changes.pop("completed_step", None)
    next_step = changes.get("current_step")
    if next_step and next_step != current.get("current_step"):
        changes["current_step_started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    current.update(changes)
    if completed:
        names = list(current.get("completed_steps") or [])
        if completed not in names:
            names.append(completed)
        current["completed_steps"] = names
    current["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(current, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _record_error(run_dir: Path, where: str, **details) -> None:
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "where": where,
        **details,
    }
    with (run_dir / "errors.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _repair_unreadable_frames(run_dir: Path, frames_dir: Path, log_path: Path) -> dict:
    started = time.monotonic()
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    previews = {}
    damaged = []
    for index, path in enumerate(frames):
        try:
            with Image.open(path) as image:
                image.load()
                image.thumbnail((96, 160))
                previews[index] = np.asarray(image.convert("RGB"), dtype=np.float32)
        except (OSError, ValueError) as exc:
            damaged.append((index, path, type(exc).__name__, str(exc), None))
        if (index + 1) % 100 == 0 or index + 1 == len(frames):
            _log(
                log_path,
                f"JPEG validation progress: {index + 1}/{len(frames)} "
                f"elapsed={time.monotonic() - started:.1f}s",
            )

    visual_scores = []
    for index in range(1, len(frames) - 1):
        if any(neighbor not in previews for neighbor in (index - 1, index, index + 1)):
            continue
        previous, current, following = (previews[j] for j in (index - 1, index, index + 1))
        if previous.shape != current.shape or current.shape != following.shape:
            continue
        residual = current - (previous + following) / 2
        residual -= np.median(residual, axis=(0, 1))
        score = float(np.mean(np.abs(residual)))
        neighbor_difference = previous - following
        neighbor_difference -= np.median(neighbor_difference, axis=(0, 1))
        baseline = float(np.mean(np.abs(neighbor_difference)))
        visual_scores.append((index, score, baseline))

    for index, score, baseline in visual_scores:
        if score >= 8.0 and score >= 4 * max(baseline, 0.5):
            damaged.append((index, frames[index], "VisualOutlier",
                            f"temporal residual {score:.2f}, neighbor difference {baseline:.2f}", score))

    damaged_indices = {item[0] for item in damaged}
    valid = [index for index in previews if index not in damaged_indices]

    if damaged and not valid:
        raise RuntimeError(f"All {len(damaged)} JPEGs in {frames_dir} are unreadable")
    for index, path, error_type, error, score in damaged:
        source_index = min(valid, key=lambda candidate: (abs(candidate - index), candidate))
        source = frames[source_index]
        backup = path.with_suffix(".corrupt")
        if backup.exists():
            backup = path.with_name(f"{path.stem}.{int(time.time())}.corrupt")
        path.replace(backup)
        try:
            shutil.copy2(source, path)
        except Exception:
            backup.replace(path)
            raise
        _record_error(
            run_dir, "postprocess_jpeg_repair", frame=path.name,
            error_type=error_type, error=error, visual_score=score,
            replacement=source.name, preserved_original=backup.name,
        )
        _log(log_path, f"Repaired unreadable {path.name} using {source.name}; original: {backup.name}")
    elapsed = time.monotonic() - started
    return {"name": "validate_and_repair_jpegs", "returncode": 0,
            "checked": len(frames), "repaired": len(damaged),
            "elapsed_seconds": elapsed}


def _run_step(log_path: Path, name: str, cmd: list[str], cwd: Path) -> dict:
    started = time.monotonic()
    _write_live_status(log_path, status="running", current_step=name)
    _log(log_path, f"START {name}: " + " ".join(shlex.quote(x) for x in cmd))
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n" + "=" * 72 + f"\n{name}\n" + "=" * 72 + "\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            f.write(line)
            f.flush()
            _print_stdout(line.rstrip())
        proc.wait()
    elapsed = time.monotonic() - started
    _log(log_path, f"END {name}: returncode={proc.returncode} elapsed={elapsed:.1f}s")
    if proc.returncode == 0:
        _write_live_status(log_path, current_step="", completed_step=name)
    else:
        _write_live_status(log_path, status="failed", current_step=name)
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


def _queue_instagram_reel(log_path: Path, run_dir: Path, cwd: Path, python: str, video: Path | None = None) -> dict:
    cmd = [
        python,
        str(cwd / "instagram_queue.py"),
        str(run_dir),
        "--force",
    ]
    if video is not None:
        cmd.extend(["--video", str(video)])
    return _run_step(log_path, "queue_instagram_reel", cmd, cwd)


def _publish_instagram(log_path: Path, run_dir: Path, cwd: Path, python: str) -> dict:
    return _run_step(
        log_path,
        "publish_instagram_carousel_and_reel",
        [python, str(cwd / "instagram_auto_publish.py"), str(run_dir)],
        cwd,
    )


AUTO_PUBLISH_INSTAGRAM_MODES = frozenset({"sunrise", "sunset"})


def should_auto_publish_instagram(mode: str) -> bool:
    """Return whether a completed mission may publish to Instagram automatically."""
    return str(mode).strip().lower() in AUTO_PUBLISH_INSTAGRAM_MODES


def run_postprocess(
    run_dir: Path,
    *,
    download_fullres: bool,
    smooth_exposure: bool,
    render: bool,
    queue_instagram: bool = True,
    publish_instagram: bool = True,
    copy_videos_to_mac: bool,
    mac_video_dest: str | None = None,
) -> dict:
    workflow_started = time.monotonic()
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
        "smooth_exposure": bool(smooth_exposure),
        "render": bool(render),
        "queue_instagram": bool(queue_instagram),
        "publish_instagram": bool(publish_instagram),
        "copy_videos_to_mac": bool(copy_videos_to_mac),
        "mac_video_dest": dest,
        "steps": [],
        "status": "not_started",
    }
    _write_live_status(
        log_path,
        status="running",
        current_step="preparing",
        completed_steps=[],
        started_at=summary["started_at"],
        run_dir=str(run_dir),
        publish_instagram=bool(publish_instagram),
        elapsed_seconds=0,
        ended_at="",
        error="",
    )

    try:
        _log(log_path, "=" * 72)
        _log(log_path, "TIMELAPSER V5 POST-RUN WORKFLOW")
        _log(log_path, f"Run directory: {run_dir}")

        if download_fullres:
            step = _run_step(
                log_path,
                "import_fullres_jpegs_via_mega4_usb",
                [
                    python,
                    str(root / "mega4_usb_import.py"),
                    str(run_dir),
                ],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_download_fullres"
                return summary

        if render:
            fullres_source = run_dir / "frames_full_jpeg"
            smoothed_source = run_dir / SMOOTHED_FRAME_DIRNAME

            _write_live_status(log_path, status="running", current_step="validate_and_repair_jpegs")
            step = _repair_unreadable_frames(run_dir, fullres_source, log_path)
            summary["steps"].append(step)
            _write_live_status(log_path, current_step="", completed_step=step["name"])
            _log(log_path, f"JPEG check: {step['checked']} checked, {step['repaired']} repaired")
            if step["checked"] < 3:
                raise RuntimeError(f"Need at least 3 full-resolution JPEGs, found {step['checked']}")

            if smooth_exposure:
                step = _run_step(
                    log_path,
                    "smooth_exposure_v2",
                    [
                        python,
                        str(root / "smooth_exposure.py"),
                        str(fullres_source),
                        "--output",
                        str(smoothed_source),
                        "--apply",
                        "--overwrite",
                        *SMOOTHING_ARGS,
                    ],
                    root,
                )
                summary["steps"].append(step)
                if step["returncode"] != 0:
                    summary["status"] = "failed_smooth_exposure"
                    return summary

            source = smoothed_source if smooth_exposure else fullres_source
            if smooth_exposure:
                _write_live_status(log_path, status="running", current_step="validate_smoothed_jpegs")
                step = _repair_unreadable_frames(run_dir, source, log_path)
                summary["steps"].append(step)
                _write_live_status(log_path, current_step="", completed_step="validate_smoothed_jpegs")
                _log(log_path, f"Smoothed JPEG check: {step['checked']} checked, {step['repaired']} repaired")
            step = _run_step(
                log_path,
                "render_videos",
                [
                    python,
                    str(root / "render_timelapse.py"),
                    str(run_dir),
                    "--source",
                    str(source),
                    "--output-stem-suffix",
                    "_final",
                    "--brightness-source",
                    "frames",
                    "--overwrite",
                ],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_render"
                return summary

            step = _run_step(
                log_path,
                "create_instagram_carousel_formats",
                [
                    python,
                    str(root / "instagram_formats.py"),
                    str(run_dir),
                    "--overwrite",
                ],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_instagram_formats"
                return summary

        if queue_instagram:
            final_reel = run_dir / f"{run_dir.name}_final_instagram-reel_clean.mp4"
            step = _queue_instagram_reel(
                log_path,
                run_dir,
                root,
                python,
                video=final_reel if final_reel.exists() else None,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_queue_instagram"
                return summary

        if publish_instagram and render:
            step = _publish_instagram(log_path, run_dir, root, python)
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_publish_instagram"
                return summary

        if copy_videos_to_mac:
            step = _copy_videos_to_mac(log_path, run_dir, dest, root)
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_copy_to_mac"
                return summary

        if render:
            step = _run_step(
                log_path,
                "prune_rendered_images_older_than_30_days",
                [python, str(root / "prune_old_frames.py"), str(run_dir.parent), "--days", "30"],
                root,
            )
            summary["steps"].append(step)
            if step["returncode"] != 0:
                summary["status"] = "failed_image_retention"
                return summary

        summary["status"] = "complete"
        return summary
    except Exception as exc:
        summary["status"] = "failed_exception"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        _log(log_path, f"FATAL postprocess exception: {summary['error']}")
        return summary
    finally:
        summary["elapsed_seconds"] = time.monotonic() - workflow_started
        summary["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_live_status(
            log_path,
            status=summary["status"],
            current_step="",
            elapsed_seconds=summary["elapsed_seconds"],
            ended_at=summary["ended_at"],
            error=summary.get("error", ""),
        )
        if summary["status"].startswith("failed"):
            _record_error(
                run_dir, "postprocess_workflow", status=summary["status"],
                error=summary.get("error", "step returned a nonzero exit code"),
                failed_step=summary["steps"][-1]["name"] if summary["steps"] else None,
            )
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        _log(
            log_path,
            f"Postprocess status: {summary['status']} "
            f"elapsed={summary['elapsed_seconds']:.1f}s",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Timelapser V5 post-run workflow.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-download-fullres", action="store_true")
    parser.add_argument("--skip-smoothing", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--skip-instagram-queue", action="store_true")
    parser.add_argument("--skip-instagram-publish", action="store_true")
    parser.add_argument("--skip-copy-videos-to-mac", action="store_true")
    parser.add_argument("--mac-video-dest", default=None)
    args = parser.parse_args()

    summary = run_postprocess(
        args.run_dir,
        download_fullres=not args.skip_download_fullres,
        smooth_exposure=not args.skip_smoothing,
        render=not args.skip_render,
        queue_instagram=not args.skip_instagram_queue,
        publish_instagram=not args.skip_instagram_publish,
        copy_videos_to_mac=not args.skip_copy_videos_to_mac,
        mac_video_dest=args.mac_video_dest,
    )
    _print_stdout(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
