import json

import v6_exposure_model
from v6_exposure_model import cadence_prediction, expected_shutter_seconds


def test_sunrise_curve_shortens_exposure_and_cadence():
    dark = cadence_prediction("sunrise", -40, 5.0)
    daylight = cadence_prediction("sunrise", 20, 5.0)
    assert dark is not None and daylight is not None
    assert dark.shutter_seconds == 6.0
    assert dark.interval_seconds >= 17.5
    assert daylight.shutter_seconds < 0.01
    assert daylight.interval_seconds == 5.0


def test_sunset_curve_lengthens_and_plateaus():
    daylight = cadence_prediction("sunset", -40, 5.0)
    dark = cadence_prediction("sunset", 30, 5.0)
    assert daylight is not None and dark is not None
    assert daylight.shutter_seconds < dark.shutter_seconds
    assert dark.shutter_seconds == 3.2
    assert dark.interval_seconds == 6.5


def test_interpolation_uses_exposure_space():
    midpoint = expected_shutter_seconds("sunrise", -21.5)
    assert midpoint is not None
    assert 2.4 < midpoint < 2.7


def test_general_mode_has_no_prediction():
    assert cadence_prediction("general", 0, 5.0) is None


def test_learned_curve_can_only_make_prediction_more_conservative(tmp_path, monkeypatch):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({
        "modes": {"sunrise": {"points": [[-40, 8.0], [-20, 4.0], [0, 0.1]]}}
    }))
    monkeypatch.setenv("TIMELAPSER_V6_EXPOSURE_CALIBRATION", str(calibration))
    assert expected_shutter_seconds("sunrise", -40) == 8.0
    assert expected_shutter_seconds("sunrise", 0) >= 0.1
    assert "learned calibration" in v6_exposure_model.model_source("sunrise")
