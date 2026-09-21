#!/usr/bin/env python3
"""
Timelapser Video Renderer V3
============================
Portrait-first Instagram renderer for Timelapser V5.x with optional telemetry overlays. V3.1.

Default outputs:
  1) clean      - normal Instagram Reel timelapse
  2) brightness - same timelapse + a LIVE scene-brightness graph
  3) director   - same timelapse + exposure/scene telemetry + brightness graph

Overlay layout and typography live in overlay_config.py and are intentionally
user-editable. Source JPEGs are never modified.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from v5_overlay_context import run_bounds, solar_markers

DEFAULT_FPS = 30.0


@dataclass(frozen=True)
class ExpectedFrame:
    frame: int
    remote_jpg: str
    basename: str


def read_telemetry(run_dir: Path) -> tuple[list[ExpectedFrame], list[dict]]:
    telemetry = run_dir / "telemetry.csv"
    if not telemetry.exists():
        raise FileNotFoundError(f"Missing telemetry.csv: {telemetry}")

    pairs: list[tuple[ExpectedFrame, dict]] = []
    with telemetry.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"frame", "remote_jpg"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"telemetry.csv missing columns: {sorted(missing)}")
        for raw in reader:
            fr = str(raw.get("frame", "")).strip()
            remote = str(raw.get("remote_jpg", "")).strip()
            if not fr or not remote:
                continue
            frame = int(float(fr))
            pairs.append((ExpectedFrame(frame, remote, Path(remote).name), dict(raw)))

    if not pairs:
        raise ValueError("No production frame mappings found in telemetry.csv")
    pairs.sort(key=lambda x: x[0].frame)
    expected = [p[0] for p in pairs]
    rows = [p[1] for p in pairs]

    nums = [x.frame for x in expected]
    expected_nums = list(range(nums[0], nums[-1] + 1))
    if nums != expected_nums:
        missing = sorted(set(expected_nums) - set(nums))
        raise ValueError(f"Telemetry frame sequence is not contiguous. Missing: {missing[:30]}")
    names = [x.basename.lower() for x in expected]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate Olympus JPEG basenames found in telemetry.csv")
    return expected, rows


def build_source_index(source: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for p in source.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg"}:
            index.setdefault(p.name.lower(), []).append(p)
    return index


def clear_stage(stage: Path):
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)


def stage_sequence(expected, index, stage: Path):
    missing, ambiguous, manifest = [], [], []
    for item in expected:
        # Prefer untouched Olympus basenames, but also accept the post-run
        # downloader's frame_000001.jpg naming documented in README_RENDER.
        lookup_names = [item.basename.lower(), f"frame_{item.frame:06d}.jpg"]
        matches = []
        for name in lookup_names:
            matches = index.get(name, [])
            if matches:
                break
        if not matches:
            missing.append(item); continue
        if len(matches) > 1:
            ambiguous.append((item, matches)); continue
        source_path = matches[0]
        staged = stage / f"frame_{item.frame:06d}.jpg"
        os.symlink(source_path.resolve(), staged)
        manifest.append({
            "frame": item.frame, "remote_jpg": item.remote_jpg,
            "source_file": str(source_path.resolve()), "staged_file": str(staged.resolve()),
            "bytes": source_path.stat().st_size,
        })
    return manifest, missing, ambiguous


def write_manifest(path: Path, rows: list[dict]):
    fields = ["frame", "remote_jpg", "source_file", "staged_file", "bytes"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)


def profile_defaults(name: str):
    profiles = {
        "instagram-reel": dict(width=1080, height=1920, framing="crop", fps=30.0, crf=18, preset="veryfast"),
        "instagram-feed": dict(width=1080, height=1350, framing="crop", fps=30.0, crf=18, preset="veryfast"),
        "portrait4k": dict(width=2160, height=3840, framing="crop", fps=30.0, crf=17, preset="veryfast"),
        "4k": dict(width=3840, height=2160, framing="crop", fps=30.0, crf=17, preset="veryfast"),
        "native43": dict(width=2880, height=2160, framing="contain", fps=30.0, crf=17, preset="veryfast"),
    }
    return profiles[name].copy()


def filter_chain(mode: str, width: int, height: int) -> str:
    if mode == "crop":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}"
    if mode == "contain":
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    raise ValueError(mode)


def encoder_args(crf: int, preset: str, width: int, height: int):
    # H.264 level 4.2 is suitable for 1080-class outputs; use 5.1 for 4K-class output.
    level = "5.1" if width * height > 1920 * 1080 else "4.2"
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-level", level, "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-threads", "0"]


def load_overlay_config(path: Path):
    spec = importlib.util.spec_from_file_location("timelapser_overlay_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load overlay config: {path}")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def num(row: dict, *names, default=float("nan")):
    for name in names:
        value = row.get(name)
        if value is None or str(value).strip() == "":
            continue
        try: return float(value)
        except (TypeError, ValueError): continue
    return default


def textval(row: dict, *names, default=""):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip(): return str(value).strip()
    return default


def brightness_series(rows: list[dict]) -> list[float]:
    aliases = ("median", "median_brightness", "brightness_median", "luminance_median")
    values = [num(r, *aliases) for r in rows]
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        raise ValueError("No brightness metric found in telemetry.csv (expected e.g. 'median').")
    # Fill isolated missing values with prior value, then first finite value.
    first = finite[0]; last = first; out = []
    for v in values:
        if math.isfinite(v): last = v
        out.append(last)
    return out


def brightness_series_from_sources(manifest: list[dict], max_edge: int = 512) -> list[float]:
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError:
        raise SystemExit("Frame brightness analysis requires Pillow and NumPy")

    values = []
    for index, item in enumerate(manifest, start=1):
        source = Path(str(item["source_file"]))
        image = Image.open(source)
        image = ImageOps.exif_transpose(image).convert("RGB")
        if max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.BICUBIC)
        arr = np.asarray(image, dtype=np.float32) / 255.0
        linear = np.where(arr <= 0.04045, arr / 12.92, ((arr + 0.055) / 1.055) ** 2.4)
        lum = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
        values.append(float(np.median(lum)))
        if index % 100 == 0 or index == len(manifest):
            print(f"[{index}/{len(manifest)}] measured frame brightness")
    return values


def fmt_shutter(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds <= 0: return "—"
    if seconds >= 1: return f"{seconds:g}s"
    denom = round(1.0 / seconds)
    return f"1/{denom}s" if denom else f"{seconds:.4f}s"


def enrich_rows_with_exif(rows: list[dict], manifest: list[dict], run_dir: Path) -> str:
    """Prefer actual JPEG EXIF exposure for overlays when full-res files exist."""
    if not rows or not manifest:
        return "telemetry"

    files: list[str] = []
    source_to_frame: dict[str, int] = {}
    for item in manifest:
        try:
            frame = int(item["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        full_jpeg = run_dir / "frames_full_jpeg" / f"frame_{frame:06d}.jpg"
        source = str(full_jpeg if full_jpeg.is_file() else item.get("source_file") or "")
        if not source:
            continue
        files.append(source)
        source_to_frame[source] = frame

    row_by_frame: dict[int, dict] = {}
    for row in rows:
        try:
            row_by_frame[int(float(str(row.get("frame", "")).strip()))] = row
        except (TypeError, ValueError):
            continue

    def enrich_with_pillow() -> int:
        try:
            from PIL import Image
        except ImportError:
            return 0
        count = 0
        for source, frame in source_to_frame.items():
            row = row_by_frame.get(frame)
            if row is None:
                continue
            try:
                with Image.open(source) as image:
                    exif = image.getexif().get_ifd(0x8769)
                exposure = exif.get(33434)
                aperture = exif.get(33437)
                iso = exif.get(34855)
                if iso is not None:
                    row["actual_iso"] = float(iso)
                if aperture is not None:
                    row["actual_aperture"] = float(aperture)
                if exposure is not None:
                    row["actual_shutter_seconds"] = float(exposure)
            except (OSError, TypeError, ValueError, AttributeError):
                continue
            if any(k in row for k in ("actual_iso", "actual_aperture", "actual_shutter_seconds")):
                count += 1
        return count

    exiftool = shutil.which("exiftool")
    if exiftool is None:
        return "exif" if enrich_with_pillow() else "telemetry"

    enriched = 0
    batch_size = 200
    for start in range(0, len(files), batch_size):
        cmd = [
            exiftool,
            "-j",
            "-n",
            "-ExposureTime",
            "-FNumber",
            "-ISO",
            *files[start:start + batch_size],
        ]
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
            if proc.returncode != 0:
                continue
            payload = json.loads(proc.stdout or "[]")
        except Exception:
            continue
        for exif in payload:
            source = str(exif.get("SourceFile") or "")
            row = row_by_frame.get(source_to_frame.get(source, -1))
            if row is None:
                continue
            if exif.get("ISO") is not None:
                row["actual_iso"] = exif["ISO"]
            if exif.get("FNumber") is not None:
                row["actual_aperture"] = exif["FNumber"]
            if exif.get("ExposureTime") is not None:
                row["actual_shutter_seconds"] = exif["ExposureTime"]
            if any(k in row for k in ("actual_iso", "actual_aperture", "actual_shutter_seconds")):
                enriched += 1
    if not enriched:
        enriched = enrich_with_pillow()
    return "exif" if enriched else "telemetry"


def short_time(row: dict) -> str:
    raw = textval(row, "time", "timestamp")
    if not raw: return "—"
    try:
        return datetime.fromisoformat(raw).strftime("%H:%M:%S")
    except Exception:
        return raw[-12:-4] if len(raw) >= 8 else raw


def clock_label(row: dict) -> str:
    raw = textval(row, "time", "timestamp")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%H:%M")
    except Exception:
        return raw[-8:-3] if len(raw) >= 5 else raw


def date_label(rows: list[dict]) -> str:
    for row in rows:
        raw = textval(row, "time", "timestamp")
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw).strftime("%d %b %Y").lstrip("0")
        except Exception:
            continue
    return ""


def _safe_rect(cfg, width, height):
    left = int(round(width * cfg.MARGIN_LEFT)); right = int(round(width * (1-cfg.MARGIN_RIGHT)))
    top = int(round(height * cfg.MARGIN_TOP)); bottom = int(round(height * (1-cfg.MARGIN_BOTTOM)))
    if right <= left or bottom <= top: raise ValueError("Overlay margins leave no usable safe area")
    return left, top, right, bottom


def _graph_rect(cfg, safe, width, height, kind):
    sl, st, sr, sb = safe; sw, sh = sr-sl, sb-st
    if kind == "brightness":
        g = getattr(cfg, "BRIGHTNESS_ONLY_GRAPH", cfg.BRIGHTNESS_GRAPH)
    else:
        g = getattr(cfg, "DIRECTOR_BRIGHTNESS_GRAPH", cfg.BRIGHTNESS_GRAPH)
    x0 = sl + int(sw * float(g["x"])); y0 = st + int(sh * float(g["y"]))
    gw = int(sw * float(g["width"])); gh = int(sh * float(g["height"]))
    x1, y1 = x0+gw, y0+gh
    if cfg.ENFORCE_SAFE_AREA and (x0 < sl or y0 < st or x1 > sr or y1 > sb):
        raise ValueError("BRIGHTNESS_GRAPH exceeds configured safe area")
    return x0, y0, x1, y1


def make_overlay_frames(kind: str, rows: list[dict], expected: list[ExpectedFrame], width: int, height: int,
                        cfg, out_dir: Path, bright_values: list[float] | None = None,
                        only_frame: int | None = None):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise SystemExit("Overlay rendering requires Pillow: pip install pillow")

    clear_stage(out_dir)
    bright = bright_values if bright_values is not None else brightness_series(rows)
    n = len(rows); safe = _safe_rect(cfg, width, height); sl, st, sr, sb = safe
    graph = _graph_rect(cfg, safe, width, height, kind)
    stroke = max(1, int(round(height * cfg.TEXT_STROKE_WIDTH)))
    line_white = max(2, int(round(width * cfg.GRAPH_LINE_WIDTH)))
    line_edge = line_white + 2 * max(1, int(round(width * cfg.GRAPH_EDGE_WIDTH)))
    axis_w = max(1, int(round(width * cfg.GRAPH_AXIS_WIDTH)))
    grid_w = max(1, int(round(width * cfg.GRAPH_GRID_WIDTH)))
    radius = max(3, int(round(width * cfg.GRAPH_POINT_RADIUS)))

    def font(path, frac):
        px = max(10, int(round(height * frac)))
        try: return ImageFont.truetype(path, px)
        except OSError: return ImageFont.load_default()
    f_small = font(cfg.FONT_PATH, cfg.TEXT_SIZE_SMALL)
    f_med = font(cfg.FONT_PATH, cfg.TEXT_SIZE_MEDIUM)
    f_large = font(cfg.FONT_BOLD_PATH, cfg.TEXT_SIZE_LARGE)
    f_title = font(cfg.FONT_BOLD_PATH, cfg.TEXT_SIZE_TITLE)

    def txt(draw, xy, s, fnt, anchor=None):
        draw.text(xy, s, font=fnt, fill=cfg.TEXT_FILL, stroke_width=stroke,
                  stroke_fill=cfg.TEXT_STROKE_FILL, anchor=anchor)

    def text_width(draw, s, fnt):
        box = draw.textbbox((0, 0), s, font=fnt, stroke_width=stroke)
        return box[2] - box[0]

    def fit_font(draw, s, bold_path, start_frac, max_width, min_frac=0.014):
        frac = start_frac
        while frac >= min_frac:
            fnt = font(bold_path, frac)
            if text_width(draw, s, fnt) <= max_width:
                return fnt
            frac -= 0.002
        return font(bold_path, min_frac)

    gx0, gy0, gx1, gy1 = graph
    # Internal graph padding reserves labels without crossing the safe area.
    pad_l = int(width * 0.020); pad_r = int(width * 0.012)
    pad_t = int(height * 0.048); pad_b = int(height * 0.020)
    px0, py0, px1, py1 = gx0+pad_l, gy0+pad_t, gx1-pad_r, gy1-pad_b
    yrange = max(1e-9, cfg.GRAPH_Y_MAX - cfg.GRAPH_Y_MIN)

    def draw_graph_header(draw, show_title, show_value, value):
        left = gx0 + pad_l
        right = gx1 - pad_r
        date_text = run_date if getattr(cfg, "OVERLAY_SHOW_DATE", True) else ""
        header_y = gy0 - int(height * 0.008)
        if show_title:
            title = str(cfg.BRIGHTNESS_TITLE).split("(", 1)[0].strip().upper()
            available = (right - left) * (0.60 if date_text else 1.0)
            title_font = fit_font(draw, title, cfg.FONT_BOLD_PATH, 0.022, available)
            txt(draw, (left, header_y), title, title_font, anchor="ls")
        if date_text:
            date_font = fit_font(draw, date_text, cfg.FONT_BOLD_PATH, 0.020,
                                 (right - left) * (0.38 if show_title else 1.0))
            txt(draw, (right, header_y), date_text, date_font, anchor="rs")
        if show_value:
            value_font = fit_font(draw, value, cfg.FONT_BOLD_PATH, 0.022, (right - left) * 0.30)
            txt(draw, (right, gy0 + int(height * 0.007)), value, value_font, anchor="ra")

    # Precompute coordinates for speed.
    xs = [px0 + (px1-px0) * (i / max(1, n-1)) for i in range(n)]
    ys = [py1 - (py1-py0) * ((max(cfg.GRAPH_Y_MIN, min(cfg.GRAPH_Y_MAX, v))-cfg.GRAPH_Y_MIN)/yrange) for v in bright]
    run_date = date_label(rows)
    axis_font = f_small
    start, end = run_bounds(rows)
    marker_positions = [
        (round(px0 + (px1-px0) * ((at-start)/(end-start))), icon)
        for at, icon in solar_markers(rows)
    ] if end > start else []

    def phase_icon(draw, x, y, icon):
        unit = max(5, round(width * 0.008))
        if icon == "golden":
            gold = (248, 205, 103, 255)
            draw.ellipse((x-unit, y-unit, x+unit, y+unit), fill=gold, outline="white", width=2)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1.5, -1.5), (1.5, -1.5), (-1.5, 1.5), (1.5, 1.5)):
                draw.line((x+dx*unit*.65, y+dy*unit*.65, x+dx*unit, y+dy*unit), fill=gold, width=2)
        elif icon == "blue":
            draw.ellipse((x-unit, y-unit, x+unit, y+unit), fill=(168, 217, 255, 255), outline="white", width=2)
            draw.ellipse((x-unit//3, y-unit-unit//3, x+unit+unit//2, y+unit//2), fill=(55, 65, 69, 255))
        else:
            draw.ellipse((x-unit, y-unit, x+unit, y+unit), fill=(0, 0, 0, 255), outline="white", width=2)
            outer = unit * 0.62
            inner = outer * 0.42
            points = []
            for point in range(10):
                angle = math.radians(-90 + point * 36)
                radius = outer if point % 2 == 0 else inner
                points.append((x + math.cos(angle) * radius, y + math.sin(angle) * radius))
            draw.polygon(points, fill="white")

    for i, (row, exp) in enumerate(zip(rows, expected)):
        if only_frame is not None and exp.frame != only_frame:
            continue
        im = Image.new("RGBA", (width, height), (0,0,0,0)); d = ImageDraw.Draw(im, "RGBA")

        # Brightness graph panel.
        d.rounded_rectangle(graph, radius=max(8, int(width*0.012)), fill=(0,0,0,int(cfg.GRAPH_PANEL_ALPHA)))
        guides = max(2, int(cfg.GRAPH_HORIZONTAL_GUIDES))
        for k in range(guides):
            yy = py0 + (py1-py0) * k/(guides-1)
            d.line((px0, yy, px1, yy), fill=(255,255,255,int(cfg.GRAPH_GRID_ALPHA)), width=grid_w)
        d.line((px0, py0, px0, py1), fill=(255,255,255,int(cfg.GRAPH_AXIS_ALPHA)), width=axis_w)
        d.line((px0, py1, px1, py1), fill=(255,255,255,int(cfg.GRAPH_AXIS_ALPHA)), width=axis_w)
        if getattr(cfg, "GRAPH_SHOW_AXIS_LABELS", True):
            y_min = f"{cfg.GRAPH_Y_MIN:g}"
            y_mid = f"{(cfg.GRAPH_Y_MIN + cfg.GRAPH_Y_MAX) / 2:g}"
            y_max = f"{cfg.GRAPH_Y_MAX:g}"
            txt(d, (gx0+int(width*0.004), py1), y_min, axis_font, anchor="ls")
            txt(d, (gx0+int(width*0.004), (py0+py1)/2), y_mid, axis_font, anchor="lm")
            txt(d, (gx0+int(width*0.004), py0), y_max, axis_font, anchor="la")
            txt(d, (px0, gy1-int(height*0.005)), clock_label(rows[0]), axis_font, anchor="ls")
            txt(d, (px1, gy1-int(height*0.005)), clock_label(rows[-1]), axis_font, anchor="rs")

        for marker_x, icon in marker_positions:
            d.line((marker_x, py0, marker_x, py1), fill=(255, 255, 255, 190), width=max(1, round(width*0.0015)))
            phase_icon(d, marker_x, py0+int(height*0.012), icon)

        upto = i+1 if cfg.GRAPH_REVEAL_LIVE else n
        if upto >= 2:
            pts = list(zip(xs[:upto], ys[:upto]))
            d.line(pts, fill=(0,0,0,235), width=line_edge, joint="curve")
            d.line(pts, fill=(255,255,255,255), width=line_white, joint="curve")
        cx, cy = xs[upto-1], ys[upto-1]
        d.ellipse((cx-radius,cy-radius,cx+radius,cy+radius), fill=(255,255,255,255), outline=(0,0,0,255), width=max(1,stroke))

        if kind == "brightness":
            draw_graph_header(d, cfg.BRIGHTNESS_SHOW_TITLE, cfg.BRIGHTNESS_SHOW_CURRENT_VALUE,
                              f"{bright[i]:.3f}")
            meta = []
            if cfg.BRIGHTNESS_SHOW_TIME and not getattr(cfg, "GRAPH_SHOW_AXIS_LABELS", True):
                meta.append(short_time(row))
            if cfg.BRIGHTNESS_SHOW_FRAME: meta.append(f"FRAME {exp.frame:04d}")
            if meta: txt(d, (gx0+pad_l, gy1-int(height*0.006)), "  •  ".join(meta), f_small, anchor="ls")

        elif kind == "director":
            x = sl + int((sr-sl)*cfg.DIRECTOR_INFO_X); y = st + int((sb-st)*cfg.DIRECTOR_INFO_Y)
            txt(d, (x,y), cfg.DIRECTOR_TITLE, f_title); y += int(height*(cfg.TEXT_SIZE_TITLE + cfg.DIRECTOR_LINE_SPACING))
            meta = []
            if cfg.DIRECTOR_SHOW_FRAME: meta.append(f"FRAME {exp.frame:04d}/{expected[-1].frame:04d}")
            if cfg.DIRECTOR_SHOW_TIME: meta.append(short_time(row))
            if meta: txt(d, (x,y), "   ".join(meta), f_med); y += int(height*(cfg.TEXT_SIZE_MEDIUM + cfg.DIRECTOR_LINE_SPACING))
            if cfg.DIRECTOR_SHOW_EXPOSURE:
                iso = num(row, "actual_iso", "iso")
                ap = num(row, "actual_aperture", "aperture")
                sh = num(row, "actual_shutter_seconds", "shutter_seconds")
                iso_s = str(int(round(iso))) if math.isfinite(iso) else "—"
                ap_s = f"ƒ/{ap:g}" if math.isfinite(ap) else "ƒ/—"
                txt(d, (x,y), f"ISO {iso_s}   {ap_s}   {fmt_shutter(sh)}", f_large); y += int(height*(cfg.TEXT_SIZE_LARGE + cfg.DIRECTOR_LINE_SPACING))
            # No separate SCENE row: the live brightness chart carries scene telemetry.
            if cfg.DIRECTOR_SHOW_BRIGHTNESS_GRAPH:
                draw_graph_header(d, True, cfg.DIRECTOR_SHOW_CURRENT_BRIGHTNESS,
                                  f"{bright[i]:.3f}")

            # Camera updates are inferred from settings that actually appear in telemetry.
            # Therefore rejected AI proposals can never be displayed here.
            if getattr(cfg, "DIRECTOR_SHOW_APPLIED_CAMERA_UPDATES", True) and i > 0:
                hold = max(1, int(getattr(cfg, "DIRECTOR_UPDATE_HOLD_FRAMES", 18)))
                change_i = None
                for j in range(i, max(0, i-hold), -1):
                    prev, cur = rows[j-1], rows[j]
                    keys = ("actual_iso", "actual_aperture", "actual_shutter_seconds")
                    if not any(k in cur for k in keys):
                        keys = ("iso", "aperture", "shutter_seconds")
                    if any(num(prev,k) != num(cur,k) for k in keys):
                        change_i = j
                        break
                if change_i is not None:
                    cur = rows[change_i]
                    iso = num(cur, "actual_iso", "iso")
                    ap = num(cur, "actual_aperture", "aperture")
                    sh = num(cur, "actual_shutter_seconds", "shutter_seconds")
                    iso_s = str(int(round(iso))) if math.isfinite(iso) else "—"
                    ap_s = f"ƒ/{ap:g}" if math.isfinite(ap) else "ƒ/—"
                    update = f"CAMERA UPDATE  ✓  ISO {iso_s}   {ap_s}   {fmt_shutter(sh)}"
                    uy = y + int(height * 0.035)
                    txt(d, (x, uy), update, f_small, anchor="ls")

        im.save(out_dir / f"overlay_{exp.frame:06d}.png", optimize=True)


def run_ffmpeg(stage: Path, overlay_dir: Path | None, start_frame: int, cfg: dict, output: Path, overwrite: bool):
    fps, w, h = cfg["fps"], cfg["width"], cfg["height"]
    base_vf = filter_chain(cfg["framing"], w, h)
    cmd = ["ffmpeg", "-hide_banner", "-stats", "-y" if overwrite else "-n",
           "-framerate", str(fps), "-start_number", str(start_frame), "-i", str(stage / "frame_%06d.jpg")]
    if overlay_dir is None:
        cmd += ["-vf", base_vf, "-r", str(fps)]
    else:
        cmd += ["-framerate", str(fps), "-start_number", str(start_frame), "-i", str(overlay_dir / "overlay_%06d.png"),
                "-filter_complex", f"[0:v]{base_vf}[base];[1:v]format=rgba[ov];[base][ov]overlay=0:0:format=auto[outv]",
                "-map", "[outv]", "-r", str(fps)]
    cmd += encoder_args(cfg["crf"], cfg["preset"], w, h) + ["-an", str(output)]
    print("\n" + " ".join(f'\"{x}\"' if " " in x else x for x in cmd))
    started = time.monotonic(); proc = subprocess.run(cmd); return proc.returncode, time.monotonic()-started


def main() -> int:
    p = argparse.ArgumentParser(description="Timelapser V3 renderer: clean + live telemetry overlay videos.")
    p.add_argument("run_dir", type=Path); p.add_argument("--source", type=Path, required=True)
    p.add_argument("--profile", choices=["instagram-reel","instagram-feed","portrait4k","4k","native43"], default="instagram-reel")
    p.add_argument("--outputs", nargs="+", choices=["clean","brightness","director"], default=None,
                   help="Videos to make. Default comes from overlay_config.py (clean brightness director).")
    p.add_argument("--overlay-config", type=Path, default=Path(__file__).with_name("overlay_config.py"))
    p.add_argument("--fps", type=float); p.add_argument("--width", type=int); p.add_argument("--height", type=int)
    p.add_argument("--framing", choices=["crop","contain"]); p.add_argument("--crf", type=int)
    p.add_argument("--preset", choices=["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"])
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--output-stem-suffix", default="", help="Suffix appended to output filename stem before profile/kind")
    p.add_argument("--brightness-source", choices=["telemetry","frames"], default="telemetry",
                   help="Use telemetry brightness or measure brightness from the source frames")
    p.add_argument("--brightness-analysis-max-edge", type=int, default=512,
                   help="Long-edge size for per-frame brightness analysis")
    p.add_argument("--overwrite", action="store_true"); p.add_argument("--keep-stage", action="store_true"); p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = profile_defaults(args.profile)
    for key in ("fps","width","height","framing","crf","preset"):
        value = getattr(args,key)
        if value is not None: cfg[key] = value
    if cfg["fps"] <= 0: raise SystemExit("FPS must be > 0")

    run_dir = args.run_dir.expanduser().resolve(); source = args.source.expanduser().resolve()
    if not run_dir.is_dir(): raise SystemExit(f"Run directory does not exist: {run_dir}")
    if not source.is_dir(): raise SystemExit(f"Source directory does not exist: {source}")
    overlay_cfg = load_overlay_config(args.overlay_config.expanduser().resolve())
    outputs = args.outputs or list(overlay_cfg.DEFAULT_OUTPUTS)
    expected, telemetry_rows = read_telemetry(run_dir)

    print("="*72); print("TIMELAPSER VIDEO RENDERER V3 — INSTAGRAM + LIVE OVERLAYS")
    print(f"Run:      {run_dir}\nFrames:   {len(expected)}\nProfile:  {args.profile}\nOutput:   {cfg['width']}x{cfg['height']} @ {cfg['fps']:g} fps")
    print(f"Videos:   {', '.join(outputs)}\nOverlay:  {args.overlay_config}"); print("="*72)

    index = build_source_index(source); stage = run_dir / "_render_stage"; clear_stage(stage)
    manifest, missing, ambiguous = stage_sequence(expected,index,stage)
    if ambiguous:
        print(f"REFUSING TO RENDER: {len(ambiguous)} duplicate source basename(s)"); return 3
    if missing:
        print(f"REFUSING TO RENDER: {len(missing)} production frame(s) missing");
        for item in missing[:40]: print(f"  frame {item.frame:06d}: {item.basename}")
        return 2
    write_manifest(run_dir / "render_manifest.csv", manifest)
    exposure_metadata_source = enrich_rows_with_exif(telemetry_rows, manifest, run_dir)

    out_dir = (args.output_dir.expanduser().resolve() if args.output_dir else run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    planned = {kind: out_dir / f"{run_dir.name}{args.output_stem_suffix}_{args.profile}_{kind}.mp4" for kind in outputs}
    collisions = [p for p in planned.values() if p.exists()]
    if collisions and not args.overwrite:
        raise SystemExit("Output exists; use --overwrite:\n" + "\n".join(str(x) for x in collisions))

    if args.dry_run:
        # Also validate brightness if an overlay output was requested.
        if any(x in outputs for x in ("brightness","director")):
            if args.brightness_source == "frames":
                brightness_series_from_sources(manifest, max_edge=args.brightness_analysis_max_edge)
            else:
                brightness_series(telemetry_rows)
        print(f"DRY RUN COMPLETE: frame mapping and requested overlay telemetry validated. Exposure metadata: {exposure_metadata_source}.")
        if not args.keep_stage: shutil.rmtree(stage)
        return 0
    if shutil.which("ffmpeg") is None: raise SystemExit("ffmpeg not found. Install with: sudo apt install ffmpeg")

    results = []
    bright_values = None
    if args.brightness_source == "frames" and any(x in outputs for x in ("brightness","director")):
        print("\nMeasuring brightness from rendered source frames...")
        bright_values = brightness_series_from_sources(manifest, max_edge=args.brightness_analysis_max_edge)
    for kind in outputs:
        ov_dir = None
        if kind in ("brightness","director"):
            ov_dir = run_dir / f"_overlay_stage_{kind}"
            print(f"\nGenerating {kind} overlay frames...")
            make_overlay_frames(kind, telemetry_rows, expected, cfg["width"], cfg["height"], overlay_cfg, ov_dir, bright_values)
        print(f"\nRendering {kind}: {planned[kind].name}")
        rc, elapsed = run_ffmpeg(stage, ov_dir, expected[0].frame, cfg, planned[kind], args.overwrite)
        results.append({"kind":kind,"output":str(planned[kind]),"returncode":rc,"render_seconds":elapsed})
        if ov_dir and not (args.keep_stage or overlay_cfg.KEEP_OVERLAY_FRAMES): shutil.rmtree(ov_dir, ignore_errors=True)
        if rc != 0:
            print(f"FFmpeg failed while rendering {kind} (code {rc})."); break

    summary = {
        "written_at":datetime.now().isoformat(timespec="seconds"), "renderer_version":"3.0",
        "run_dir":str(run_dir), "source_dir":str(source), "profile":args.profile,
        "frames":len(expected), "fps":cfg["fps"], "width":cfg["width"], "height":cfg["height"],
        "framing":cfg["framing"], "preset":cfg["preset"], "crf":cfg["crf"], "outputs":results,
        "overlay_config":str(args.overlay_config.expanduser().resolve()),
        "brightness_source": args.brightness_source,
        "exposure_metadata_source": exposure_metadata_source,
        "status":"complete" if results and all(r["returncode"]==0 for r in results) and len(results)==len(outputs) else "failed",
    }
    (run_dir / "render_summary_v3.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    if not args.keep_stage: shutil.rmtree(stage, ignore_errors=True)
    if summary["status"] != "complete": return next((r["returncode"] for r in results if r["returncode"]),1)

    print("\n"+"="*72); print("ALL REQUESTED RENDERS COMPLETE")
    for r in results:
        pth=Path(r["output"]); print(f"{r['kind']:10s} {pth.name}  {pth.stat().st_size/1_000_000:.1f} MB  {r['render_seconds']/60:.1f} min")
    print("="*72); return 0


if __name__ == "__main__": raise SystemExit(main())
