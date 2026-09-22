#!/usr/bin/env python3
"""Remove old run images only when their rendered videos are complete."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path


IMAGE_DIRS = ("frames_jpeg", "frames_full_jpeg", "frames_smoothed_luma_v2")
RUN_DATE = re.compile(r"^(\d{8})_\d{6}_")


def eligible(run_dir: Path, cutoff: date) -> bool:
    match = RUN_DATE.match(run_dir.name)
    if not match or date.fromisoformat(f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:]}") >= cutoff:
        return False
    summary_path = run_dir / "render_summary_v3.json"
    if not summary_path.is_file():
        return False
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        outputs = summary["outputs"]
        return (summary.get("status") == "complete" and len(outputs) >= 3 and
                {item.get("kind") for item in outputs} >= {"clean", "brightness", "director"} and
                all(item.get("returncode") == 0 and
                    (run_dir / Path(item["output"]).name).is_file() and
                    (run_dir / Path(item["output"]).name).stat().st_size > 0
                    for item in outputs))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prune(root: Path, days: int, *, dry_run: bool = False, today: date | None = None) -> list[Path]:
    cutoff = (today or date.today()) - timedelta(days=days)
    removed = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir() or not eligible(run_dir, cutoff):
            continue
        for name in IMAGE_DIRS:
            path = run_dir / name
            if path.is_dir() and not path.is_symlink():
                if not dry_run:
                    shutil.rmtree(path)
                removed.append(path)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Directory containing timelapse run folders")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    for path in prune(args.root, args.days, dry_run=args.dry_run):
        print(f"{'WOULD REMOVE' if args.dry_run else 'REMOVED'} {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
