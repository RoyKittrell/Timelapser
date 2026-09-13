import math
import subprocess
from pathlib import Path

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None


def extract_preview_from_orf(orf_path: Path, jpeg_path: Path):
    jpeg_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "exiftool",
        "-b",
        "-PreviewImage",
        str(orf_path),
    ]
    with jpeg_path.open("wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
    if result.returncode != 0 or jpeg_path.stat().st_size == 0:
        # Try embedded JPEG as fallback.
        with jpeg_path.open("wb") as f:
            result = subprocess.run(
                ["exiftool", "-b", "-JpgFromRaw", str(orf_path)],
                stdout=f,
                stderr=subprocess.PIPE,
            )
    if result.returncode != 0 or not jpeg_path.exists() or jpeg_path.stat().st_size == 0:
        raise RuntimeError("Could not extract embedded JPEG from ORF")
    return jpeg_path


def analyse_jpeg(jpeg_path: Path):
    if cv2 is None:
        return {}
    img = cv2.imread(str(jpeg_path))
    if img is None:
        return {}
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype("float32") / 255.0
    median = float(np.median(gray))
    mean = float(np.mean(gray))
    highlights_pct = float(np.mean(gray >= 0.98) * 100.0)
    shadows_pct = float(np.mean(gray <= 0.02) * 100.0)
    p50_luma = median * 255.0
    pixel_ev_p50 = math.log2(
        max((max(p50_luma, 1.0) / 255.0) ** 2.2, 1e-9) / 0.18
    ) + 12.0
    return {
        "median": median,
        "mean": mean,
        "p50_luma": p50_luma,
        "pixel_ev_p50": float(pixel_ev_p50),
        "hist_std": float(np.std(gray) * 255.0),
        "highlights_pct": highlights_pct,
        "shadows_pct": shadows_pct,
        "highlight_fraction": highlights_pct / 100.0,
        "shadow_fraction": shadows_pct / 100.0,
    }
