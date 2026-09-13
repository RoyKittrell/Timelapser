#!/usr/bin/env python3
"""
download_run_fullres.py
=======================

Robust post-run full-resolution JPEG recovery for Timelapser V5.x.

The active timelapse deliberately downloads only Olympus thumbnails.  This
utility runs AFTER photography has stopped and uses telemetry.csv as the
authoritative frame -> camera JPEG manifest.

Design goals
------------
* Never guess filenames: use the exact remote_jpg recorded for each frame.
* Resume safely after interruption or reboot.
* Download to .part, validate, then atomically rename.
* Retry transient Olympus Wi-Fi failures and recreate the camera session.
* Make multiple repair passes over anything that failed.
* Verify frame continuity, SD-card presence, local JPEG integrity and counts.
* Never delete or modify anything on the camera SD card.
* Produce a durable CSV manifest, JSON summary and human-readable log.

Typical use
-----------
    source /home/roy/timelapser-venv/bin/activate

    python3 download_run_fullres.py \
        "/home/roy/Timelapser Sept2026/timelapser_v5/20260910_110739_sunset_v5_wifi"

Or automatically choose the newest V5 run:
    python3 download_run_fullres.py --latest

The finished masters are written to:
    <RUN_DIR>/frames_full_jpeg/frame_000001.jpg
    <RUN_DIR>/frames_full_jpeg/frame_000002.jpg
    ...

The exact Olympus filename for every local frame remains recorded in:
    <RUN_DIR>/fullres_manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from olympuswifi.camera import OlympusCamera
except Exception as exc:
    raise SystemExit(
        "Could not import olympuswifi. Activate the timelapser venv first.\n"
        f"Import error: {type(exc).__name__}: {exc}"
    )


DEFAULT_V5_ROOT = Path("/home/roy/Timelapser Sept2026/timelapser_v5")
DEFAULT_OUTPUT_DIRNAME = "frames_full_jpeg"

# A real E-M1/E-M5 full JPEG can be many MB.  This is deliberately low enough
# not to reject unusually small but valid images while still catching nonsense.
MIN_REASONABLE_JPEG_BYTES = 50_000

MANIFEST_FIELDS = [
    "frame",
    "remote_jpg",
    "local_file",
    "status",
    "bytes",
    "sha256",
    "attempts",
    "last_error",
    "verified_at",
]


@dataclass(frozen=True)
class ExpectedFrame:
    frame: int
    remote_jpg: str
    local_name: str


class DownloadTimeout(TimeoutError):
    pass


@contextmanager
def hard_timeout(seconds: float):
    """Linux/Pi hard timeout around a possibly wedged olympuswifi HTTP call."""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(signum, frame):
        raise DownloadTimeout(f"camera operation exceeded {seconds:.0f}s hard timeout")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


class Log:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str):
        stamp = datetime.now().isoformat(timespec="seconds")
        line = f"{stamp}  {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class OlympusDownloadSession:
    """One disposable olympuswifi session.

    Recreating this object is our recovery mechanism after transport failures.
    """

    def __init__(self, log: Log, op_timeout: float):
        self.log = log
        self.op_timeout = op_timeout
        self.cam: Optional[OlympusCamera] = None

    def connect(self):
        self.close()
        self.log.write("Connecting to Olympus Wi-Fi API...")
        with hard_timeout(self.op_timeout):
            self.cam = OlympusCamera()
            self.cam.send_command("switch_cammode", mode="play")
        self.log.write("Olympus session ready in PLAY mode.")

    def close(self):
        self.cam = None

    def reset(self, reason: str, delay: float):
        self.log.write(f"Resetting Olympus session: {reason}")
        self.close()
        if delay > 0:
            time.sleep(delay)
        self.connect()

    def list_images(self):
        if self.cam is None:
            self.connect()
        with hard_timeout(self.op_timeout):
            return self.cam.list_images()

    def download(self, remote_jpg: str) -> bytes:
        if self.cam is None:
            self.connect()
        with hard_timeout(self.op_timeout):
            data = self.cam.download_image(remote_jpg)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"download_image({remote_jpg!r}) returned {type(data).__name__}, not bytes"
            )
        return bytes(data)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_jpeg_bytes(data: bytes) -> tuple[bool, str]:
    if len(data) < MIN_REASONABLE_JPEG_BYTES:
        return False, f"too small ({len(data)} bytes)"
    if not data.startswith(b"\xff\xd8"):
        return False, "missing JPEG SOI marker"
    # JPEGs may contain a few padding bytes after EOI, so search near the tail.
    if b"\xff\xd9" not in data[-64:]:
        return False, "missing JPEG EOI marker near end of file"
    return True, ""


def validate_jpeg_file(path: Path, expected_bytes: int | None = None,
                       expected_sha256: str | None = None) -> tuple[bool, str]:
    if not path.exists():
        return False, "file does not exist"
    size = path.stat().st_size
    if size < MIN_REASONABLE_JPEG_BYTES:
        return False, f"too small ({size} bytes)"
    if expected_bytes and size != expected_bytes:
        return False, f"size mismatch: local={size}, manifest={expected_bytes}"

    with path.open("rb") as f:
        head = f.read(2)
        try:
            f.seek(-64, os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read()

    if head != b"\xff\xd8":
        return False, "missing JPEG SOI marker"
    if b"\xff\xd9" not in tail:
        return False, "missing JPEG EOI marker near EOF"

    if expected_sha256:
        actual = sha256_file(path)
        if actual != expected_sha256:
            return False, "SHA-256 mismatch"

    return True, ""


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    try:
        if part.exists():
            part.unlink()
        with part.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


def read_expected_frames(run_dir: Path) -> list[ExpectedFrame]:
    telemetry = run_dir / "telemetry.csv"
    if not telemetry.exists():
        raise FileNotFoundError(f"Missing telemetry.csv: {telemetry}")

    rows: list[ExpectedFrame] = []
    with telemetry.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"frame", "remote_jpg"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"telemetry.csv is missing required columns: {sorted(missing)}"
            )

        for row in reader:
            frame_raw = str(row.get("frame", "")).strip()
            remote = str(row.get("remote_jpg", "")).strip()
            if not frame_raw or not remote:
                continue
            frame = int(float(frame_raw))
            rows.append(
                ExpectedFrame(
                    frame=frame,
                    remote_jpg=remote,
                    local_name=f"frame_{frame:06d}.jpg",
                )
            )

    if not rows:
        raise ValueError("telemetry.csv contains no production frame mappings.")

    rows.sort(key=lambda x: x.frame)

    frames = [x.frame for x in rows]
    duplicates = sorted({x for x in frames if frames.count(x) > 1})
    if duplicates:
        raise ValueError(f"Duplicate frame numbers in telemetry: {duplicates[:20]}")

    expected_numbers = list(range(frames[0], frames[-1] + 1))
    if frames != expected_numbers:
        missing = sorted(set(expected_numbers) - set(frames))
        raise ValueError(
            "Telemetry frame numbers are not contiguous. "
            f"Missing frame numbers: {missing[:30]}"
        )

    remotes = [x.remote_jpg for x in rows]
    if len(remotes) != len(set(remotes)):
        raise ValueError("Duplicate remote_jpg paths detected in telemetry.")

    return rows


def read_run_summary(run_dir: Path) -> dict:
    path = run_dir / "run_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_manifest(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    out = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[int(row["frame"])] = row
            except Exception:
                continue
    return out


def write_manifest(path: Path, expected: list[ExpectedFrame], records: dict[int, dict]):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for item in expected:
            r = records.get(item.frame, {})
            writer.writerow({
                "frame": item.frame,
                "remote_jpg": item.remote_jpg,
                "local_file": item.local_name,
                "status": r.get("status", "pending"),
                "bytes": r.get("bytes", ""),
                "sha256": r.get("sha256", ""),
                "attempts": r.get("attempts", 0),
                "last_error": r.get("last_error", ""),
                "verified_at": r.get("verified_at", ""),
            })
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def choose_latest_run(root: Path) -> Path:
    candidates = [
        p for p in root.iterdir()
        if p.is_dir() and (p / "telemetry.csv").exists()
    ]
    if not candidates:
        raise FileNotFoundError(f"No V5 run directories found under {root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def backoff_seconds(attempt: int, base: float) -> float:
    # Bounded exponential backoff.
    return min(30.0, base * (2 ** max(0, attempt - 1)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Robustly download full-resolution Olympus JPEGs for one V5 timelapse run."
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("run_dir", nargs="?", help="V5 run directory containing telemetry.csv")
    group.add_argument("--latest", action="store_true", help="Use newest run under --root")

    parser.add_argument("--root", type=Path, default=DEFAULT_V5_ROOT)
    parser.add_argument("--output-dirname", default=DEFAULT_OUTPUT_DIRNAME)
    parser.add_argument("--attempts-per-frame", type=int, default=4)
    parser.add_argument("--repair-passes", type=int, default=5)
    parser.add_argument("--operation-timeout", type=float, default=120.0,
                        help="Hard timeout in seconds for one Olympus HTTP operation")
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--inventory-timeout", type=float, default=180.0)
    parser.add_argument("--skip-inventory", action="store_true",
                        help="Skip SD inventory check and try exact telemetry paths directly")
    parser.add_argument("--accept-valid-existing", action="store_true",
                        help="Trust valid pre-existing JPEGs even when no manifest hash exists")
    parser.add_argument("--force-redownload", action="store_true")
    args = parser.parse_args()

    if args.latest:
        run_dir = choose_latest_run(args.root)
    elif args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
    else:
        parser.error("Specify RUN_DIR or --latest")

    if not run_dir.is_dir():
        raise SystemExit(f"Run directory does not exist: {run_dir}")

    out_dir = run_dir / args.output_dirname
    manifest_path = run_dir / "fullres_manifest.csv"
    summary_path = run_dir / "fullres_download_summary.json"
    log = Log(run_dir / "fullres_download.log")

    log.write("=" * 72)
    log.write("TIMELAPSER FULL-RES JPEG RECOVERY")
    log.write(f"Run directory: {run_dir}")
    log.write(f"Output directory: {out_dir}")
    log.write("=" * 72)

    expected = read_expected_frames(run_dir)
    summary = read_run_summary(run_dir)
    first = expected[0]
    last = expected[-1]

    log.write(
        f"Telemetry expects {len(expected)} frames: "
        f"{first.frame:06d}={Path(first.remote_jpg).name} -> "
        f"{last.frame:06d}={Path(last.remote_jpg).name}"
    )

    # Cross-check run_summary, but telemetry is authoritative for exact mappings.
    if summary:
        reported = summary.get("downloaded_frames")
        if reported is not None and int(reported) != len(expected):
            raise SystemExit(
                f"REFUSING TO START: run_summary says {reported} downloaded frames "
                f"but telemetry contains {len(expected)} production mappings."
            )
        last_summary = summary.get("last_remote_jpg")
        if last_summary and last_summary != last.remote_jpg:
            raise SystemExit(
                "REFUSING TO START: last_remote_jpg disagrees between run_summary "
                f"({last_summary}) and telemetry ({last.remote_jpg})."
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    records = load_manifest(manifest_path)

    # Ensure any old manifest still maps frames to the same camera filenames.
    for item in expected:
        old = records.get(item.frame)
        if old and old.get("remote_jpg") and old["remote_jpg"] != item.remote_jpg:
            raise SystemExit(
                f"REFUSING TO START: existing manifest frame {item.frame} maps to "
                f"{old['remote_jpg']}, telemetry maps to {item.remote_jpg}."
            )

    session = OlympusDownloadSession(log, args.operation_timeout)
    session.connect()

    sd_names: set[str] | None = None
    missing_on_sd: list[ExpectedFrame] = []

    if not args.skip_inventory:
        log.write("Reading one authoritative SD-card inventory before transfer...")
        original_timeout = session.op_timeout
        session.op_timeout = args.inventory_timeout
        try:
            files = session.list_images()
            sd_names = {str(x.file_name) for x in files}
            log.write(f"SD inventory returned {len(sd_names)} files.")
            missing_on_sd = [x for x in expected if x.remote_jpg not in sd_names]
            if missing_on_sd:
                log.write(
                    f"WARNING: {len(missing_on_sd)} expected JPEG(s) were not visible "
                    "in the initial SD inventory. Direct download will still be attempted."
                )
                for item in missing_on_sd[:20]:
                    log.write(
                        f"  SD inventory missing frame {item.frame:06d}: {item.remote_jpg}"
                    )
            else:
                log.write("All telemetry JPEG paths are present in the SD inventory.")
        except Exception as exc:
            log.write(
                f"WARNING: SD inventory failed ({type(exc).__name__}: {exc}). "
                "Continuing with exact telemetry paths; inventory is not required."
            )
            session.reset("inventory failure", args.retry_delay)
        finally:
            session.op_timeout = original_timeout

    # Reconcile already-complete files.
    complete: set[int] = set()
    for item in expected:
        local = out_dir / item.local_name
        old = records.get(item.frame, {})

        if args.force_redownload:
            continue

        expected_bytes = None
        expected_hash = None
        if old.get("status") == "complete":
            try:
                expected_bytes = int(old.get("bytes") or 0) or None
            except Exception:
                expected_bytes = None
            expected_hash = old.get("sha256") or None

        if expected_hash:
            ok, reason = validate_jpeg_file(local, expected_bytes, expected_hash)
            if ok:
                complete.add(item.frame)
                continue
            log.write(
                f"Existing frame {item.frame:06d} failed manifest verification: {reason}; "
                "will re-download."
            )
        elif args.accept_valid_existing and local.exists():
            ok, reason = validate_jpeg_file(local)
            if ok:
                digest = sha256_file(local)
                records[item.frame] = {
                    "status": "complete",
                    "bytes": local.stat().st_size,
                    "sha256": digest,
                    "attempts": int(old.get("attempts") or 0),
                    "last_error": "",
                    "verified_at": datetime.now().isoformat(timespec="seconds"),
                }
                complete.add(item.frame)
            else:
                log.write(
                    f"Existing unmanifested frame {item.frame:06d} is invalid ({reason}); "
                    "will re-download."
                )

    write_manifest(manifest_path, expected, records)
    if complete:
        log.write(f"Resume check: {len(complete)} frame(s) already verified complete.")

    def download_one(item: ExpectedFrame) -> bool:
        nonlocal session
        local = out_dir / item.local_name
        record = records.setdefault(item.frame, {})
        prior_attempts = int(record.get("attempts") or 0)

        for attempt in range(1, args.attempts_per_frame + 1):
            total_attempt = prior_attempts + attempt
            started = time.monotonic()
            try:
                log.write(
                    f"[{item.frame:06d}/{last.frame:06d}] "
                    f"Downloading {item.remote_jpg} -> {item.local_name} "
                    f"(attempt {attempt}/{args.attempts_per_frame})"
                )

                data = session.download(item.remote_jpg)
                ok, reason = validate_jpeg_bytes(data)
                if not ok:
                    raise IOError(f"downloaded JPEG failed validation: {reason}")

                digest = sha256_bytes(data)
                atomic_write(local, data)

                # Re-open the final file after atomic rename and verify it matches
                # exactly what was downloaded.
                ok, reason = validate_jpeg_file(
                    local, expected_bytes=len(data), expected_sha256=digest
                )
                if not ok:
                    raise IOError(f"post-write verification failed: {reason}")

                elapsed = time.monotonic() - started
                records[item.frame] = {
                    "status": "complete",
                    "bytes": len(data),
                    "sha256": digest,
                    "attempts": total_attempt,
                    "last_error": "",
                    "verified_at": datetime.now().isoformat(timespec="seconds"),
                }
                write_manifest(manifest_path, expected, records)
                log.write(
                    f"  VERIFIED frame {item.frame:06d}: "
                    f"{len(data)/1024/1024:.2f} MiB, SHA256 {digest[:12]}..., "
                    f"{elapsed:.1f}s"
                )
                return True

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                records[item.frame] = {
                    "status": "failed",
                    "bytes": "",
                    "sha256": "",
                    "attempts": total_attempt,
                    "last_error": error,
                    "verified_at": "",
                }
                write_manifest(manifest_path, expected, records)
                log.write(f"  FAILED frame {item.frame:06d}: {error}")

                # Throw away the client after ANY failed camera transfer. A
                # half-dead Olympus session is not worth trusting.
                try:
                    session.reset(
                        f"frame {item.frame:06d} transfer failure",
                        backoff_seconds(attempt, args.retry_delay),
                    )
                except Exception as reset_exc:
                    log.write(
                        f"  Session reconnect also failed: "
                        f"{type(reset_exc).__name__}: {reset_exc}"
                    )

        return False

    pending = [x for x in expected if x.frame not in complete]
    log.write(f"{len(pending)} frame(s) need full-resolution transfer.")

    interrupted = False
    try:
        for repair_pass in range(1, args.repair_passes + 1):
            if not pending:
                break

            log.write(
                f"--- Download pass {repair_pass}/{args.repair_passes}: "
                f"{len(pending)} pending frame(s) ---"
            )
            successes = 0
            still_pending: list[ExpectedFrame] = []

            for item in pending:
                if download_one(item):
                    complete.add(item.frame)
                    successes += 1
                else:
                    still_pending.append(item)

            pending = still_pending

            if pending:
                log.write(
                    f"Pass {repair_pass} complete: {successes} recovered, "
                    f"{len(pending)} still missing."
                )
                if successes == 0 and repair_pass < args.repair_passes:
                    log.write(
                        "No progress in this pass. Rebuilding session and waiting "
                        "before the next repair pass."
                    )
                    try:
                        session.reset("no-progress repair pass", 10.0)
                    except Exception as exc:
                        log.write(f"Reconnect failed: {type(exc).__name__}: {exc}")
                elif repair_pass < args.repair_passes:
                    time.sleep(args.retry_delay)

    except KeyboardInterrupt:
        interrupted = True
        log.write("Stopped by user. Progress is durable; rerun the same command to resume.")
    finally:
        session.close()

    # FINAL FULL-DIRECTORY VERIFICATION, independent of transfer-loop bookkeeping.
    log.write("Performing final local verification pass...")
    verified: list[ExpectedFrame] = []
    invalid: list[tuple[ExpectedFrame, str]] = []

    for item in expected:
        rec = records.get(item.frame, {})
        local = out_dir / item.local_name
        try:
            eb = int(rec.get("bytes") or 0) or None
        except Exception:
            eb = None
        eh = rec.get("sha256") or None
        ok, reason = validate_jpeg_file(local, eb, eh)
        if ok and rec.get("status") == "complete" and eh:
            verified.append(item)
        else:
            invalid.append((item, reason or "manifest not complete"))

    verified_numbers = {x.frame for x in verified}
    missing_final = [x for x in expected if x.frame not in verified_numbers]

    result = {
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "run_directory": str(run_dir),
        "output_directory": str(out_dir),
        "expected_frames": len(expected),
        "verified_frames": len(verified),
        "missing_or_invalid_frames": len(missing_final),
        "initial_inventory_missing": len(missing_on_sd),
        "interrupted": interrupted,
        "complete": len(missing_final) == 0,
        "first_frame": first.frame,
        "first_remote_jpg": first.remote_jpg,
        "last_frame": last.frame,
        "last_remote_jpg": last.remote_jpg,
        "missing_frames": [
            {
                "frame": x.frame,
                "remote_jpg": x.remote_jpg,
                "local_file": x.local_name,
                "last_error": records.get(x.frame, {}).get("last_error", ""),
            }
            for x in missing_final
        ],
    }

    tmp_summary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    tmp_summary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(tmp_summary, summary_path)
    write_manifest(manifest_path, expected, records)

    log.write("=" * 72)
    if result["complete"]:
        log.write("SESSION VERIFIED COMPLETE")
        log.write(f"Expected frames:     {len(expected)}")
        log.write(f"Verified full JPEGs: {len(verified)}")
        log.write("Missing/invalid:     0")
        log.write(f"Manifest:            {manifest_path}")
        log.write(f"Summary:             {summary_path}")
        log.write("=" * 72)
        return 0

    log.write("SESSION INCOMPLETE")
    log.write(f"Expected frames:     {len(expected)}")
    log.write(f"Verified full JPEGs: {len(verified)}")
    log.write(f"Missing/invalid:     {len(missing_final)}")
    for item in missing_final[:30]:
        log.write(
            f"  frame {item.frame:06d}: {item.remote_jpg} | "
            f"{records.get(item.frame, {}).get('last_error', 'not verified')}"
        )
    log.write("Rerun the exact same command: verified files will be skipped and gaps retried.")
    log.write(f"Manifest:            {manifest_path}")
    log.write(f"Summary:             {summary_path}")
    log.write("=" * 72)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
