#!/usr/bin/env python3
"""Create Instagram carousel copies while preserving full 9:16 masters."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


CAROUSEL_WIDTH = 1080
CAROUSEL_HEIGHT = 1350
KINDS = ("clean", "director", "brightness")


def probe(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,r_frame_rate",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def find_reel(run_dir: Path, kind: str) -> Path:
    exact = run_dir / f"{run_dir.name}_final_instagram-reel_{kind}.mp4"
    if exact.exists():
        return exact
    matches = sorted(run_dir.glob(f"*_instagram-reel_{kind}.mp4"))
    if not matches:
        raise FileNotFoundError(f"No 9:16 {kind} video found in {run_dir}")
    return matches[-1]


def carousel_path(source: Path) -> Path:
    marker = "_instagram-reel_"
    if marker not in source.name:
        raise ValueError(f"Unexpected Reel filename: {source.name}")
    return source.with_name(source.name.replace(marker, "_instagram-carousel_", 1))


def create_carousel_copy(source: Path, output: Path, *, overwrite: bool) -> dict[str, Any]:
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output exists; use --overwrite: {output}")
    filter_graph = (
        f"scale={CAROUSEL_WIDTH}:{CAROUSEL_HEIGHT}:"
        "force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={CAROUSEL_WIDTH}:{CAROUSEL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-stats",
        "-y" if overwrite else "-n",
        "-i",
        str(source),
        "-vf",
        filter_graph,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-profile:v",
        "high",
        "-level",
        "4.2",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output),
    ]
    subprocess.run(cmd, check=True)
    details = probe(output)
    stream = (details.get("streams") or [{}])[0]
    if (stream.get("width"), stream.get("height")) != (CAROUSEL_WIDTH, CAROUSEL_HEIGHT):
        raise RuntimeError(f"Unexpected carousel dimensions for {output}: {stream}")
    return {
        "source": str(source),
        "output": str(output),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "bytes": output.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit complete 9:16 Timelapser videos inside black 4:5 carousel canvases."
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--outputs", nargs="+", choices=KINDS, default=list(KINDS))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise SystemExit("ffmpeg and ffprobe are required")
    run_dir = args.run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"Run directory does not exist: {run_dir}")

    results = []
    for kind in args.outputs:
        source = find_reel(run_dir, kind)
        output = carousel_path(source)
        print(f"Creating {kind} carousel copy: {output.name}", flush=True)
        results.append(create_carousel_copy(source, output, overwrite=args.overwrite))

    summary_path = run_dir / "instagram_formats_summary.json"
    summary_path.write_text(json.dumps({"status": "complete", "outputs": results}, indent=2), encoding="utf-8")
    print(json.dumps({"status": "complete", "outputs": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
