import csv
import json
from pathlib import Path

from v6_exposure_learning import valid_run_samples


def make_run(root: Path, name: str, started_at: str, iso: int) -> Path:
    run = root / name
    run.mkdir()
    (run / "run_manifest.json").write_text(json.dumps({
        "mode": "sunrise",
        "event_time": "2026-10-29T06:57:00+08:00",
        "started_at": started_at,
    }))
    (run / "v6_usb_import_summary.json").write_text(json.dumps({"status": "complete"}))
    with (run / "telemetry.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "time", "actual_shutter_seconds", "actual_iso", "actual_aperture",
        ))
        writer.writeheader()
        for index in range(100):
            writer.writerow({
                "time": f"2026-10-29 06:{17 + index // 60:02d}:{index % 60:02d}",
                "actual_shutter_seconds": 2.0,
                "actual_iso": iso,
                "actual_aperture": 5.8,
            })
    return run


def test_learning_excludes_runs_before_new_camera_settings(tmp_path):
    run = make_run(tmp_path, "old", "2026-10-28T06:17:00+08:00", 200)
    assert valid_run_samples(run) is None


def test_learning_accepts_auto_iso_range_after_epoch(tmp_path):
    run = make_run(tmp_path, "new", "2026-10-29T06:17:00+08:00", 400)
    mode, samples = valid_run_samples(run)
    assert mode == "sunrise"
    assert len(samples) == 100
