#!/usr/bin/env python3
"""Run one full-resolution import through MEGA4 USB storage safely."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


CAMERA_SWITCH = "/usr/local/sbin/timelapser-camera-usb"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    log("RUN " + " ".join(command))
    return subprocess.run(command, text=True, check=check)


def camera_partition() -> Path | None:
    result = subprocess.run(
        ["lsblk", "--json", "-o", "PATH,MODEL,TYPE"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    for disk in json.loads(result.stdout).get("blockdevices", []):
        if disk.get("type") != "disk" or "E-M5MarkIII" not in (disk.get("model") or ""):
            continue
        partitions = [child for child in disk.get("children", []) if child.get("type") == "part"]
        if len(partitions) != 1:
            raise RuntimeError(f"Expected one Olympus SD partition, found {len(partitions)}")
        return Path(partitions[0]["path"])
    return None


def mountpoint(device: Path) -> Path | None:
    result = subprocess.run(
        ["findmnt", "-nr", "-o", "TARGET", "-S", str(device)],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return Path(value) if result.returncode == 0 and value else None


def set_port(action: str) -> None:
    run(["sudo", CAMERA_SWITCH, action])


def wait_for_device(timeout: float) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        device = camera_partition()
        if device and device.exists():
            log(f"Olympus storage appeared as {device}")
            return device
        log("Waiting for Olympus USB storage...")
        time.sleep(2)
    raise TimeoutError(f"Olympus storage did not appear within {timeout:.0f} seconds")


def ensure_mounted(device: Path, timeout: float = 30) -> Path:
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        current = mountpoint(device)
        if current is not None:
            log(f"Olympus storage mounted at {current}")
            return current

        attempt += 1
        result = run(["udisksctl", "mount", "-b", str(device)], check=False)
        if result.returncode != 0:
            log(f"Mount attempt {attempt} is not ready yet; retrying...")
        time.sleep(2)

    raise TimeoutError(f"Could not mount {device} within {timeout:.0f} seconds")


def cleanup(device: Path | None) -> None:
    try:
        run(["sync"], check=False)
        if device is not None and mountpoint(device) is not None:
            run(["udisksctl", "unmount", "-b", str(device)], check=False)
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
    device = None
    try:
        stage_started = time.monotonic()
        set_port("on")
        port_enabled = True
        log(f"MEGA4 power-on elapsed={time.monotonic() - stage_started:.1f}s")
        stage_started = time.monotonic()
        device = wait_for_device(args.device_timeout)
        log(f"USB enumeration elapsed={time.monotonic() - stage_started:.1f}s")
        stage_started = time.monotonic()
        sd_root = ensure_mounted(device)
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
            cleanup(device)
            log(f"USB cleanup elapsed={time.monotonic() - cleanup_started:.1f}s")
        log(f"MEGA4 import workflow elapsed={time.monotonic() - workflow_started:.1f}s")


if __name__ == "__main__":
    raise SystemExit(main())
