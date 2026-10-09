from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from timelapser_v6_usb_import import import_run
import v6_config
from v6_intervalometer import SimulatedOutput, WiredCamera


def make_jpeg(path: Path, *, taken: str, iso: int, exposure=(1, 125)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.effect_noise((1800, 1200), 35).convert("RGB")
    exif = Image.Exif()
    exif[306] = taken
    exif[0x8769] = {34855: iso, 33434: exposure, 33437: (56, 10), 36867: taken}
    image.save(path, quality=95, exif=exif)


def test_wired_camera_pulses_and_releases(monkeypatch):
    monkeypatch.setattr("v6_intervalometer.time.sleep", lambda _seconds: None)
    camera = WiredCamera(17, 27, simulate=True)
    assert isinstance(camera.focus, SimulatedOutput)
    camera.autofocus_once(1.0)
    camera.take_picture(0.1)
    assert camera.focus.value is False
    assert camera.shutter.value is False
    camera.close()
    assert camera.focus.closed
    assert camera.shutter.closed


def test_installed_relay_polarity_is_active_high():
    assert v6_config.RELAY_ACTIVE_LOW is False


def test_capture_simulation_creates_trigger_only_run(tmp_path):
    command = [
        sys.executable, str(Path(__file__).parent / "timelapser_v6.py"),
        "--simulate", "--skip-autofocus", "--output-root", str(tmp_path),
        "--interval", "0.5", "--max-frames", "3", "--max-run-minutes", "1",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    run_dir = next(tmp_path.glob("*_v6_wired_simulated"))
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    assert manifest["completed_frames"] == 3
    assert (run_dir / "trigger_timeline.csv").exists()
    assert not (run_dir / "telemetry.csv").exists()
    assert not (run_dir / "frames_jpeg").exists()


def test_usb_import_selects_newest_sequence_and_builds_telemetry(tmp_path):
    run_dir = tmp_path / "run_v6_wired"
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(json.dumps({"completed_frames": 2}))
    sd_root = tmp_path / "sd"
    olympus = sd_root / "DCIM" / "100OLYMP"
    make_jpeg(olympus / "PA010001.JPG", taken="2026:10:06 06:00:00", iso=100)
    make_jpeg(olympus / "PA010002.JPG", taken="2026:10:06 07:00:00", iso=200)
    make_jpeg(olympus / "PA010003.JPG", taken="2026:10:06 07:00:05", iso=400)

    result = import_run(run_dir, sd_root)
    assert result["status"] == "complete"
    assert result["imported_frames"] == 2
    telemetry = (run_dir / "telemetry.csv").read_text()
    assert "PA010002.JPG" in telemetry
    assert "PA010003.JPG" in telemetry
    assert "PA010001.JPG" not in telemetry
    assert len(list((run_dir / "frames_full_jpeg").glob("frame_*.jpg"))) == 2


def test_real_capture_refuses_same_gpio_for_both_relays(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "timelapser_v6.py"),
         "--output-root", str(tmp_path), "--max-frames", "1",
         "--focus-gpio", "23", "--shutter-gpio", "23"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "must be different" in result.stderr
