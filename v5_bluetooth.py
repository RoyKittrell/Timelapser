import subprocess
import time
import urllib.request

from v5_config import (
    CAMERA_BLUETOOTH_ADDRESS,
    CAMERA_BLUETOOTH_NAME,
    CAMERA_IP,
    CAMERA_WIFI_CONNECTION_NAME,
    CAMERA_WIFI_INTERFACE,
    CAMERA_WIFI_WAKE_POLL_SECONDS,
    CAMERA_WIFI_WAKE_TIMEOUT_SECONDS,
    ENABLE_BLUETOOTH_WIFI_WAKE,
)


def _run_command(args, *, timeout):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "duration_seconds": time.monotonic() - started,
            "output": (proc.stdout or "").strip(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "duration_seconds": time.monotonic() - started,
            "output": f"{type(exc).__name__}: {exc}",
        }


def camera_api_available(*, camera_ip=CAMERA_IP, timeout=3.0):
    try:
        with urllib.request.urlopen(
            f"http://{camera_ip}/get_caminfo.cgi",
            timeout=float(timeout),
        ) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def _bluetooth_scan_connect(logger, *, reason):
    script_steps = [
        ("agent on\n", 0.5),
        ("default-agent\n", 0.5),
        ("scan on\n", 10.0),
        (f"connect {CAMERA_BLUETOOTH_ADDRESS}\n", 8.0),
        (f"info {CAMERA_BLUETOOTH_ADDRESS}\n", 1.0),
        ("scan off\n", 0.5),
        ("quit\n", 0.0),
    ]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for text, delay in script_steps:
            proc.stdin.write(text)
            proc.stdin.flush()
            if delay:
                time.sleep(delay)
        output, _ = proc.communicate(timeout=8)
        ok = proc.returncode == 0 and (
            "Connection successful" in output or f"[{CAMERA_BLUETOOTH_NAME}]" in output
        )
        logger.event(
            "bluetooth_wifi_wake_connect",
            reason=str(reason),
            ok=ok,
            returncode=proc.returncode,
            duration_seconds=time.monotonic() - started,
            output_tail=output[-2000:],
        )
        return ok
    except Exception as exc:
        logger.event(
            "bluetooth_wifi_wake_connect",
            reason=str(reason),
            ok=False,
            duration_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False


def try_wake_camera_wifi(logger, *, reason, camera_ip=CAMERA_IP):
    """Try to wake/rejoin the Olympus Wi-Fi endpoint via Bluetooth.

    The E-M5 III advertises a BLE service that appears to wake its OI.Share Wi-Fi
    endpoint when connected.  This helper only runs after normal Wi-Fi access has
    failed, and it never writes to camera GATT characteristics.
    """
    if not ENABLE_BLUETOOTH_WIFI_WAKE:
        return False
    if camera_api_available(camera_ip=camera_ip, timeout=2.0):
        return True

    logger.event(
        "bluetooth_wifi_wake_started",
        reason=str(reason),
        bluetooth_address=CAMERA_BLUETOOTH_ADDRESS,
        bluetooth_name=CAMERA_BLUETOOTH_NAME,
        wifi_interface=CAMERA_WIFI_INTERFACE,
        wifi_connection=CAMERA_WIFI_CONNECTION_NAME,
        timeout_seconds=float(CAMERA_WIFI_WAKE_TIMEOUT_SECONDS),
    )
    logger.human(
        f"Camera Wi-Fi is not responding; trying Bluetooth wake via "
        f"{CAMERA_BLUETOOTH_NAME}."
    )

    for label, args in (
        ("wifi_link_up", ["ip", "link", "set", CAMERA_WIFI_INTERFACE, "up"]),
        ("wifi_nmcli_connect", ["nmcli", "dev", "connect", CAMERA_WIFI_INTERFACE]),
        ("wifi_nmcli_connection_up", ["nmcli", "connection", "up", CAMERA_WIFI_CONNECTION_NAME]),
    ):
        result = _run_command(args, timeout=8)
        logger.event(f"bluetooth_wifi_wake_{label}", reason=str(reason), **result)

    _bluetooth_scan_connect(logger, reason=reason)

    deadline = time.monotonic() + float(CAMERA_WIFI_WAKE_TIMEOUT_SECONDS)
    poll = max(0.5, float(CAMERA_WIFI_WAKE_POLL_SECONDS))
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        result = _run_command(
            ["nmcli", "connection", "up", CAMERA_WIFI_CONNECTION_NAME],
            timeout=8,
        )
        logger.event(
            "bluetooth_wifi_wake_connection_retry",
            reason=str(reason),
            attempt=attempt,
            **result,
        )
        if camera_api_available(camera_ip=camera_ip, timeout=3.0):
            logger.event(
                "bluetooth_wifi_wake_succeeded",
                reason=str(reason),
                attempt=attempt,
            )
            logger.human("Camera Wi-Fi API recovered after Bluetooth wake.")
            return True
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    logger.event("bluetooth_wifi_wake_failed", reason=str(reason))
    logger.human("Bluetooth wake did not restore the camera Wi-Fi API.")
    return False
