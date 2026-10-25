#!/usr/bin/env python3
"""Post-run workflow for wired Timelapser V6."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import timelapser_postprocess_v5 as legacy
import v6_config


def run_postprocess(
    run_dir: Path,
    *,
    publish_instagram: bool = True,
    instagram_reel_only: bool = False,
) -> dict:
    run_dir = run_dir.expanduser().resolve()
    root = Path(__file__).resolve().parent
    log_path = run_dir / "postprocess_workflow.log"
    python = legacy._python_executable()
    legacy._write_live_status(
        log_path,
        status="running",
        current_step="import_wired_v6_jpegs_via_mega4_usb",
        completed_steps=[],
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        publish_instagram=bool(publish_instagram),
        instagram_reel_only=bool(instagram_reel_only),
        elapsed_seconds=0,
        ended_at="",
        error="",
    )
    import_step = legacy._run_step(
        log_path,
        "import_wired_v6_jpegs_via_mega4_usb",
        [python, str(root / "mega4_usb_import.py"), str(run_dir), "--v6"],
        root,
    )
    if import_step["returncode"] != 0:
        result = {"status": "failed_v6_usb_import", "steps": [import_step]}
        (run_dir / "postprocess_workflow_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        legacy._write_live_status(log_path, status=result["status"], current_step="")
        return result

    learning_step = legacy._run_step(
        log_path,
        "update_v6_exposure_model",
        [python, str(root / "v6_exposure_learning.py"), str(run_dir.parent)],
        root,
    )

    result = legacy.run_postprocess(
        run_dir,
        download_fullres=False,
        smooth_exposure=True,
        render=True,
        # A non-publishing test must not leave a latent queue item that an
        # automatic publisher could upload later.
        queue_instagram=publish_instagram,
        publish_instagram=publish_instagram,
        instagram_reel_only=instagram_reel_only,
        copy_videos_to_mac=v6_config.COPY_VIDEOS_TO_MAC,
        initial_completed_steps=(import_step["name"], learning_step["name"]),
    )
    result.setdefault("steps", []).insert(0, import_step)
    result["steps"].insert(1, learning_step)
    if result.get("status") == "complete":
        cleanup_step = legacy._run_step(
            log_path,
            "erase_camera_sd_after_success",
            [python, str(root / "erase_camera_sd_after_success.py"), str(run_dir)],
            root,
        )
        result["steps"].append(cleanup_step)
        if cleanup_step["returncode"] != 0:
            result["status"] = "failed_camera_sd_cleanup"
    result["version"] = "6.0"
    result["graphs_released"] = result.get("status") == "complete"
    (run_dir / "postprocess_workflow_summary.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    legacy._write_live_status(
        log_path,
        status=result["status"],
        current_step="",
        elapsed_seconds=result.get("elapsed_seconds", 0),
        ended_at=result.get("ended_at", ""),
        error=result.get("error", ""),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete Timelapser V6 post-capture workflow")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-instagram-publish", action="store_true")
    parser.add_argument("--instagram-reel-only", action="store_true")
    args = parser.parse_args()
    deferred = {}
    deferred_path = args.run_dir.expanduser().resolve() / "postprocess_deferred.json"
    try:
        deferred = json.loads(deferred_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    reel_only = bool(args.instagram_reel_only or deferred.get("instagram_reel_only", False))
    result = run_postprocess(
        args.run_dir,
        publish_instagram=not args.skip_instagram_publish,
        instagram_reel_only=reel_only,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
