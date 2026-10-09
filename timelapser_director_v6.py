#!/usr/bin/env python3
"""Small operational dashboard for wired Timelapser V6."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import v6_config


st.set_page_config(page_title="Timelapser V6", page_icon="camera", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 980px; padding: 1rem 0.8rem 3rem;}
    [data-testid="stMetricValue"] {font-size: 1.35rem;}
    @media (max-width: 600px) {
      .block-container {padding: 0.55rem 0.55rem 2rem;}
      h1 {font-size: 1.75rem !important;}
      [data-testid="stHorizontalBlock"] {gap: 0.35rem;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def output_root() -> Path:
    return v6_config.DEFAULT_OUTPUT_ROOT


def runs() -> list[Path]:
    root = output_root()
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.glob("*_v6_wired*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def count_rows(path: Path) -> int:
    try:
        return max(0, sum(1 for _ in path.open(encoding="utf-8")) - 1)
    except OSError:
        return 0


st.title("Timelapser V6")
st.caption("Wired camera control. Image analysis begins after capture, not during it.")

paused = v6_config.SCHEDULER_PAUSE_FILE.exists()
with st.sidebar:
    st.subheader("Capture scheduler")
    st.warning("Paused") if paused else st.success("Enabled")
    if st.button("Resume" if paused else "Pause", use_container_width=True):
        if paused:
            v6_config.SCHEDULER_PAUSE_FILE.unlink(missing_ok=True)
        else:
            v6_config.SCHEDULER_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            v6_config.SCHEDULER_PAUSE_FILE.write_text('{"paused": true}\n', encoding="utf-8")
        st.rerun()
    st.caption("Camera Wi-Fi is not used.")

available = runs()
if not available:
    st.info(f"No V6 wired runs yet under {output_root()}")
    st.stop()

names = [path.name for path in available]
selected = st.selectbox("Run", names, index=0, label_visibility="collapsed")
run_dir = available[names.index(selected)]
manifest = read_json(run_dir / "run_manifest.json")
post = read_json(run_dir / "postprocess_workflow_summary.json")
live = read_json(run_dir / "postprocess_live_status.json")
completed = int(manifest.get("completed_frames") or count_rows(run_dir / "trigger_timeline.csv"))
planned = int(manifest.get("max_frames") or 0)

cols = st.columns(4)
cols[0].metric("State", manifest.get("status", "unknown").replace("_", " ").title())
cols[1].metric("Mode", str(manifest.get("mode", "unknown")).title())
cols[2].metric("Triggers", f"{completed}/{planned}" if planned else str(completed))
cols[3].metric("Interval", f"{float(manifest.get('interval_seconds', 0)):g}s")

if planned:
    st.progress(min(1.0, completed / planned))
if manifest.get("error"):
    st.error(str(manifest["error"]))

st.subheader("Camera control")
control_cols = st.columns(3)
control_cols[0].metric("Capture path", "Wired relay")
control_cols[1].metric("Autofocus", "Once at startup" if manifest.get("autofocus_once") else "Skipped")
control_cols[2].metric("Live images", "Off")
st.caption(
    f"AF GPIO {manifest.get('focus_gpio', 'not configured')} | "
    f"RLS GPIO {manifest.get('shutter_gpio', 'not configured')} | "
    f"last trigger {read_json(v6_config.ACTIVE_RUN).get('last_trigger_time', 'not available')}"
)

st.subheader("Post-run workflow")
workflow_state = post.get("status") or live.get("status") or "waiting for capture"
st.write(workflow_state.replace("_", " ").title())
if live.get("current_step"):
    st.caption(f"Current stage: {str(live['current_step']).replace('_', ' ')}")

# V6 intentionally withholds exposure and brightness history until the entire
# workflow, including publishing, succeeds. There is no partial/live graph.
if post.get("status") == "complete" and post.get("graphs_released"):
    telemetry_path = run_dir / "telemetry.csv"
    if telemetry_path.exists():
        data = pd.read_csv(telemetry_path)
        st.subheader("Completed-run image data")
        graph_cols = [name for name in ("median", "actual_iso", "actual_shutter_seconds", "actual_aperture") if name in data]
        if graph_cols:
            st.line_chart(data[graph_cols], height=260)
        st.caption("Extracted from full JPEGs after USB import; publishing is complete.")
else:
    st.info("Exposure and brightness graphs unlock after download, rendering, and upload all complete.")

with st.expander("Run files"):
    st.code(str(run_dir), language=None)
    st.json({"manifest": manifest, "postprocess": post})
