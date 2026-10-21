from datetime import datetime, timedelta

from v6_camera_alive import new_jpegs, recent_success


def test_new_jpegs_only_returns_new_relative_paths():
    before = {"DCIM/100OLYMP/P1010001.JPG"}
    after = before | {"DCIM/100OLYMP/P1010002.JPG"}
    assert new_jpegs(before, after) == ["DCIM/100OLYMP/P1010002.JPG"]


def test_recent_success_is_scoped_to_mission(tmp_path):
    state = tmp_path / "camera_alive.json"
    state.write_text(
        '{"status":"ok","mission_key":"sunrise",'
        f'"checked_at":"{datetime.now().astimezone().isoformat()}"}}'
    )
    assert recent_success(state, "sunrise", 900) is not None
    assert recent_success(state, "sunset", 900) is None


def test_recent_success_expires(tmp_path):
    state = tmp_path / "camera_alive.json"
    old = datetime.now().astimezone() - timedelta(minutes=20)
    state.write_text(
        '{"status":"ok","mission_key":"sunrise",'
        f'"checked_at":"{old.isoformat()}"}}'
    )
    assert recent_success(state, "sunrise", 900) is None
