#!/usr/bin/env python3
"""Safely discover and switch the MEGA4 port containing the Olympus camera."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


CAMERA_IDS = {("07b4", "012e")}
SYSFS_USB = Path("/sys/bus/usb/devices")
STATE_FILE = Path("/var/lib/timelapser/camera_usb_port.json")
UHUBCTL = "/usr/sbin/uhubctl"
USB_PATH = re.compile(r"^(?P<hub>\d+-\d+(?:\.\d+)*)\.(?P<port>\d+)$")


def read_text(path: Path) -> str:
    return path.read_text(encoding="ascii").strip().lower()


def discover_camera(sysfs_root: Path = SYSFS_USB) -> dict[str, str | int] | None:
    matches = []
    for device in sysfs_root.iterdir():
        try:
            identity = (read_text(device / "idVendor"), read_text(device / "idProduct"))
        except OSError:
            continue
        if identity not in CAMERA_IDS:
            continue
        path_match = USB_PATH.match(device.name)
        if not path_match:
            raise RuntimeError(f"Olympus found at unsupported USB path {device.name}")
        matches.append({
            "hub": path_match.group("hub"),
            "port": int(path_match.group("port")),
            "usb_path": device.name,
            "vendor": identity[0],
            "product": identity[1],
        })
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Olympus USB devices found: {matches}")
    return matches[0] if matches else None


def save_mapping(mapping: dict[str, str | int], state_file: Path = STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state_file)


def load_mapping(state_file: Path = STATE_FILE) -> dict[str, str | int]:
    mapping = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(mapping.get("hub"), str) or not isinstance(mapping.get("port"), int):
        raise RuntimeError(f"Invalid saved camera USB mapping: {state_file}")
    return mapping


def run_switch(mapping: dict[str, str | int], action: str) -> None:
    subprocess.run(
        [UHUBCTL, "-l", str(mapping["hub"]), "-p", str(mapping["port"]), "-a", action],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("discover", "discover-and-off", "on", "off"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("camera_usb_port.py must run as root")

    visible = discover_camera()
    if visible:
        save_mapping(visible)
        print(f"Olympus USB: hub={visible['hub']} port={visible['port']} path={visible['usb_path']}")

    if args.action == "discover":
        return 0 if visible else 1
    if args.action == "discover-and-off":
        if not visible:
            print("Olympus USB is not visible; leaving every hub port unchanged")
            return 0
        run_switch(visible, "off")
        return 0
    if args.action == "off":
        if not visible:
            raise SystemExit("Refusing to switch off: Olympus USB is not currently visible")
        run_switch(visible, "off")
        return 0

    mapping = visible or load_mapping()
    run_switch(mapping, "on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
