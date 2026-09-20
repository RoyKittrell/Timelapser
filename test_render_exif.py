import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from render_timelapse import enrich_rows_with_exif


class RenderExifTests(unittest.TestCase):
    def test_original_full_jpeg_exif_is_preferred_over_smoothed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            originals = run_dir / "frames_full_jpeg"
            originals.mkdir()
            original = originals / "frame_000001.jpg"
            original.touch()
            smoothed = run_dir / "frames_smoothed_luma_v2" / "frame_000001.jpg"
            rows = [{"frame": "1"}]
            manifest = [{"frame": 1, "source_file": str(smoothed)}]
            payload = [{
                "SourceFile": str(original),
                "ISO": 200,
                "FNumber": 5.6,
                "ExposureTime": 0.00625,
            }]

            class Result:
                returncode = 0
                stdout = json.dumps(payload)

            with patch("render_timelapse.shutil.which", return_value="/usr/bin/exiftool"), \
                 patch("render_timelapse.subprocess.run", return_value=Result()) as run:
                source = enrich_rows_with_exif(rows, manifest, run_dir)

            self.assertEqual(source, "exif")
            self.assertIn(str(original), run.call_args.args[0])
            self.assertNotIn(str(smoothed), run.call_args.args[0])
            self.assertEqual(rows[0]["actual_iso"], 200)
            self.assertEqual(rows[0]["actual_aperture"], 5.6)
            self.assertEqual(rows[0]["actual_shutter_seconds"], 0.00625)

    def test_smoothed_source_remains_a_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            smoothed = run_dir / "frames_smoothed_luma_v2" / "frame_000001.jpg"
            rows = [{"frame": "1"}]
            manifest = [{"frame": 1, "source_file": str(smoothed)}]

            class Result:
                returncode = 0
                stdout = "[]"

            with patch("render_timelapse.shutil.which", return_value="/usr/bin/exiftool"), \
                 patch("render_timelapse.subprocess.run", return_value=Result()) as run:
                source = enrich_rows_with_exif(rows, manifest, run_dir)

            self.assertEqual(source, "telemetry")
            self.assertIn(str(smoothed), run.call_args.args[0])

    def test_pillow_fallback_reads_nested_exif_ifd(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            originals = run_dir / "frames_full_jpeg"
            originals.mkdir()
            original = originals / "frame_000001.jpg"
            original.touch()
            rows = [{"frame": "1"}]
            manifest = [{"frame": 1, "source_file": "smoothed.jpg"}]

            class Exif:
                def get_ifd(self, key):
                    self.key = key
                    return {33434: 0.0125, 33437: 4.0, 34855: 400}

            class ImageFile:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def getexif(self):
                    return Exif()

            with patch("render_timelapse.shutil.which", return_value=None), \
                 patch("PIL.Image.open", return_value=ImageFile()):
                source = enrich_rows_with_exif(rows, manifest, run_dir)

            self.assertEqual(source, "exif")
            self.assertEqual(rows[0]["actual_iso"], 400.0)
            self.assertEqual(rows[0]["actual_aperture"], 4.0)
            self.assertEqual(rows[0]["actual_shutter_seconds"], 0.0125)


if __name__ == "__main__":
    unittest.main()
