#!/usr/bin/env python3
"""Prove that the wired camera can create and expose a new JPEG."""

from __future__ import annotations

import fcntl
import json
import time
from datetime import datetime
from pathlib import Path

import mega4_usb_import
import v6_config
from PIL import Image
from v6_intervalometer import WiredCamera


JPEG_SUFFIXES = {".jpg", ".jpeg"}


def jpeg_inventory(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in JPEG_SUFFIXES
    }


def new_jpegs(before: set[str], after: set[str]) -> list[str]:
    return sorted(after - before)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def purge_stale_test_shot(now: datetime | None = None) -> bool:
    """Delete dashboard test-shot files once their 24-hour lifetime expires."""
    state = _load_state(v6_config.TEST_SHOT_STATE)
    timestamp = state.get("completed_at") or state.get("requested_at")
    checked_at = None
    if timestamp:
        try:
            checked_at = datetime.fromisoformat(str(timestamp))
        except (TypeError, ValueError):
            checked_at = None
    if checked_at is None and v6_config.TEST_SHOT_IMAGE.exists():
        checked_at = datetime.fromtimestamp(
            v6_config.TEST_SHOT_IMAGE.stat().st_mtime,
            tz=datetime.now().astimezone().tzinfo,
        )
    if checked_at is None:
        return False
    current = now or datetime.now().astimezone()
    if (current - checked_at).total_seconds() <= v6_config.TEST_SHOT_MAX_AGE_SECONDS:
        return False
    v6_config.TEST_SHOT_IMAGE.unlink(missing_ok=True)
    v6_config.TEST_SHOT_STATE.unlink(missing_ok=True)
    return True


def _save_test_shot_preview(source: Path, mission_key: str, completed_at: str) -> None:
    """Publish an upright dashboard preview without retaining camera media."""
    v6_config.TEST_SHOT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = v6_config.TEST_SHOT_IMAGE.with_suffix(".tmp.jpg")
    with Image.open(source) as image:
        image.rotate(90, expand=True).convert("RGB").save(
            temporary,
            format="JPEG",
            quality=92,
            optimize=True,
        )
    temporary.replace(v6_config.TEST_SHOT_IMAGE)
    _save_state(v6_config.TEST_SHOT_STATE, {
        "status": "complete",
        "source": "scheduled_preflight",
        "mission_key": mission_key,
        "requested_at": completed_at,
        "completed_at": completed_at,
        "source_name": source.name,
        "image": str(v6_config.TEST_SHOT_IMAGE),
    })


def recent_success(path: Path, mission_key: str, max_age_seconds: float) -> dict | None:
    state = _load_state(path)
    if state.get("status") != "ok" or state.get("mission_key") != mission_key:
        return None
    try:
        checked_at = datetime.fromisoformat(state["checked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    age = (datetime.now().astimezone() - checked_at).total_seconds()
    return state if 0 <= age <= max_age_seconds else None


def _read_camera_inventory() -> tuple[set[str], Path, Path]:
    device = None
    try:
        mega4_usb_import.set_port("on")
        device = mega4_usb_import.wait_for_device(
            v6_config.CAMERA_ALIVE_DEVICE_TIMEOUT_SECONDS
        )
        root = mega4_usb_import.ensure_mounted(device)
        return jpeg_inventory(root), device, root
    except Exception:
        mega4_usb_import.cleanup(device)
        raise


def _release_camera(device: Path | None) -> None:
    mega4_usb_import.cleanup(device)


def _run_camera_alive_test_locked(mission_key: str) -> dict:
    """Capture one diagnostic frame, verify it is new, then remove it."""
    _save_state(v6_config.CAMERA_ALIVE_STATE, {
        "status": "testing",
        "mission_key": mission_key,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    before, device, _ = _read_camera_inventory()
    _release_camera(device)
    # The E-M5 III remains at "One moment" after USB storage disappears. The
    # dashboard's proven path needs this full release interval before capture.
    time.sleep(v6_config.CAMERA_ALIVE_USB_SETTLE_SECONDS)

    with WiredCamera(
        v6_config.FOCUS_GPIO,
        v6_config.SHUTTER_GPIO,
        active_low=v6_config.RELAY_ACTIVE_LOW,
    ) as camera:
        camera.take_picture(v6_config.SHUTTER_PULSE_SECONDS)
        # The camera is in aperture priority and may still be exposing long
        # after the relay releases. AF cleanup during that exposure can disturb
        # the remote state, while enabling USB too early makes a valid delayed
        # JPEG look like a failed capture.
        time.sleep(v6_config.CAMERA_ALIVE_CAPTURE_SETTLE_SECONDS)
        camera.clear_half_press(v6_config.HALF_PRESS_CLEAR_SECONDS)

    time.sleep(v6_config.CAMERA_ALIVE_USB_SETTLE_SECONDS)
    device = None
    try:
        after, device, root = _read_camera_inventory()
        created = new_jpegs(before, after)
        if not created:
            raise RuntimeError("camera did not create a new JPEG")
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        preview_source = root / created[-1]
        _save_test_shot_preview(preview_source, mission_key, checked_at)
        for relative in created:
            (root / relative).unlink()
        payload = {
            "status": "ok",
            "mission_key": mission_key,
            "checked_at": checked_at,
            "created_and_deleted": created,
            "preview": str(v6_config.TEST_SHOT_IMAGE),
        }
        _save_state(v6_config.CAMERA_ALIVE_STATE, payload)
        return payload
    except Exception as exc:
        payload = {
            "status": "failed",
            "mission_key": mission_key,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        }
        _save_state(v6_config.CAMERA_ALIVE_STATE, payload)
        raise
    finally:
        _release_camera(device)


def run_camera_alive_test(mission_key: str) -> dict:
    """Serialize camera USB and relay ownership across dashboard and scheduler."""
    v6_config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with v6_config.CAMERA_OPERATION_LOCK.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _run_camera_alive_test_locked(mission_key)


def ensure_camera_alive(mission_key: str) -> tuple[bool, str]:
    cached = recent_success(
        v6_config.CAMERA_ALIVE_STATE,
        mission_key,
        v6_config.CAMERA_ALIVE_MAX_AGE_SECONDS,
    )
    if cached:
        return True, f"OK camera alive test cached from {cached['checked_at']}"
    try:
        result = run_camera_alive_test(mission_key)
    except Exception as exc:
        return False, f"FAIL camera alive test: {type(exc).__name__}: {exc}"
    created = result.get("created_and_deleted", [])
    name = Path(created[-1]).name if created else "new JPEG"
    return True, f"OK camera alive: captured, verified, and deleted {name}"
