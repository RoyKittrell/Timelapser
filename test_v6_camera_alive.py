from datetime import datetime, timedelta

from unittest.mock import patch

from v6_camera_alive import new_jpegs, purge_stale_test_shot, recent_success


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


def test_stale_dashboard_preview_is_deleted(tmp_path):
    shot_dir = tmp_path / "test_shots"
    shot_dir.mkdir()
    image = shot_dir / "latest.jpg"
    state = shot_dir / "latest.json"
    image.write_bytes(b"jpeg")
    old = datetime.now().astimezone() - timedelta(hours=25)
    state.write_text('{"status":"complete","completed_at":"' + old.isoformat() + '"}')
    with (
        patch("v6_camera_alive.v6_config.TEST_SHOT_IMAGE", image),
        patch("v6_camera_alive.v6_config.TEST_SHOT_STATE", state),
        patch("v6_camera_alive.v6_config.TEST_SHOT_MAX_AGE_SECONDS", 86400),
    ):
        assert purge_stale_test_shot() is True
    assert not image.exists()
    assert not state.exists()


def test_recent_dashboard_preview_is_retained(tmp_path):
    shot_dir = tmp_path / "test_shots"
    shot_dir.mkdir()
    image = shot_dir / "latest.jpg"
    state = shot_dir / "latest.json"
    image.write_bytes(b"jpeg")
    recent = datetime.now().astimezone() - timedelta(hours=23)
    state.write_text('{"status":"complete","completed_at":"' + recent.isoformat() + '"}')
    with (
        patch("v6_camera_alive.v6_config.TEST_SHOT_IMAGE", image),
        patch("v6_camera_alive.v6_config.TEST_SHOT_STATE", state),
        patch("v6_camera_alive.v6_config.TEST_SHOT_MAX_AGE_SECONDS", 86400),
    ):
        assert purge_stale_test_shot() is False
    assert image.exists()
    assert state.exists()
