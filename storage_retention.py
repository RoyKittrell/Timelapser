#!/usr/bin/env python3
"""Recover drive space from verified runs while preserving their records."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

INTERMEDIATE_DIRS = (
    "frames_smoothed_luma_4k", "frames_smoothed_luma_4k_test",
    "frames_smoothed_luma_v2", "_render_stage",
)
SOURCE_DIRS = ("frames_full_jpeg", "frames_jpeg")
RECORD_SUFFIXES = {".json", ".jsonl", ".csv", ".log", ".txt"}


def verified(run: Path) -> bool:
    receipt = run / "instagram_publish_receipt.json"
    if receipt.is_file():
        try:
            if json.loads(receipt.read_text()).get("status") == "complete":
                return True
        except (OSError, ValueError, TypeError):
            pass
    summary = run / "postprocess_workflow_summary.json"
    try:
        complete = json.loads(summary.read_text()).get("status") == "complete"
    except (OSError, ValueError, TypeError):
        return False
    return complete and any(run.glob("*_final_instagram-reel_clean.mp4"))


def size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def candidates(root: Path) -> list[Path]:
    runs = [run for run in root.iterdir() if run.is_dir() and verified(run)]
    return sorted(runs, key=lambda path: path.stat().st_mtime)


def removable_tiers(run: Path):
    yield [run / name for name in INTERMEDIATE_DIRS]
    yield list(run.glob("_overlay_stage_*")) + list(run.glob("*_1080-backup.mp4"))
    # Carousel-ready overlay videos are redundant once the 4:5 copies exist.
    if all((run / f"{run.name}_final_instagram-carousel_{kind}.mp4").is_file()
           for kind in ("brightness", "director")):
        yield [run / f"{run.name}_final_instagram-reel_{kind}.mp4"
               for kind in ("brightness", "director")]
    yield [run / name for name in SOURCE_DIRS]


def prune(root: Path, *, min_free_gb: float, target_free_gb: float, dry_run: bool = False,
          free_bytes: int | None = None) -> list[dict]:
    free = shutil.disk_usage(root).free if free_bytes is None else free_bytes
    minimum = int(min_free_gb * 1024**3)
    target = int(target_free_gb * 1024**3)
    if free >= minimum:
        return []
    removed: list[dict] = []
    runs = candidates(root)
    for tier in range(4):
        for run in runs:
            tier_paths = list(removable_tiers(run))[tier]
            for path in tier_paths:
                if not path.exists() or path.suffix in RECORD_SUFFIXES:
                    continue
                reclaimed = size(path)
                removed.append({"path": str(path), "bytes": reclaimed, "tier": tier + 1})
                if not dry_run:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                free += reclaimed
                if free >= target:
                    return removed
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--min-free-gb", type=float, default=180)
    parser.add_argument("--target-free-gb", type=float, default=240)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.target_free_gb < args.min_free_gb:
        parser.error("target free space must be at least the minimum")
    actions = prune(args.root, min_free_gb=args.min_free_gb,
                    target_free_gb=args.target_free_gb, dry_run=args.dry_run)
    print(json.dumps({"removed": actions, "dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
