#!/usr/bin/env python3
"""Render the 4K clean Reel and high-quality 1080p overlay masters."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    common = [
        sys.executable, str(root / "render_timelapse.py"), str(args.run_dir),
        "--source", str(args.source), "--output-stem-suffix", "_final",
        "--output-profile-name", "instagram-reel", "--brightness-source", "frames",
    ]
    if args.overwrite:
        common.append("--overwrite")
    commands = [
        common + ["--profile", "portrait4k", "--outputs", "clean"],
        common + ["--profile", "instagram-reel-hq", "--outputs", "brightness", "director"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
