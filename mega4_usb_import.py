#!/usr/bin/env python3
"""Run one full-resolution import through MEGA4 USB storage safely."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


DEVICE = Path("/dev/sda1")
UHUBCTL = "/usr/sbin/uhubctl"
HUB_LOCATION = "1-1"
HUB_PORT = "4"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    log("RUN " + " ".join(command))
    return subprocess.run(command, text=True, check=check)


def mountpoint() -> Path | None:
    result = subprocess.run(
        ["findmnt", "-nr", "-o", "TARGET", "-S", str(DEVICE)],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return Path(value) if result.returncode == 0 and value else None


def set_port(action: str) -> None:
    run(["sudo", UHUBCTL, "-l", HUB_LOCATION, "-p", HUB_PORT, "-a", action])


def wait_for_device(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if DEVICE.exists():
            log(f"Olympus storage appeared as {DEVICE}")
            return
        log("Waiting for Olympus USB storage...")
        time.sleep(2)
    raise TimeoutError(f"{DEVICE} did not appear within {timeout:.0f} seconds")


def ensure_mounted(timeout: float = 30) -> Path:
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        current = mountpoint()
        if current is not None:
            log(f"Olympus storage mounted at {current}")
            return current

        attempt += 1
        result = run(["udisksctl", "mount", "-b", str(DEVICE)], check=False)
        if result.returncode != 0:
            log(f"Mount attempt {attempt} is not ready yet; retrying...")
        time.sleep(2)

    raise TimeoutError(f"Could not mount {DEVICE} within {timeout:.0f} seconds")


def cleanup() -> None:
    try:
        run(["sync"], check=False)
        if mountpoint() is not None:
            run(["udisksctl", "unmount", "-b", str(DEVICE)], check=False)
    finally:
        set_port("off")
        log("MEGA4 camera port is off; camera may return to shooting mode")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--device-timeout", type=float, default=45)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workflow_started = time.monotonic()
    root = Path(__file__).resolve().parent
    importer = root / "timelapser_v6_beta_usb_import.py"
    result_code = 1
    port_enabled = False
    try:
        stage_started = time.monotonic()
        set_port("on")
        port_enabled = True
        log(f"MEGA4 power-on elapsed={time.monotonic() - stage_started:.1f}s")
        stage_started = time.monotonic()
        wait_for_device(args.device_timeout)
        log(f"USB enumeration elapsed={time.monotonic() - stage_started:.1f}s")
        stage_started = time.monotonic()
        sd_root = ensure_mounted()
        log(f"SD mount elapsed={time.monotonic() - stage_started:.1f}s")
        stage_started = time.monotonic()
        result = run(
            [
                sys.executable,
                str(importer),
                str(args.run_dir.resolve()),
                "--sd-root",
                str(sd_root),
                "--delete-imported-from-sd",
            ],
            check=False,
        )
        log(f"Full-JPEG copy elapsed={time.monotonic() - stage_started:.1f}s")
        result_code = result.returncode
        return result_code
    finally:
        if port_enabled:
            cleanup_started = time.monotonic()
            cleanup()
            log(f"USB cleanup elapsed={time.monotonic() - cleanup_started:.1f}s")
        log(f"MEGA4 import workflow elapsed={time.monotonic() - workflow_started:.1f}s")


if __name__ == "__main__":
    raise SystemExit(main())
