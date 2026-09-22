#!/usr/bin/env python3
"""
V6 beta post-run importer for Olympus USB Storage mode.

This does not control the camera and does not use PTP. It treats the mounted
camera SD card as a read-only source, maps each V5 telemetry frame to the exact
Olympus JPEG on the card, verifies EXIF/file timestamps to avoid reused-name
mistakes, and copies masters into <RUN_DIR>/frames_full_jpeg/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
except Exception as exc:
    raise SystemExit(
        "Pillow is required for EXIF timestamp checks. Run from the timelapser venv.\n"
        f"Import error: {type(exc).__name__}: {exc}"
    )

V5_ROOT = Path("/home/roy/Timelapser Sept2026/timelapser_v5")
DEFAULT_MOUNT_ROOTS = [Path("/media"), Path("/mnt"), Path("/run/media")]
DEFAULT_OUTPUT_DIRNAME = "frames_full_jpeg"
MANIFEST_NAME = "usb_fullres_copy_manifest.csv"
SUMMARY_PREFIX = "v6_beta_usb_import_summary_"
MIN_JPEG_BYTES = 50_000
DEFAULT_TIME_TOLERANCE_SECONDS = 10 * 60
JPEG_EXTENSIONS = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    name: str
    size: int
    mtime: float
    exif_datetime: datetime | None


@dataclass(frozen=True)
class ExpectedFrame:
    frame: int
    remote_jpg: str
    telemetry_time: datetime | None
    local_name: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V6 beta: import full-res JPEGs from Olympus USB Storage into V5 run folders."
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("run", nargs="?", help="Specific V5 run directory or run folder name.")
    group.add_argument("--latest", action="store_true", help="Import the newest V5 run with telemetry.")
    group.add_argument("--all", action="store_true", help="Scan all V5 runs and import those with exact SD matches.")
    p.add_argument("--sd-root", type=Path, default=None, help="Mounted SD root, e.g. /media/roy/5B48-8907.")
    p.add_argument("--output-dirname", default=DEFAULT_OUTPUT_DIRNAME)
    p.add_argument("--dry-run", action="store_true", help="Report mappings without copying or deleting.")
    p.add_argument("--replace-thumbnails", action="store_true", help="Delete frames_jpeg/*.jpg after a complete verified import.")
    p.add_argument("--delete-imported-from-sd", action="store_true", help="Remove only verified, imported camera JPEGs after a complete run import.")
    p.add_argument("--force", action="store_true", help="Overwrite existing full JPEGs even when size matches.")
    p.add_argument("--time-tolerance-seconds", type=float, default=DEFAULT_TIME_TOLERANCE_SECONDS)
    p.add_argument(
        "--allow-time-mismatch",
        action="store_true",
        help="Dangerous: match by basename even if EXIF time is missing/mismatched.",
    )
    p.add_argument("--summary", type=Path, default=None, help="Optional JSON summary output path.")
    return p.parse_args()


def run_cmd(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False).stdout
    except Exception:
        return ""


def exif_datetime(path: Path) -> datetime | None:
    try:
        raw = Image.open(path).getexif().get(306)
        if not raw:
            return None
        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def detect_sd_root() -> Path:
    candidates: list[Path] = []
    for root in DEFAULT_MOUNT_ROOTS:
        if not root.exists():
            continue
        for dcim in root.rglob("DCIM"):
            if dcim.is_dir() and any(dcim.glob("*OLYMP")):
                candidates.append(dcim.parent)
    if not candidates:
        raise FileNotFoundError("No mounted Olympus-style SD root found under /media, /mnt, or /run/media.")

    scored = []
    lsblk = run_cmd(["lsblk", "-o", "NAME,MODEL,MOUNTPOINTS"])
    for candidate in candidates:
        count = sum(1 for _ in candidate.glob("DCIM/*OLYMP/*"))
        model_bonus = 1 if str(candidate) in lsblk and "E-M5MarkIII" in lsblk else 0
        scored.append((model_bonus, count, candidate))
    scored.sort(reverse=True)
    return scored[0][2]


def find_dcim_dirs(sd_root: Path) -> list[Path]:
    dcim = sd_root / "DCIM"
    if not dcim.exists():
        raise FileNotFoundError(f"No DCIM directory found under {sd_root}")
    dirs = [p for p in dcim.iterdir() if p.is_dir()]
    if not dirs:
        raise FileNotFoundError(f"No camera folders found under {dcim}")
    return sorted(dirs)


def index_sd(sd_root: Path) -> dict[str, list[SourceFile]]:
    index: dict[str, list[SourceFile]] = {}
    for folder in find_dcim_dirs(sd_root):
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in JPEG_EXTENSIONS:
                continue
            st = path.stat()
            src = SourceFile(
                path=path,
                name=path.name.upper(),
                size=st.st_size,
                mtime=st.st_mtime,
                exif_datetime=exif_datetime(path),
            )
            index.setdefault(src.name, []).append(src)
    return index


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except Exception:
        return None


def expected_frames(run_dir: Path) -> list[ExpectedFrame]:
    telemetry = run_dir / "telemetry.csv"
    if not telemetry.exists():
        raise FileNotFoundError(f"Missing telemetry.csv: {telemetry}")
    rows: list[ExpectedFrame] = []
    with telemetry.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            remote = (row.get("remote_jpg") or "").strip()
            frame = (row.get("frame") or "").strip()
            if not remote or not frame:
                continue
            n = int(float(frame))
            rows.append(
                ExpectedFrame(
                    frame=n,
                    remote_jpg=Path(remote).name.upper(),
                    telemetry_time=parse_time((row.get("time") or "").strip()),
                    local_name=f"frame_{n:06d}.jpg",
                )
            )
    return rows


def candidate_delta(src: SourceFile, exp: ExpectedFrame) -> float | None:
    if src.exif_datetime is None or exp.telemetry_time is None:
        return None
    return abs((src.exif_datetime - exp.telemetry_time).total_seconds())


def choose_source(
    exp: ExpectedFrame,
    matches: list[SourceFile],
    tolerance: float,
    allow_time_mismatch: bool,
) -> tuple[SourceFile | None, str, float | None]:
    if not matches:
        return None, "missing_on_sd", None
    valid_time = [(candidate_delta(src, exp), src) for src in matches]
    valid_time = [(delta, src) for delta, src in valid_time if delta is not None]
    if valid_time:
        valid_time.sort(key=lambda item: item[0])
        delta, src = valid_time[0]
        if delta <= tolerance:
            return src, "matched", delta
        if allow_time_mismatch:
            return src, "matched_time_mismatch_allowed", delta
        return None, "timestamp_mismatch", delta
    if len(matches) == 1 and allow_time_mismatch:
        return matches[0], "matched_no_timestamp_allowed", None
    return None, "timestamp_missing", None


def validate_jpeg(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size < MIN_JPEG_BYTES:
        return False, f"too small: {size}"
    with path.open("rb") as f:
        head = f.read(2)
        try:
            f.seek(-64, os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read()
    if head != b"\xff\xd8":
        return False, "missing JPEG SOI"
    if b"\xff\xd9" not in tail:
        return False, "missing JPEG EOI near EOF"
    return True, ""


def copy_atomic(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        part.unlink()
    with src.open("rb") as r, part.open("wb") as w:
        shutil.copyfileobj(r, w, length=1024 * 1024)
        w.flush()
        os.fsync(w.fileno())
    os.replace(part, dest)
    shutil.copystat(src, dest, follow_symlinks=True)


def resolve_run(path_or_name: str) -> Path:
    path = Path(path_or_name)
    if path.exists():
        return path.resolve()
    path = V5_ROOT / path_or_name
    if path.exists():
        return path.resolve()
    raise FileNotFoundError(f"Run directory not found: {path_or_name}")


def all_runs() -> list[Path]:
    runs = []
    for path in V5_ROOT.glob("*_v5_wifi"):
        if path.is_dir() and (path / "telemetry.csv").exists():
            runs.append(path.resolve())
    return sorted(runs, key=lambda path: path.stat().st_mtime, reverse=True)


def latest_run() -> Path:
    runs = all_runs()
    if not runs:
        raise FileNotFoundError(f"No V5 run directories with telemetry found under {V5_ROOT}")
    return runs[0]


def import_run(run_dir: Path, sd_index: dict[str, list[SourceFile]], args: argparse.Namespace) -> dict:
    expected = expected_frames(run_dir)
    out_dir = run_dir / args.output_dirname
    manifest_rows = []
    failures = []
    copied = skipped = 0
    bytes_total = 0

    for exp in expected:
        src, status, delta = choose_source(
            exp,
            sd_index.get(exp.remote_jpg, []),
            float(args.time_tolerance_seconds),
            bool(args.allow_time_mismatch),
        )
        dest = out_dir / exp.local_name
        row = {
            "frame": exp.frame,
            "remote_jpg": exp.remote_jpg,
            "source_path": str(src.path) if src else "",
            "dest_file": exp.local_name,
            "status": status,
            "bytes": src.size if src else 0,
            "sd_datetime": src.exif_datetime.isoformat(sep=" ") if src and src.exif_datetime else "",
            "telemetry_time": exp.telemetry_time.isoformat(sep=" ") if exp.telemetry_time else "",
            "telemetry_delta_seconds": f"{delta:.3f}" if delta is not None else "",
        }
        if src is None:
            failures.append(row)
            manifest_rows.append(row)
            continue

        bytes_total += src.size
        if args.dry_run:
            row["status"] = "dry_run_" + status
        else:
            if dest.exists() and not args.force and dest.stat().st_size == src.size:
                ok, reason = validate_jpeg(dest)
                if ok:
                    skipped += 1
                    row["status"] = "skipped_existing"
                else:
                    copy_atomic(src.path, dest)
                    copied += 1
                    row["status"] = "replaced_invalid_existing"
            else:
                copy_atomic(src.path, dest)
                copied += 1
                row["status"] = "copied"
            ok, reason = validate_jpeg(dest)
            if not ok:
                row["status"] = "copied_but_invalid"
                row["error"] = reason
                failures.append(row)
        manifest_rows.append(row)

    complete = len(failures) == 0 and len(expected) > 0
    deleted_thumbnails = 0
    deleted_sd_jpegs = 0
    if complete and args.delete_imported_from_sd and not args.dry_run:
        sources = {row["source_path"]: row for row in manifest_rows}
        for source_path, row in sources.items():
            dest = out_dir / row["dest_file"]
            ok, reason = validate_jpeg(dest)
            if not ok or dest.stat().st_size != row["bytes"]:
                raise RuntimeError(f"Refusing SD cleanup: {dest} failed verification ({reason})")
        for source_path in sources:
            Path(source_path).unlink()
            deleted_sd_jpegs += 1
    if complete and args.replace_thumbnails and not args.dry_run:
        thumb_dir = run_dir / "frames_jpeg"
        if thumb_dir.exists():
            for path in thumb_dir.glob("*.jpg"):
                path.unlink()
                deleted_thumbnails += 1

    manifest_path = out_dir / MANIFEST_NAME
    if not args.dry_run:
        out_dir.mkdir(exist_ok=True)
        fields = [
            "frame",
            "remote_jpg",
            "source_path",
            "dest_file",
            "status",
            "bytes",
            "sd_datetime",
            "telemetry_time",
            "telemetry_delta_seconds",
            "error",
        ]
        with manifest_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in manifest_rows:
                writer.writerow({key: row.get(key, "") for key in fields})

    return {
        "run": run_dir.name,
        "run_dir": str(run_dir),
        "expected_frames": len(expected),
        "complete": complete,
        "copied": copied,
        "skipped_existing": skipped,
        "failures": len(failures),
        "first_failures": failures[:5],
        "bytes": bytes_total,
        "gb": round(bytes_total / 1024 / 1024 / 1024, 3),
        "output_dir": str(out_dir),
        "manifest": str(manifest_path),
        "deleted_thumbnails": deleted_thumbnails,
        "deleted_sd_jpegs": deleted_sd_jpegs,
    }


def main() -> int:
    args = parse_args()
    sd_root = args.sd_root or detect_sd_root()
    print(f"Olympus USB Storage root: {sd_root}", flush=True)
    sd_index = index_sd(sd_root)
    total_sources = sum(len(items) for items in sd_index.values())
    print(f"Indexed {total_sources} JPEGs from SD card.", flush=True)

    if args.all:
        runs = all_runs()
    elif args.latest:
        runs = [latest_run()]
    else:
        runs = [resolve_run(args.run)]

    summary = {
        "tool": "timelapser_v6_beta_usb_import.py",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sd_root": str(sd_root),
        "dry_run": bool(args.dry_run),
        "replace_thumbnails": bool(args.replace_thumbnails),
        "time_tolerance_seconds": float(args.time_tolerance_seconds),
        "allow_time_mismatch": bool(args.allow_time_mismatch),
        "runs": [],
    }
    for run in runs:
        result = import_run(run, sd_index, args)
        summary["runs"].append(result)
        status = "OK" if result["complete"] else "SKIP/FAIL"
        print(
            f"{status} {result['run']}: expected={result['expected_frames']} "
            f"copied={result['copied']} skipped={result['skipped_existing']} "
            f"failures={result['failures']} output={result['output_dir']}",
            flush=True,
        )

    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    out = args.summary
    if out is None and not args.dry_run:
        out = V5_ROOT / f"{SUMMARY_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    if out is not None:
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Summary: {out}", flush=True)

    incomplete = [run for run in summary["runs"] if not run["complete"]]
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
