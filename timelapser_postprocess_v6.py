#!/usr/bin/env python3
"""Post-run workflow for wired Timelapser V6."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import timelapser_postprocess_v5 as legacy


def run_postprocess(run_dir: Path, *, publish_instagram: bool = True) -> dict:
    run_dir = run_dir.expanduser().resolve()
    root = Path(__file__).resolve().parent
    log_path = run_dir / "postprocess_workflow.log"
    python = legacy._python_executable()
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

    result = legacy.run_postprocess(
        run_dir,
        download_fullres=False,
        smooth_exposure=True,
        render=True,
        queue_instagram=True,
        publish_instagram=publish_instagram,
        copy_videos_to_mac=True,
    )
    result.setdefault("steps", []).insert(0, import_step)
    result["version"] = "6.0"
    result["graphs_released"] = result.get("status") == "complete"
    (run_dir / "postprocess_workflow_summary.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete Timelapser V6 post-capture workflow")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-instagram-publish", action="store_true")
    args = parser.parse_args()
    result = run_postprocess(args.run_dir, publish_instagram=not args.skip_instagram_publish)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
