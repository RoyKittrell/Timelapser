from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from PIL import Image

from v5_config import (
    DIRECTOR_MAX_DURATION_SECONDS,
    GENERAL_BASELINE,
    SUNRISE_BASELINE,
    SUNSET_BASELINE,
    LENS_PROFILE_NAME,
    LENS_MIN_APERTURE,
    LENS_MAX_APERTURE,
    MIN_APERTURE,
    PREFERRED_MAX_SHUTTER_SECONDS,
)
from v5_camera import WifiCameraController
from v5_commander import AICommander
from v5_image import analyse_jpeg
from v5_logging import RunLogger
from v5_state import SharedState, TimelapseState

PROJECT_ROOT = Path('/home/roy/Timelapser Sept2026')
V5_ROOT = PROJECT_ROOT / 'timelapser_v5'
ENV_FILE = PROJECT_ROOT / 'env' / '.env'
PYTHON = Path('/home/roy/timelapser-venv/bin/python3')
TIMELAPSER_SCRIPT = V5_ROOT / 'timelapser_v5.py'
CONTROL_DIR = V5_ROOT / 'control'
STOP_REQUEST_FILE = CONTROL_DIR / 'stop_requested.json'
DIRECTOR_CONTROL_LOG = CONTROL_DIR / 'director_control.log'
PREFLIGHT_ALERT_FILE = CONTROL_DIR / 'director_preflight_alert.json'
TEST_SHOTS_DIR = V5_ROOT / 'director_test_shots'
CAMERA_INFO_URL = 'http://192.168.0.10/get_caminfo.cgi'
CAMERA_IMAGE_LIST_URL = 'http://192.168.0.10/get_imglist.cgi'
CAMERA_SD_CARD_TOTAL_BYTES = 64 * 1024 ** 3
CAMERA_SD_CAPACITY_TTL_SECONDS = 300
REFRESH_SECONDS = 5
MAX_CHART_ROWS = 1200
TIMELAPSER_PROCESS_NAMES = (
    'timelapser_v5.py',
    'timelapser_v5_aperture_priority.py',
)


def configured_output_root() -> Path:
    override = os.environ.get('TIMELAPSER_OUTPUT_ROOT')
    if override:
        return Path(override).expanduser()

    schedule_path = V5_ROOT / 'schedule.py'
    try:
        spec = importlib.util.spec_from_file_location('director_output_config', schedule_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f'Cannot load {schedule_path}')
        schedule = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schedule)
        return Path(schedule.OUTPUT_ROOT).expanduser()
    except Exception:
        return PROJECT_ROOT


RUNS_ROOT = configured_output_root()

load_dotenv(ENV_FILE)

st.set_page_config(
    page_title='Timelapser V5 Director',
    page_icon='📷',
    layout='wide',
    initial_sidebar_state='expanded',
)

st.markdown(
    """
    <style>
    html, body, [data-testid="stAppViewContainer"] {
        max-width: 100vw;
        overflow-x: hidden;
    }

    [data-testid="stMetricValue"] {
        overflow-wrap: anywhere;
        white-space: normal;
    }

    img, video, canvas, svg {
        max-width: 100%;
        height: auto;
    }

    pre, code, [data-testid="stJson"] {
        white-space: pre-wrap;
        word-break: break-word;
    }

    @media (max-width: 768px) {
        .block-container {
            max-width: 100vw;
            padding-left: 1rem;
            padding-right: 1rem;
            padding-top: 1.25rem;
        }

        [data-testid="column"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 0 !important;
        }

        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
            gap: 0.35rem;
        }

        [data-testid="stMetric"] {
            min-width: 0;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Files / process helpers. Normal timelapse start/stop still goes through
# constrained local process/control files. The explicit test-shot workflow below
# may briefly create its own WifiCameraController only when no timelapse is
# running.
# -----------------------------------------------------------------------------

def list_v5_runs() -> list[Path]:
    candidates: list[Path] = []
    roots = list(dict.fromkeys((RUNS_ROOT, V5_ROOT, PROJECT_ROOT)))
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            iterables = [
                root.rglob('*_v5_wifi'),
                root.rglob('*_v5_aperture_priority_wifi'),
            ]
            for iterable in iterables:
                for p in iterable:
                    if p.is_dir() and p not in seen and (
                        (p / 'telemetry.csv').exists()
                        or (p / 'run.log').exists()
                        or (p / 'events.jsonl').exists()
                    ):
                        seen.add(p)
                        candidates.append(p)
        except Exception:
            pass
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def is_timelapser_process(args: str) -> bool:
    if 'streamlit' in args:
        return False
    return any(name in args for name in TIMELAPSER_PROCESS_NAMES)


def process_script_label(args: str) -> str:
    if 'timelapser_v5_aperture_priority.py' in args:
        return 'aperture_priority'
    if 'timelapser_v5.py' in args:
        return 'v5'
    return 'unknown'


def tail_text(path: Path, max_chars: int = 20000) -> str:
    if not path.exists():
        return ''
    try:
        return path.read_text(encoding='utf-8', errors='replace')[-max_chars:]
    except Exception:
        return ''


def read_jsonl_tail(path: Path, max_lines: int = 100) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()[-max_lines:]
        for line in lines:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    except Exception:
        pass
    return out


def read_telemetry(run_dir: Path) -> pd.DataFrame:
    path = run_dir / 'telemetry.csv'
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, on_bad_lines='skip')
    except Exception:
        return pd.DataFrame()
    if 'time' in df.columns:
        df['time_dt'] = pd.to_datetime(df['time'], errors='coerce')
    return df


def latest_jpeg(run_dir: Path) -> Path | None:
    # Prefer the actual downloaded timelapse frame; fall back to startup scout.
    frame_dir = run_dir / 'frames_jpeg'
    imgs: list[Path] = []
    if frame_dir.exists():
        for pattern in ('*.jpg', '*.jpeg', '*.JPG', '*.JPEG'):
            imgs.extend(frame_dir.glob(pattern))
    if imgs:
        return max(imgs, key=lambda p: p.stat().st_mtime)
    scout = run_dir / 'startup_scout.jpg'
    return scout if scout.exists() else None


def run_test_shots_dir(run_dir: Path) -> Path:
    return run_dir / 'director_test_shots'


def _path_is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def latest_test_shot(run_dir: Path | None = None) -> Path | None:
    base = run_test_shots_dir(run_dir) if run_dir is not None else TEST_SHOTS_DIR
    if not base.exists():
        return None
    imgs = list(base.glob('*/final_full.jpg'))
    return max(imgs, key=lambda p: p.stat().st_mtime) if imgs else None


def display_test_shot_for_run(run_dir: Path) -> Path | None:
    session_path = st.session_state.get('latest_test_shot_path')
    if session_path:
        candidate = Path(session_path)
        if candidate.exists() and _path_is_inside(candidate, run_test_shots_dir(run_dir)):
            return candidate
    return latest_test_shot(run_dir)


def cleanup_test_shots_if_frames_deleted(run_dir: Path) -> None:
    test_dir = run_test_shots_dir(run_dir)
    if not test_dir.exists() or not (run_dir / 'run_summary.json').exists():
        return

    frame_dirs = [
        run_dir / 'frames_jpeg',
        run_dir / 'frames_full_jpeg',
        run_dir / 'frames_smoothed_luma_v2',
    ]
    if any(count_jpegs(folder) > 0 for folder in frame_dirs):
        return

    try:
        shutil.rmtree(test_dir)
        if st.session_state.get('latest_test_shot_path') and _path_is_inside(
            Path(st.session_state['latest_test_shot_path']),
            test_dir,
        ):
            st.session_state.pop('latest_test_shot_path', None)
            st.session_state.pop('hidden_test_shot_path', None)
    except Exception as exc:
        append_control_log({
            'action': 'test_shot_cleanup_failed',
            'run_dir': str(run_dir),
            'error_type': type(exc).__name__,
            'error': str(exc),
        })


def latest_test_shot_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return read_json(path.parent / 'test_shot_summary.json')


def rotated_image(path: Path) -> Image.Image | str:
    try:
        img = Image.open(path)
        return img.rotate(90, expand=True)
    except Exception:
        return str(path)


def v5_process_running() -> bool:
    return bool(active_v5_processes())


def active_v5_processes() -> list[dict[str, Any]]:
    try:
        cp = subprocess.run(
            ['ps', '-eo', 'pid=,args='],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        out = []
        for line in cp.stdout.splitlines():
            if not is_timelapser_process(line):
                continue
            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue
            out.append({
                'pid': int(parts[0]) if parts[0].isdigit() else parts[0],
                'args': parts[1] if len(parts) > 1 else '',
                'script': process_script_label(parts[1] if len(parts) > 1 else ''),
            })
        return out
    except Exception:
        return []


def append_control_log(record: dict[str, Any]) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    payload = {'time': datetime.now().astimezone().isoformat(timespec='seconds'), **record}
    with DIRECTOR_CONTROL_LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + '\n')


def camera_api_preflight(timeout: float = 3.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(CAMERA_INFO_URL, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read(256)
        return True, f'Olympus camera API reachable ({resp.status}, {len(payload)}+ bytes)'
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def show_test_shot(run_dir: Path | None = None) -> str:
    active = active_v5_processes()
    if active:
        return (
            f"A timelapse is already running under PID {active[0]['pid']}. "
            "I did not take a test shot because that would compete with the active capture loop."
        )

    ok, preflight = camera_api_preflight()
    append_control_log({'action': 'test_shot_preflight', 'ok': ok, 'camera_api': preflight})
    if not ok:
        return (
            "I could not take a test shot because the Olympus camera API is not reachable.\n\n"
            f"Camera check: {preflight}\n\n"
            "The camera Wi-Fi may be off, the Pi may not be joined to the camera SSID on wlan1, "
            "or the camera may still be waking up."
        )

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    shot_base = run_test_shots_dir(run_dir) if run_dir is not None else TEST_SHOTS_DIR
    shot_dir = shot_base / stamp
    shot_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(shot_dir)
    camera = WifiCameraController(logger)
    commander = AICommander(logger)
    state = SharedState(TimelapseState(mode='test_shot', run_dir=str(shot_dir)))

    try:
        current = camera.open()
        state.update(lambda s: (
            setattr(s, 'iso', int(current['iso'])),
            setattr(s, 'aperture', float(current['aperture'])),
            setattr(s, 'shutter_seconds', float(current['shutter_seconds'])),
        ))

        scout = camera.capture(0)
        scout_thumb = shot_dir / 'scout_thumbnail.jpg'
        thumb_data = camera.download_thumbnail(scout.remote_jpg)
        scout_thumb.write_bytes(bytes(thumb_data))
        scout_metrics = analyse_jpeg(scout_thumb)
        state.update(lambda s: (
            setattr(s, 'last_remote_jpg', scout.remote_jpg),
            setattr(s, 'last_remote_orf', scout.remote_orf),
            setattr(s, 'last_local_jpeg', str(scout_thumb)),
            setattr(s, 'last_preview_jpeg', str(scout_thumb)),
            setattr(s, 'last_image_metrics', scout_metrics),
        ))

        actions = commander.startup_review(state.snapshot(), str(scout_thumb))
        exposure = next((a for a in actions if a.action == 'SET_EXPOSURE'), None)
        exposure_note = 'AI returned no valid exposure change; final shot used current camera settings.'
        exposure_outcome: dict[str, Any] = {'ok': True, 'applied': {}}
        if exposure is not None:
            exposure_outcome = camera.set_exposure(
                iso=exposure.iso,
                aperture=exposure.aperture,
                shutter_seconds=exposure.shutter_seconds,
            )
            applied = exposure_outcome.get('applied', {})
            state.update(lambda s: (
                setattr(s, 'iso', int(applied.get('iso', s.iso))),
                setattr(s, 'aperture', float(applied.get('aperture', s.aperture))),
                setattr(s, 'shutter_seconds', float(applied.get('shutter_seconds', s.shutter_seconds))),
                setattr(s, 'ai_last_note', exposure.reason),
            ))
            exposure_note = exposure.reason or 'AI selected test-shot exposure.'

        final = camera.capture(1)
        final_full = shot_dir / 'final_full.jpg'
        camera.download_jpeg(final.remote_jpg, final_full)
        final_metrics = analyse_jpeg(final_full)
        state.update(lambda s: (
            setattr(s, 'last_remote_jpg', final.remote_jpg),
            setattr(s, 'last_remote_orf', final.remote_orf),
            setattr(s, 'last_local_jpeg', str(final_full)),
            setattr(s, 'last_preview_jpeg', str(final_full)),
            setattr(s, 'last_image_metrics', final_metrics),
        ))

        summary = {
            'kind': 'director_test_shot',
            'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'run_dir': str(run_dir) if run_dir is not None else '',
            'shot_dir': str(shot_dir),
            'display_image': 'final_full',
            'scout_remote_jpg': scout.remote_jpg,
            'final_remote_jpg': final.remote_jpg,
            'scout_thumbnail': str(scout_thumb),
            'final_full': str(final_full),
            'scout_metrics': scout_metrics,
            'final_metrics': final_metrics,
            'exposure_outcome': exposure_outcome,
            'exposure_note': exposure_note,
        }
        (shot_dir / 'test_shot_summary.json').write_text(
            json.dumps(summary, indent=2, default=str),
            encoding='utf-8',
        )
        append_control_log({'action': 'test_shot_completed', **summary})
        st.session_state['latest_test_shot_path'] = str(final_full)
        return (
            "Test shot complete.\n\n"
            "Displayed image: final post-adjustment full JPEG\n"
            f"Scout median: {scout_metrics.get('median', 0):.3f}; "
            f"final median: {final_metrics.get('median', 0):.3f}\n"
            f"Final exposure state: ISO {state.snapshot()['iso']} · "
            f"f/{state.snapshot()['aperture']:g} · {format_shutter(state.snapshot()['shutter_seconds'])}\n"
            f"Image: {final_full}\n\n"
            f"Exposure note: {exposure_note}"
        )
    except Exception as exc:
        logger.error('director_test_shot', exc)
        append_control_log({
            'action': 'test_shot_failed',
            'shot_dir': str(shot_dir),
            'error_type': type(exc).__name__,
            'error': str(exc),
        })
        return f'Test shot failed: {type(exc).__name__}: {exc}'
    finally:
        try:
            camera.close()
        except Exception:
            pass


NUMBER_WORDS = {
    'a': 1,
    'an': 1,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
    'eleven': 11,
    'twelve': 12,
    'fifteen': 15,
    'twenty': 20,
    'thirty': 30,
    'forty': 40,
    'fifty': 50,
    'sixty': 60,
    'ninety': 90,
}
NUMBER_PATTERN = (
    r'(?:\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|'
    r'eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety)'
)


def parse_number_token(value: str) -> float:
    return float(NUMBER_WORDS.get(value.lower(), value))


def has_timelapse_word(text: str) -> bool:
    return bool(re.search(r'\btime\s*lapse\b|\btimelapse\b', text.lower()))


def parse_duration_seconds(text: str, default_seconds: int = 3600) -> tuple[int, bool]:
    total = 0.0
    pattern = rf'\b({NUMBER_PATTERN})\s*(hours?|hrs?|h|minutes?|mins?|min|m)\b'
    for value, unit in re.findall(pattern, text.lower()):
        n = parse_number_token(value)
        if unit.startswith(('h', 'hr', 'hour')):
            total += n * 3600
        else:
            total += n * 60
    if total <= 0:
        total = default_seconds
    requested = max(60, int(total))
    clamped = requested > int(DIRECTOR_MAX_DURATION_SECONDS)
    return min(requested, int(DIRECTOR_MAX_DURATION_SECONDS)), clamped


def parse_interval_seconds(text: str, default_seconds: float = 5.0) -> float:
    m = re.search(
        rf'(?:interval|every)\s*(?:of\s*)?({NUMBER_PATTERN})\s*(seconds?|secs?|s)\b',
        text.lower(),
    )
    if not m:
        return default_seconds
    return max(1.0, min(parse_number_token(m.group(1)), 300.0))


def _unit_seconds(value: str, unit: str) -> float:
    n = parse_number_token(value)
    if unit.startswith(('h', 'hr', 'hour')):
        return n * 3600.0
    if unit.startswith(('s', 'sec', 'second')):
        return n
    return n * 60.0


def parse_future_start(text: str, tz: ZoneInfo) -> tuple[datetime, str] | None:
    """Parse simple Director schedule phrases into a local start datetime."""
    now = datetime.now(tz)
    t = text.lower()

    delay_match = re.search(
        rf'\b(?:in|after)\s*({NUMBER_PATTERN})\s*(hours?|hrs?|h|minutes?|mins?|min|m|seconds?|secs?|s)\b',
        t,
    )
    if not delay_match:
        delay_match = re.search(
            rf'\b({NUMBER_PATTERN})\s*(hours?|hrs?|h|minutes?|mins?|min|m|seconds?|secs?|s)\s+from\s+now\b',
            t,
        )
    if delay_match:
        target = now + timedelta(seconds=_unit_seconds(delay_match.group(1), delay_match.group(2)))
        if target.second or target.microsecond:
            target = (target + timedelta(minutes=1)).replace(second=0, microsecond=0)
        return target, text[:delay_match.start()] + text[delay_match.end():]

    date_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', t)
    time_match = re.search(r'\b(?:at\s*)?(\d{1,2}):(\d{2})\b', t)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if date_match:
            day = datetime.strptime(date_match.group(1), '%Y-%m-%d').date()
        elif 'tomorrow' in t:
            day = (now + timedelta(days=1)).date()
        else:
            day = now.date()
        target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if target <= now and not date_match and 'tomorrow' not in t:
            target += timedelta(days=1)
        sanitized = text
        matches = [m for m in (date_match, time_match) if m is not None]
        for match in sorted(matches, key=lambda m: m.start(), reverse=True):
            sanitized = sanitized[:match.start()] + sanitized[match.end():]
        return target, sanitized

    return None


def parse_schedule_command(text: str) -> dict[str, Any] | None:
    t = text.lower().strip()
    if not has_timelapse_word(t) or not re.search(r'\b(schedule|plan|add|create)\b', t):
        return None

    try:
        schedule_mod, _missions, _state = load_scheduler_data()
        tz = ZoneInfo(schedule_mod.TIMEZONE)
    except Exception:
        tz = datetime.now().astimezone().tzinfo or ZoneInfo('Asia/Kuala_Lumpur')

    parsed_start = parse_future_start(text, tz)
    if parsed_start is None:
        return None
    start_dt, duration_text = parsed_start

    mode = 'general'
    if 'sunrise' in t:
        mode = 'sunrise'
    elif 'sunset' in t:
        mode = 'sunset'

    interval = parse_interval_seconds(t)
    duration, duration_clamped = parse_duration_seconds(duration_text)
    transfer_mode = 'full' if re.search(r'\bfull(?:-|\s*)res|full\s+jpe?g|transfer\s+full\b', t) else 'thumbnail'
    max_frames = max(1, int(duration // interval))
    return {
        'action': 'schedule_future',
        'mode': mode,
        'start_dt': start_dt,
        'duration_seconds': duration,
        'duration_clamped': duration_clamped,
        'interval': interval,
        'max_frames': max_frames,
        'transfer_mode': transfer_mode,
        'source_text': text.strip(),
    }


def parse_operator_command(text: str) -> dict[str, Any] | None:
    t = text.lower().strip()
    if re.search(r'\b(show|take|make|display|get)\b', t) and re.search(r'\b(test|scout)\s+shot\b', t):
        return {'action': 'test_shot'}

    if re.search(r'\b(stop|end|halt)\b', t) and re.search(r'\b(run|timelapse|capture|current)\b', t):
        return {'action': 'stop', 'reason': text.strip() or 'operator requested graceful stop'}

    scheduled = parse_schedule_command(text)
    if scheduled is not None:
        return scheduled

    wants_timelapse = re.search(r'\b(start|take|run|begin)\b', t) and has_timelapse_word(t)
    if not wants_timelapse:
        return None
    if re.search(r'\bin\s+\d+', t) and 'now' not in t:
        return {'action': 'unsupported_future_start'}

    mode = 'general'
    if 'sunrise' in t:
        mode = 'sunrise'
    elif 'sunset' in t:
        mode = 'sunset'

    interval = parse_interval_seconds(t)
    duration, duration_clamped = parse_duration_seconds(t)
    transfer_mode = 'full' if re.search(r'\bfull(?:-|\s*)res|full\s+jpe?g|transfer\s+full\b', t) else 'thumbnail'
    max_frames = max(1, int(duration // interval))
    return {
        'action': 'start_now',
        'mode': mode,
        'duration_seconds': duration,
        'duration_clamped': duration_clamped,
        'interval': interval,
        'max_frames': max_frames,
        'transfer_mode': transfer_mode,
    }


def format_duration(seconds: int) -> str:
    minutes = seconds // 60
    if minutes >= 60 and minutes % 60 == 0:
        return f'{minutes // 60}h'
    if minutes >= 60:
        return f'{minutes // 60}h {minutes % 60}m'
    return f'{minutes}m'


def format_countdown(seconds: float) -> str:
    total_minutes = max(0, int(round(seconds / 60.0)))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f'{hours}h {minutes}min'
    if hours:
        return f'{hours}h'
    return f'{minutes}min'


def format_bytes(num: float) -> str:
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(num)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value:.0f} B'
        value /= 1024
    return f'{value:.1f} TB'


def count_jpegs(folder: Path) -> int:
    if not folder.exists():
        return 0
    try:
        return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg'})
    except Exception:
        return 0


def active_fullres_download_process(run_dir: Path) -> bool:
    try:
        cp = subprocess.run(
            ['ps', '-eo', 'pid=,args='],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return False

    run_text = str(run_dir)
    return any('download_run_fullres.py' in line and run_text in line for line in cp.stdout.splitlines())


def _file_time_bounds(folder: Path) -> tuple[datetime | None, datetime | None]:
    first: datetime | None = None
    last: datetime | None = None
    if not folder.exists():
        return first, last
    try:
        for p in folder.iterdir():
            if not p.is_file() or p.suffix.lower() not in {'.jpg', '.jpeg'}:
                continue
            dt = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
            first = dt if first is None or dt < first else first
            last = dt if last is None or dt > last else last
    except Exception:
        pass
    return first, last


def fullres_download_status(run_dir: Path, df: pd.DataFrame) -> dict[str, Any] | None:
    full_dir = run_dir / 'frames_full_jpeg'
    log_path = run_dir / 'fullres_download.log'
    summary = read_json(run_dir / 'fullres_download_summary.json')
    downloaded = count_jpegs(full_dir)
    log_tail = tail_text(log_path, 20000)
    failed_lines = [
        line for line in log_tail.splitlines()
        if 'FAILED frame' in line or 'FATAL' in line or 'Traceback' in line
    ]
    last_failure = failed_lines[-1] if failed_lines else ''

    total = 0
    if not df.empty and 'frame' in df.columns:
        nums = pd.to_numeric(df['frame'], errors='coerce').dropna()
        if not nums.empty:
            total = int(nums.max())
    total = max(total, count_jpegs(run_dir / 'frames_jpeg'))
    try:
        total = max(total, int(summary.get('total_frames') or 0))
    except Exception:
        pass

    if not downloaded and not total and not log_path.exists() and not summary:
        return None

    active = active_fullres_download_process(run_dir)
    complete = bool(total and downloaded >= total)
    state = 'complete' if complete else 'active' if active else 'not running'
    pct = (downloaded / total) if total else 0.0
    retrieval_error = bool(failed_lines and not complete)
    incomplete_stopped = bool(total and downloaded < total and log_path.exists() and not active)

    first_file, last_file = _file_time_bounds(full_dir)
    now = datetime.now().astimezone()
    elapsed_s: int | None = None
    seconds_per_frame: float | None = None
    eta_s: int | None = None
    finish_at: datetime | None = None

    if first_file and downloaded:
        end_time = now if active and not complete else last_file or now
        elapsed_s = max(0, int((end_time - first_file).total_seconds()))
        if elapsed_s > 0:
            seconds_per_frame = elapsed_s / max(downloaded, 1)
            if total and downloaded < total:
                eta_s = int((total - downloaded) * seconds_per_frame)
                finish_at = now + timedelta(seconds=eta_s)

    return {
        'state': state,
        'downloaded': downloaded,
        'total': total,
        'pct': pct,
        'elapsed_s': elapsed_s,
        'seconds_per_frame': seconds_per_frame,
        'eta_s': eta_s,
        'finish_at': finish_at,
        'log_path': str(log_path) if log_path.exists() else '',
        'failed_attempts_recent': len(failed_lines),
        'last_failure': last_failure,
        'retrieval_error': retrieval_error,
        'incomplete_stopped': incomplete_stopped,
    }


def render_fullres_download_status(run_dir: Path, df: pd.DataFrame) -> None:
    status = fullres_download_status(run_dir, df)
    if not status:
        return

    downloaded = int(status['downloaded'])
    total = int(status['total'])
    pct = float(status['pct'])
    pct_label = f'{pct * 100.0:.1f}%' if total else '—'
    count_label = f'{downloaded}/{total}' if total else f'{downloaded}'

    st.markdown('### Full JPEG download')
    cols = st.columns(5)
    cols[0].metric('Status', str(status['state']).title())
    cols[1].metric('Progress', count_label)
    cols[2].metric('Complete', pct_label)
    failed = int(status.get('failed_attempts_recent') or 0)
    cols[3].metric('Retrieval errors', failed)
    eta_s = status.get('eta_s')
    cols[4].metric('ETA', format_countdown(float(eta_s)) if eta_s is not None else '—')
    if total:
        st.progress(min(1.0, max(0.0, pct)))

    if status.get('retrieval_error'):
        last_failure = str(status.get('last_failure') or 'Recent full-JPEG retrieval failed.')
        st.warning(f'Image retrieval error: {last_failure}')
    elif status.get('incomplete_stopped'):
        st.warning('Full-JPEG retrieval is incomplete and no downloader is currently running.')

    detail = []
    elapsed_s = status.get('elapsed_s')
    spf = status.get('seconds_per_frame')
    finish_at = status.get('finish_at')
    if elapsed_s is not None:
        detail.append(f'elapsed {format_countdown(float(elapsed_s))}')
    if spf is not None:
        detail.append(f'average {float(spf):.1f}s/frame')
    if finish_at is not None:
        detail.append(f'estimated finish {finish_at.strftime("%H:%M %Z")}')
    if detail:
        st.caption(' · '.join(detail))


def format_seconds(value: Any) -> str:
    try:
        seconds = float(value)
    except Exception:
        return '—'
    if not math.isfinite(seconds):
        return '—'
    return f'{seconds:.2f}s'


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_active_args(arg_text: str) -> dict[str, str]:
    parts = arg_text.split()
    parsed: dict[str, str] = {}
    i = 0
    while i < len(parts):
        part = parts[i]
        if part.startswith('--'):
            key = part[2:]
            if i + 1 < len(parts) and not parts[i + 1].startswith('--'):
                parsed[key] = parts[i + 1]
                i += 2
                continue
            parsed[key] = 'true'
        i += 1
    return parsed


def run_mode(run_dir: Path) -> str:
    active = active_v5_processes()
    if active:
        active_args = parse_active_args(str(active[0].get('args', '')))
        mode = active_args.get('mode')
        if mode in {'sunrise', 'sunset', 'general'}:
            return mode

    manifest = read_json(run_dir / 'run_manifest.json')
    mode = manifest.get('mode')
    if mode in {'sunrise', 'sunset', 'general'}:
        return str(mode)

    for mode_name in ('sunrise', 'sunset', 'general'):
        if f'_{mode_name}_' in run_dir.name:
            return mode_name
    return '—'


def latest_run_dir() -> Path | None:
    all_runs = list_v5_runs()
    return all_runs[0] if all_runs else None


def run_started_at(run_dir: Path, manifest: dict[str, Any], df: pd.DataFrame) -> datetime | None:
    started = manifest.get('started_at')
    if started:
        try:
            return datetime.fromisoformat(str(started))
        except Exception:
            pass
    if not df.empty and 'time_dt' in df.columns and df['time_dt'].notna().any():
        ts = df['time_dt'].dropna().iloc[0]
        try:
            dt = ts.to_pydatetime()
            return dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except Exception:
            return None
    return None


def timelapse_status_answer(selected_run: Path | None = None) -> str:
    target = latest_run_dir() if active_v5_processes() else selected_run or latest_run_dir()
    if target is None:
        return 'No V5 Wi-Fi run folders found yet.'

    active = active_v5_processes()
    active_args = parse_active_args(str(active[0].get('args', ''))) if active else {}
    manifest = read_json(target / 'run_manifest.json')
    summary = read_json(target / 'run_summary.json')
    df = read_telemetry(target)

    completed = 0
    if not df.empty and 'frame' in df.columns:
        nums = pd.to_numeric(df['frame'], errors='coerce').dropna()
        if not nums.empty:
            completed = int(nums.max())
    completed = max(completed, int(summary.get('downloaded_frames') or 0))

    planned = manifest.get('max_frames') or active_args.get('max-frames') or summary.get('physical_frames_requested') or completed
    try:
        planned = int(float(planned))
    except Exception:
        planned = completed

    interval = manifest.get('interval_seconds') or active_args.get('interval') or parse_interval(target) or 0
    try:
        interval = float(interval)
    except Exception:
        interval = 0.0

    status = run_status(target)
    remaining = max(0, planned - completed)
    started = run_started_at(target, manifest, df)
    now = datetime.now().astimezone()
    ended = None
    if summary.get('written_at'):
        try:
            ended = datetime.fromisoformat(str(summary.get('written_at')))
        except Exception:
            ended = None
    elapsed_to = ended or now
    elapsed_s = max(0, int((elapsed_to - started).total_seconds())) if started else None
    eta_s = int(remaining * interval) if interval else None
    end_time = (now + timedelta(seconds=eta_s)).strftime('%Y-%m-%d %H:%M:%S %Z') if eta_s is not None and status == 'RUNNING' else None

    lines = [
        f'Run: {target.name}',
        f'Status: {status}',
        f'Planned frames: {planned}',
        f'Completed frames: {completed}',
        f'Shots remaining: {remaining}',
    ]
    if interval:
        lines.append(f'Interval: {interval:g}s')
    if elapsed_s is not None:
        lines.append(f'Elapsed: {format_duration(elapsed_s)}')
    if eta_s is not None and status == 'RUNNING':
        lines.append(f'Estimated remaining: {format_duration(eta_s)}')
    if end_time:
        lines.append(f'Estimated end: {end_time}')
    if summary:
        lines.append(f'Exit code: {summary.get("exit_code", "unknown")}')
        lines.append(f'Normal finish: {bool(summary.get("normal_finish"))}')
    return '\n'.join(lines)


def disk_space_answer() -> str:
    usage = shutil.disk_usage(RUNS_ROOT)
    total = float(usage.total)
    free = float(usage.free)
    used = float(usage.used)
    free_pct = (free / total * 100.0) if total else 0.0
    used_pct = (used / total * 100.0) if total else 0.0
    level = 'OK'
    if free_pct < 5:
        level = 'CRITICAL'
    elif free_pct < 10:
        level = 'WARNING'
    elif free_pct < 20:
        level = 'CAUTION'
    return (
        'Timelapse storage:\n'
        f'Free: {format_bytes(free)} ({free_pct:.1f}%)\n'
        f'Used: {format_bytes(used)} ({used_pct:.1f}%)\n'
        f'Total: {format_bytes(total)}\n'
        f'Status: {level}'
    )


def _camera_imglist_url(directory: str) -> str:
    return CAMERA_IMAGE_LIST_URL + '?' + urllib.parse.urlencode({'DIR': directory})


def _read_camera_imglist(directory: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(_camera_imglist_url(directory), method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')


def estimate_camera_sd_used_bytes(directory: str = '/DCIM') -> tuple[int, int]:
    """Estimate SD usage from Olympus file listings without switching camera mode."""
    pending = [directory]
    seen_dirs: set[str] = set()
    used_bytes = 0
    file_count = 0

    while pending:
        current_dir = pending.pop()
        if current_dir in seen_dirs:
            continue
        seen_dirs.add(current_dir)

        try:
            listing = _read_camera_imglist(current_dir)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise

        for line in listing.splitlines():
            if not line or line.startswith('VER_'):
                continue
            components = line.split(',')
            if len(components) != 6:
                continue
            path = '/'.join(components[:2])
            try:
                size = int(components[2])
                attrib = int(components[3])
            except ValueError:
                continue
            if attrib & 16:
                pending.append(path)
            elif not (attrib & 14):
                used_bytes += max(0, size)
                file_count += 1

    return used_bytes, file_count


def camera_sd_capacity_status(active_capture: bool) -> tuple[str, str]:
    cache_key = 'camera_sd_capacity_cache'
    now = datetime.now().astimezone()
    cached = st.session_state.get(cache_key)
    if (
        isinstance(cached, dict)
        and now.timestamp() - float(cached.get('timestamp', 0)) < CAMERA_SD_CAPACITY_TTL_SECONDS
    ):
        return str(cached.get('label', '—')), str(cached.get('detail', 'Cached SD estimate.'))

    if active_capture:
        if isinstance(cached, dict):
            return str(cached.get('label', '—')), 'Cached SD estimate; live refresh paused during capture.'
        return '—', 'Waiting until idle to estimate SD capacity.'

    try:
        used_bytes, file_count = estimate_camera_sd_used_bytes()
        full_pct = used_bytes / float(CAMERA_SD_CARD_TOTAL_BYTES) * 100.0
        label = f'{full_pct:.1f}% full'
        detail = (
            f'Estimated from {file_count} visible file(s): '
            f'{format_bytes(used_bytes)} of {format_bytes(CAMERA_SD_CARD_TOTAL_BYTES)}.'
        )
    except Exception as exc:
        if isinstance(cached, dict):
            return str(cached.get('label', '—')), (
                'Cached SD estimate; refresh failed: '
                f'{type(exc).__name__}: {exc}'
            )
        return '—', f'SD capacity read failed: {type(exc).__name__}: {exc}'

    st.session_state[cache_key] = {
        'timestamp': now.timestamp(),
        'label': label,
        'detail': detail,
    }
    return label, detail


def load_scheduler_data() -> tuple[Any, list[Any], dict[str, Any]]:
    scheduler_path = V5_ROOT / 'timelapser_scheduler.py'
    spec = importlib.util.spec_from_file_location('director_scheduler_view', scheduler_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Cannot load scheduler from {scheduler_path}')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    schedule_mod = mod.load_schedule_module()
    missions = mod.build_missions(schedule_mod)
    state = mod.load_state()
    return schedule_mod, missions, state


def scheduler_answer(question: str) -> str:
    schedule_mod, missions, state = load_scheduler_data()
    tz = ZoneInfo(schedule_mod.TIMEZONE)
    now = datetime.now(tz)
    t = question.lower()
    if 'tomorrow' in t:
        target_date = (now + timedelta(days=1)).date()
        title = f'Scheduled timelapses tomorrow ({target_date})'
        selected = [m for m in missions if m.enabled and m.start_dt.date() == target_date]
    elif 'today' in t:
        target_date = now.date()
        title = f'Scheduled timelapses today ({target_date})'
        selected = [m for m in missions if m.enabled and m.start_dt.date() == target_date]
    else:
        title = 'Next scheduled timelapses'
        selected = [m for m in missions if m.enabled and m.end_dt > now][:5]

    if not selected:
        return f'{title}: none found.'

    events_state = state.get('events', {}) if isinstance(state, dict) else {}
    lines = [title + ':']
    for m in selected[:8]:
        st = events_state.get(m.key, {}) if isinstance(events_state, dict) else {}
        expected = int(max(0, (m.end_dt - m.start_dt).total_seconds()) // float(m.interval))
        if getattr(m, 'max_frames', None) is not None:
            expected = min(expected, int(m.max_frames))
        lines.append(
            f'- {m.key}: {m.mode} {m.start_dt.strftime("%Y-%m-%d %H:%M")} -> '
            f'{m.end_dt.strftime("%H:%M")}, every {m.interval:g}s, ~{expected} frames, '
            f'status {st.get("status", "not_started")}'
        )
    return '\n'.join(lines)


def run_director_preflight() -> str:
    try:
        schedule_mod, missions, _state = load_scheduler_data()
        scheduler_path = V5_ROOT / 'timelapser_scheduler.py'
        spec = importlib.util.spec_from_file_location('director_preflight_runner', scheduler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f'Cannot load scheduler from {scheduler_path}')
        scheduler = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = scheduler
        spec.loader.exec_module(scheduler)
        tz = ZoneInfo(schedule_mod.TIMEZONE)
        now = datetime.now(tz)
        candidates = [m for m in missions if m.enabled and m.end_dt > now]
        if not candidates:
            return 'No active or upcoming enabled timelapse was found.'
        mission = min(candidates, key=lambda item: item.start_dt)
        ok, checks = scheduler.run_preflight(schedule_mod, mission)
        alert = scheduler.write_preflight_alert(mission, checks, ok)
        heading = 'READY' if alert['ready'] else 'ATTENTION NEEDED'
        lines = [
            f'Preflight: **{heading}**',
            f'Next mission: `{mission.key}`',
            f'Capture: {mission.start_dt.strftime("%Y-%m-%d %H:%M %Z")}–{mission.end_dt.strftime("%H:%M %Z")}',
            '',
        ]
        lines.extend(f'- {line}' for line in checks)
        if alert['issues']:
            lines.extend(['', 'Problems detected:', *[f'- {line}' for line in alert['issues']]])
        return '\n'.join(lines)
    except Exception as exc:
        return f'Preflight failed to run: {type(exc).__name__}: {exc}'


def read_preflight_alert() -> dict[str, Any]:
    try:
        data = json.loads(PREFLIGHT_ALERT_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def director_clock_context() -> str:
    try:
        schedule_mod, _missions, _state = load_scheduler_data()
        tz = ZoneInfo(schedule_mod.TIMEZONE)
    except Exception:
        tz = ZoneInfo('Asia/Kuala_Lumpur')
    now = datetime.now(tz)
    return (
        f'Current local time: {now.strftime("%Y-%m-%d %H:%M:%S %Z")}.\n'
        f'Scheduler timezone: {getattr(tz, "key", str(tz))}.\n'
        'Interpret relative scheduling phrases such as "in two minutes" using this clock.'
    )


def next_scheduled_timelapse() -> tuple[str, str]:
    try:
        schedule_mod, missions, _state = load_scheduler_data()
        tz = ZoneInfo(schedule_mod.TIMEZONE)
        now = datetime.now(tz)
        in_progress = [m for m in missions if m.enabled and m.start_dt <= now < m.end_dt]
        if in_progress:
            mission = min(in_progress, key=lambda m: m.end_dt)
            remaining = format_countdown((mission.end_dt - now).total_seconds())
            return f'{mission.mode} in progress', f'Ends in {remaining} at {mission.end_dt.strftime("%H:%M %Z")}'

        upcoming = [m for m in missions if m.enabled and m.start_dt > now]
        if not upcoming:
            return '—', 'No upcoming enabled timelapses found.'
        mission = min(upcoming, key=lambda m: m.start_dt)
        countdown = format_countdown((mission.start_dt - now).total_seconds())
        return countdown, f'{mission.mode} at {mission.start_dt.strftime("%Y-%m-%d %H:%M %Z")}'
    except Exception as exc:
        return '—', f'Scheduler read failed: {type(exc).__name__}: {exc}'


def schedule_edit_answer() -> str:
    return (
        'I can schedule simple one-off timelapses now. Try: '
        '"schedule a timelapse 10min from now", '
        '"schedule a sunset timelapse today at 18:30", or '
        '"schedule a timelapse tomorrow at 07:00 for 30 minutes".'
    )


def _schedule_key(date_s: str, event: str, event_time: str) -> str:
    return f"{date_s}_{event}_{event_time.replace(':', '')}"


def _format_schedule_entry(entry: dict[str, Any]) -> str:
    lines = ["    {"]
    order = [
        "date",
        "event",
        "event_time",
        "enabled",
        "start_offset_minutes",
        "end_offset_minutes",
        "interval",
        "mode",
        "transfer_mode",
        "max_frames",
        "notes",
    ]
    for key in order:
        if key in entry:
            lines.append(f"        {key!r}: {entry[key]!r},")
    lines.append("    },")
    return "\n".join(lines)


def append_schedule_entry(entry: dict[str, Any]) -> None:
    schedule_path = V5_ROOT / 'schedule.py'
    text = schedule_path.read_text(encoding='utf-8')
    marker = '\n]'
    pos = text.rfind(marker)
    if pos < 0:
        raise RuntimeError('Could not find end of SCHEDULE list in schedule.py')
    new_text = text[:pos].rstrip() + "\n" + _format_schedule_entry(entry) + text[pos:]
    tmp = schedule_path.with_suffix('.py.tmp')
    tmp.write_text(new_text, encoding='utf-8')
    os.replace(tmp, schedule_path)


def schedule_future_timelapse(cmd: dict[str, Any]) -> str:
    try:
        schedule_mod, missions, _state = load_scheduler_data()
        tz = ZoneInfo(schedule_mod.TIMEZONE)
    except Exception as exc:
        return f'I could not read the scheduler before editing schedule.py: {type(exc).__name__}: {exc}'

    start_dt = cmd['start_dt'].astimezone(tz)
    existing = {m.key for m in missions}
    mode = cmd['mode']
    while _schedule_key(start_dt.strftime('%Y-%m-%d'), mode, start_dt.strftime('%H:%M')) in existing:
        start_dt += timedelta(minutes=1)

    duration_minutes = max(1, int(math.ceil(float(cmd['duration_seconds']) / 60.0)))
    entry = {
        'date': start_dt.strftime('%Y-%m-%d'),
        'event': mode,
        'event_time': start_dt.strftime('%H:%M'),
        'enabled': True,
        'start_offset_minutes': 0,
        'end_offset_minutes': duration_minutes,
        'interval': float(cmd['interval']),
        'mode': mode,
        'transfer_mode': cmd['transfer_mode'],
        'max_frames': int(cmd['max_frames']),
        'notes': f"Director one-off: {cmd['source_text']}",
    }

    try:
        append_schedule_entry(entry)
        schedule_mod, missions, _state = load_scheduler_data()
    except Exception as exc:
        return f'I could not write or reload schedule.py: {type(exc).__name__}: {exc}'

    key = _schedule_key(entry['date'], entry['event'], entry['event_time'])
    mission = next((m for m in missions if m.key == key), None)
    if mission is None:
        return f'I wrote the schedule entry, but could not find it after reload: {key}'

    clamp_note = ''
    if cmd.get('duration_clamped'):
        clamp_note = "\nNote: duration was capped at " + format_duration(cmd["duration_seconds"]) + "."

    return (
        "Scheduled future timelapse:\n\n"
        f"Key: {mission.key}\n"
        f"Mode: {mission.mode}\n"
        f"Capture: {mission.start_dt.strftime('%Y-%m-%d %H:%M %Z')} -> {mission.end_dt.strftime('%H:%M %Z')}\n"
        f"Duration: {format_duration(cmd['duration_seconds'])}\n"
        f"Interval: {mission.interval:g}s\n"
        f"Transfer: {mission.transfer_mode}\n"
        f"Expected frames: ~{int(mission.max_frames or cmd['max_frames'])}\n"
        "The scheduler reloads schedule.py automatically, so no restart is needed."
        f"{clamp_note}"
    )


def handle_basic_status_query(text: str) -> str | None:
    t = text.lower()
    if re.search(r'\b(pre[ -]?flight|readiness|ready for|check before)\b', t):
        return run_director_preflight()
    if re.search(r'\b(disk|drive|storage|space)\b', t) and re.search(r'\b(left|free|available|percent|%)\b', t):
        return disk_space_answer()
    if re.search(r'\b(shots?|frames?)\b', t) and re.search(r'\b(left|remaining|planned|complete|done)\b', t):
        return timelapse_status_answer(run_dir)
    if re.search(r'\b(status|progress|eta|remaining|running|current run)\b', t) and 'schedule' not in t:
        return timelapse_status_answer(run_dir)
    if re.search(r'\b(next|scheduled|schedule|today|tomorrow)\b', t) and 'timelapse' in t:
        if re.search(r'\b(add|create|schedule|plan)\b', t) and re.search(r'\b(at|on|tomorrow|today|\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2})\b', t) and 'next' not in t:
            return schedule_edit_answer()
        try:
            return scheduler_answer(text)
        except Exception as exc:
            return f'Scheduler read failed: {type(exc).__name__}: {exc}'
    return None


def start_timelapse_now(cmd: dict[str, Any]) -> str:
    active = active_v5_processes()
    if active:
        return f"A timelapse is already running under PID {active[0]['pid']}. I did not start another one."

    ok, preflight = camera_api_preflight()
    append_control_log({'action': 'start_preflight', 'ok': ok, 'camera_api': preflight})
    if not ok:
        return (
            "I did not start the timelapse because the Olympus camera API is not reachable.\n\n"
            f"Camera check: {preflight}\n\n"
            "The camera Wi-Fi may be off, the Pi may not be joined to the camera SSID on wlan1, "
            "or the camera may still be waking up."
        )

    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    if STOP_REQUEST_FILE.exists():
        STOP_REQUEST_FILE.unlink()

    command = [
        str(PYTHON),
        str(TIMELAPSER_SCRIPT),
        '--mode', cmd['mode'],
        '--interval', str(cmd['interval']),
        '--max-frames', str(cmd['max_frames']),
        '--max-run-minutes', str(max(1.0, cmd['duration_seconds'] / 60.0 + 1.0)),
        '--transfer-mode', cmd['transfer_mode'],
        '--post-download-fullres',
        '--post-render',
        '--post-copy-videos-to-mac',
    ]
    log_f = DIRECTOR_CONTROL_LOG.open('a', encoding='utf-8')
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(V5_ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_f.close()
    append_control_log({
        'action': 'start_now',
        'pid': proc.pid,
        'command': command,
        'duration_clamped': cmd.get('duration_clamped', False),
    })
    expected = cmd['max_frames']
    baselines = {
        'general': GENERAL_BASELINE,
        'sunrise': SUNRISE_BASELINE,
        'sunset': SUNSET_BASELINE,
    }
    baseline = baselines.get(cmd['mode'], GENERAL_BASELINE)
    clamp_note = ''
    if cmd.get('duration_clamped'):
        clamp_note = "\nNote: duration was capped at " + format_duration(cmd["duration_seconds"]) + "."
    label = 'General timelapse' if cmd['mode'] == 'general' else f"{cmd['mode'].title()} timelapse"
    return (
        "Scheduled now:\n\n"
        f"{label}"
        f"\nStart: now\n"
        f"Duration: {format_duration(cmd['duration_seconds'])}\n"
        f"Interval: {cmd['interval']:g}s\n"
        f"Transfer: {cmd['transfer_mode']}\n"
        "Post-run: download full JPEGs, render videos, copy videos to Mac\n"
        f"Expected frames: ~{expected}\n"
        f"Starting exposure: ISO {baseline['iso']} · f/{baseline['aperture']:g} · {baseline['shutter_seconds']:g}s\n"
        f"Lens/control: {LENS_PROFILE_NAME}; physical widest f/{LENS_MIN_APERTURE:g}, commandable floor f/{MIN_APERTURE:g}\n"
        f"PID: {proc.pid}"
        f"{clamp_note}"
    )


def request_timelapse_stop(reason: str) -> str:
    active = active_v5_processes()
    if not active:
        return 'No active Timelapser V5 capture process is running, so there is nothing to stop.'

    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        'reason': reason,
        'requested_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'source': 'timelapser_director_v5',
        'active_pids': [p['pid'] for p in active],
    }
    STOP_REQUEST_FILE.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    append_control_log({'action': 'stop_requested', **payload})
    return (
        "Graceful stop requested.\n\n"
        "The running timelapser will finish any in-flight camera operation and stop before starting another exposure."
    )


def handle_operator_command(text: str, current_run_dir: Path) -> str | None:
    cmd = parse_operator_command(text)
    if cmd is None:
        return None
    if cmd['action'] == 'unsupported_future_start':
        return schedule_edit_answer()
    if cmd['action'] == 'test_shot':
        return show_test_shot(current_run_dir)
    if cmd['action'] == 'stop':
        return request_timelapse_stop(cmd['reason'])
    if cmd['action'] == 'schedule_future':
        return schedule_future_timelapse(cmd)
    if cmd['action'] == 'start_now':
        return start_timelapse_now(cmd)
    return None


def wlan1_status() -> tuple[str, str]:
    try:
        cp = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        for line in cp.stdout.splitlines():
            parts = line.split(':', 3)
            if parts and parts[0] == 'wlan1':
                state = parts[2] if len(parts) > 2 else 'unknown'
                connection = parts[3] if len(parts) > 3 else ''
                return state, connection
    except Exception:
        pass
    return 'unknown', ''


def wlan1_indicator(state: str, connection: str) -> tuple[str, str]:
    normalized = state.lower().strip()
    if normalized == 'connected':
        return '🟢', connection or 'connected'
    if normalized in {'connecting', 'config', 'prepare', 'need-auth', 'ip-config', 'checking'}:
        return '🟡', state
    return '🔴', state or 'unknown'


def parse_interval(run_dir: Path) -> float | None:
    text = tail_text(run_dir / 'run.log', 12000)
    matches = re.findall(r'Capture interval:\s+([0-9.]+)s', text)
    return float(matches[-1]) if matches else None


def format_shutter(seconds: Any) -> str:
    try:
        s = float(seconds)
    except Exception:
        return '—'
    if not math.isfinite(s) or s <= 0:
        return '—'
    if s >= 1:
        return f'{s:g}s'
    denom = round(1.0 / s)
    return f'1/{denom}s' if denom else f'{s:.4f}s'


def latest_ai_decision(run_dir: Path) -> dict[str, Any]:
    rows = read_jsonl_tail(run_dir / 'ai_decisions.jsonl', 30)
    return rows[-1] if rows else {}


def ai_display(decision: dict[str, Any]) -> tuple[str, str, str]:
    if not decision:
        return 'No AI decision yet', '', ''
    raw = decision.get('raw')
    summary = ''
    if isinstance(raw, dict):
        summary = str(raw.get('summary', '') or '')
    actions = decision.get('actions') or []
    action_label = 'NOOP / observation'
    reason = ''
    target = ''
    if actions and isinstance(actions, list) and isinstance(actions[-1], dict):
        a = actions[-1]
        action_label = str(a.get('action', 'NOOP'))
        reason = str(a.get('reason', '') or '')
        if action_label == 'SET_EXPOSURE':
            target = (
                f"ISO {a.get('iso', '—')} · f/{a.get('aperture', '—')} · "
                f"{format_shutter(a.get('shutter_seconds'))}"
            )
    return action_label, summary or reason, target


def run_status(run_dir: Path) -> str:
    summary = run_dir / 'run_summary.json'
    if summary.exists():
        try:
            data = json.loads(summary.read_text(encoding='utf-8'))
            if data.get('normal_finish'):
                return 'FINISHED'
        except Exception:
            return 'FINISHED'
    return 'RUNNING' if v5_process_running() else 'IDLE / STOPPED'


def chart_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    plot = df.tail(MAX_CHART_ROWS).copy()
    if 'time_dt' in plot.columns and plot['time_dt'].notna().any():
        plot = plot.dropna(subset=['time_dt']).set_index('time_dt')
    elif 'frame' in plot.columns:
        plot = plot.set_index('frame')
    return plot


def collect_context(run_dir: Path) -> str:
    df = read_telemetry(run_dir)
    telemetry_tail = df.tail(60).to_dict(orient='records') if not df.empty else []
    payload = {
        'run_directory': str(run_dir),
        'run_status': run_status(run_dir),
        'wlan1': wlan1_status(),
        'capture_interval_seconds': parse_interval(run_dir),
        'telemetry_tail': telemetry_tail,
        'recent_events': read_jsonl_tail(run_dir / 'events.jsonl', 60),
        'recent_ai_decisions': read_jsonl_tail(run_dir / 'ai_decisions.jsonl', 20),
        'recent_errors': read_jsonl_tail(run_dir / 'errors.jsonl', 20),
        'run_summary': tail_text(run_dir / 'run_summary.json', 8000),
        'run_log_tail': tail_text(run_dir / 'run.log', 12000),
    }
    return json.dumps(payload, default=str, ensure_ascii=False)


runs = list_v5_runs()
if not runs:
    st.title('📷 Timelapser V5 Director')
    st.warning('No V5 Wi-Fi run folders found yet.')
    st.caption(f'Looking below {RUNS_ROOT}, {V5_ROOT}, and {PROJECT_ROOT}.')
    st.stop()

run_names = [p.name for p in runs]
selected_name = st.sidebar.selectbox('Run', run_names, index=0)
run_dir = runs[run_names.index(selected_name)]

st.sidebar.markdown('### Director')
st.sidebar.caption('Observer plus constrained start/stop controls. Camera commands remain owned by timelapser_v5.py.')
st.sidebar.caption(f'Auto-refresh: {REFRESH_SECONDS}s')
st.sidebar.code(str(run_dir), language=None)

st.title('📷 Timelapser V5 Director')
st.caption('Live Wi-Fi timelapse telemetry, image analysis, exposure history and AI reasoning.')

preflight_alert = read_preflight_alert()
if preflight_alert and not preflight_alert.get('ready', False):
    issues = preflight_alert.get('issues') or []
    checked_at = preflight_alert.get('checked_at', 'unknown time')
    mission_key = preflight_alert.get('mission_key', 'next mission')
    detail = '\n'.join(f'- {item}' for item in issues) or '- Preflight did not report a ready state.'
    st.error(f'Preflight alert for {mission_key} ({checked_at})\n\n{detail}')


def render_live_panel():
    df = read_telemetry(run_dir)
    latest = df.iloc[-1].to_dict() if not df.empty else {}
    status = run_status(run_dir)
    mode = run_mode(run_dir)
    wifi_state, wifi_connection = wlan1_status()
    wifi_dot, _wifi_detail = wlan1_indicator(wifi_state, wifi_connection)
    next_label, next_detail = next_scheduled_timelapse()
    interval = parse_interval(run_dir)
    ai_decision = latest_ai_decision(run_dir)
    ai_action, ai_summary, ai_target = ai_display(ai_decision)
    errors = read_jsonl_tail(run_dir / 'errors.jsonl', 200)

    frame_no = int(latest.get('frame', 0)) if latest else 0
    iso = latest.get('iso', '—') if latest else '—'
    aperture = latest.get('aperture', '—') if latest else '—'
    shutter = format_shutter(latest.get('shutter_seconds')) if latest else '—'
    median = latest.get('median', None) if latest else None
    cycle = latest.get('capture_cycle_seconds', None) if latest else None
    worst_cycle = None
    if not df.empty and 'capture_cycle_seconds' in df.columns:
        cycles = pd.to_numeric(df['capture_cycle_seconds'], errors='coerce').dropna()
        if not cycles.empty:
            worst_cycle = float(cycles.max())
    sd_label, sd_detail = camera_sd_capacity_status(status == 'RUNNING')

    st.metric('Run', status)
    st.metric('Mode', mode.title() if mode != '—' else '—')
    st.metric('Next scheduled timelapse', next_label)
    st.caption(next_detail)
    st.metric('Camera SD Card capacity', sd_label)
    st.caption(sd_detail)
    render_fullres_download_status(run_dir, df)

    top = st.columns(5)
    top[0].metric('Frame', frame_no if frame_no else '—')
    top[1].metric('ISO', iso)
    top[2].metric('Aperture', f'f/{aperture}' if aperture != '—' else '—')
    top[3].metric('Shutter', shutter)
    top[4].metric('Median brightness', f'{float(median):.3f}' if median is not None else '—')

    second = st.columns(5)
    second[0].metric('wlan1', wifi_dot)
    second[1].metric('Target interval', f'{interval:.1f}s' if interval is not None else '—')
    second[2].metric('Last cycle', format_seconds(cycle))
    second[3].metric('Worst cycle', format_seconds(worst_cycle))
    second[4].metric('Logged errors', len(errors))

    cleanup_test_shots_if_frames_deleted(run_dir)
    test_image_path = display_test_shot_for_run(run_dir)
    hidden_test_shot = st.session_state.get('hidden_test_shot_path')
    if test_image_path and test_image_path.exists() and str(test_image_path) != hidden_test_shot:
        test_summary = latest_test_shot_summary(test_image_path)
        st.markdown('### Test shot')
        st.caption('Final post-adjustment full JPEG, rotated for the portrait camera mount.')
        st.image(rotated_image(test_image_path), caption=str(test_image_path), use_container_width=True)
        if st.button('Hide test shot', key=f'hide_test_shot::{test_image_path}'):
            st.session_state['hidden_test_shot_path'] = str(test_image_path)
            st.rerun()
        if test_summary:
            note = test_summary.get('exposure_note')
            outcome = test_summary.get('exposure_outcome') or {}
            applied = outcome.get('applied') if isinstance(outcome, dict) else {}
            if isinstance(applied, dict) and applied:
                st.caption(
                    f"Applied: ISO {applied.get('iso', '—')} · "
                    f"f/{applied.get('aperture', '—')} · "
                    f"{format_shutter(applied.get('shutter_seconds'))}"
                )
            if note:
                st.caption(f'AI note: {note}')

    st.markdown('### Latest frame')
    image = latest_jpeg(run_dir)
    img_col, ai_col = st.columns([1.55, 1])
    with img_col:
        if image:
            st.image(rotated_image(image), caption=image.name, use_container_width=True)
        else:
            st.info('No downloaded JPEG yet.')
    with ai_col:
        st.markdown('#### AI Director')
        st.metric('Latest action', ai_action)
        if ai_target:
            st.markdown(f'**Target:** {ai_target}')
        st.write(ai_summary or 'Waiting for the first AI review.')
        trigger = ai_decision.get('trigger') if ai_decision else None
        when = ai_decision.get('time') if ai_decision else None
        if trigger:
            st.caption(f'Trigger: {trigger}')
        if when:
            st.caption(f'Decision logged: {when}')

    if df.empty:
        st.info('Waiting for telemetry.csv to receive its first frame.')
        return

    plot = chart_index(df)
    st.markdown('### Light over time')
    c1, c2 = st.columns(2)
    with c1:
        st.caption('Median scene brightness — the primary exposure trend')
        if 'median' in plot.columns:
            st.line_chart(plot[['median']], height=280)
    with c2:
        st.caption('Highlight and shadow pressure (%)')
        cols = [c for c in ('highlights_pct', 'shadows_pct') if c in plot.columns]
        if cols:
            st.line_chart(plot[cols], height=280)

    st.markdown('### Exposure history')
    e1, e2, e3 = st.columns(3)
    with e1:
        st.caption('Shutter duration (seconds)')
        if 'shutter_seconds' in plot.columns:
            st.line_chart(plot[['shutter_seconds']], height=220)
    with e2:
        st.caption('ISO')
        if 'iso' in plot.columns:
            st.line_chart(plot[['iso']], height=220)
    with e3:
        st.caption('Aperture (f-number)')
        if 'aperture' in plot.columns:
            st.line_chart(plot[['aperture']], height=220)

    if 'capture_cycle_seconds' in plot.columns:
        st.markdown('### Capture-cycle performance')
        perf = plot[['capture_cycle_seconds']].copy()
        st.line_chart(perf, height=220)
        median_cycle = pd.to_numeric(df['capture_cycle_seconds'], errors='coerce').median()
        if interval and pd.notna(median_cycle):
            headroom = interval - float(median_cycle)
            st.caption(
                f'Median capture/download/analyse cycle: {float(median_cycle):.2f}s · '
                f'median cadence headroom: {headroom:.2f}s'
            )

    with st.expander('Latest telemetry row'):
        st.json(latest)
    with st.expander('Recent AI decisions'):
        rows = read_jsonl_tail(run_dir / 'ai_decisions.jsonl', 12)
        st.json(rows if rows else {'status': 'No AI decisions logged yet.'})
    with st.expander('Recent errors'):
        st.json(errors[-20:] if errors else {'status': 'No errors logged.'})
    with st.expander('Recent run log'):
        st.code(tail_text(run_dir / 'run.log', 16000) or 'No run.log yet.')


# Streamlit >= 1.37 supports fragments that refresh without disturbing chat input.
if hasattr(st, 'fragment'):
    live_fragment = st.fragment(run_every=REFRESH_SECONDS)(render_live_panel)
    live_fragment()
else:
    render_live_panel()
    st.info('Upgrade Streamlit for automatic 5-second dashboard refresh.')

st.divider()

chat_key = f'messages::{run_dir}'
if chat_key not in st.session_state:
    st.session_state[chat_key] = []

with st.expander('Ask the AI Director', expanded=False):
    st.caption('Chat over telemetry, run a preflight for the next mission, start a timelapse now, or stop the current timelapse gracefully.')

    for msg in st.session_state[chat_key]:
        with st.chat_message(msg['role']):
            st.markdown(msg['content'])

    with st.form(f'director_question_form::{run_dir}', clear_on_submit=True):
        question = st.text_area(
            'Question',
            placeholder='Ask what the sunset is doing, why exposure changed, whether cadence is healthy...',
            height=90,
            key=f'director_question::{run_dir}',
        )
        submitted = st.form_submit_button('Send')

    if submitted and question.strip():
        question = question.strip()
        st.session_state[chat_key].append({'role': 'user', 'content': question})
        with st.chat_message('user'):
            st.markdown(question)

        operator_answer = handle_operator_command(question, run_dir)
        basic_answer = None if operator_answer is not None else handle_basic_status_query(question)
        if operator_answer is not None:
            answer = operator_answer
        elif basic_answer is not None:
            answer = basic_answer
        else:
            api_key = os.getenv('OPENAI_API_KEY')
            if not api_key:
                answer = f'OPENAI_API_KEY was not found in {ENV_FILE}.'
            else:
                try:
                    from openai import OpenAI

                    client = OpenAI(api_key=api_key)
                    instructions = '''You are the Timelapser V5 Director for an Olympus camera controlled by a Raspberry Pi over the Olympus Wi-Fi protocol.

Use ONLY the supplied current run state, telemetry, image metrics, logs, errors and recorded AI decisions for factual claims about this run. You may explain photographic concepts when useful.

V5 facts:
- Current clock and timezone are supplied in CURRENT DIRECTOR CLOCK below.
- wlan0 is the dedicated Olympus Wi-Fi link; wlan1 remains the normal internet/LAN connection.
- The camera records RAW+JPEG. ORF remains on SD; JPEG is downloaded to the Pi for analysis.
- The deterministic Python process alone controls the camera.
- AI proposes high-level exposure intentions; Python applies deterministic guardrails.
- Lens profile is M.Zuiko 75-300mm f/4.8-6.7. Olympus Wi-Fi rejects f/4.8 as a command value, so V5 uses f/5.0 as the commandable aperture floor. Do not ask for or recommend f/2.8 or f/4.8 over Wi-Fi for this profile.
- Operator preference: after about 1/4s, V5 should prefer raising ISO before making shutter longer unless ISO is already near its limit.
- Median brightness, highlights_pct and shadows_pct come from each downloaded JPEG.
- This Director may start an ad-hoc validated timelapser_v5.py run or request graceful stop through a control file.
- The Director still has no direct camera authority and never sends Olympus camera commands itself.

Be concise but technically useful. If evidence is missing, say so.'''
                    response = client.responses.create(
                        model='gpt-5.6-luna',
                        instructions=instructions,
                        input=(
                            f'CURRENT DIRECTOR CLOCK:\n{director_clock_context()}\n\n'
                            f'CURRENT V5 RUN DATA:\n{collect_context(run_dir)}\n\n'
                            f'OPERATOR QUESTION:\n{question}'
                        ),
                    )
                    answer = response.output_text
                except Exception as exc:
                    answer = f'AI request failed: {type(exc).__name__}: {exc}'

        with st.chat_message('assistant'):
            st.markdown(answer)
        st.session_state[chat_key].append({'role': 'assistant', 'content': answer})
