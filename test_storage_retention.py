import argparse
import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import prune_old_frames
import timelapser_v6_beta_usb_import as importer


class StorageRetentionTests(unittest.TestCase):
    def test_import_deletes_only_verified_camera_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "20260922_062214_sunrise_v5"
            run.mkdir()
            with (run / "telemetry.csv").open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["frame", "remote_jpg", "time"])
                writer.writeheader()
                writer.writerow({"frame": 1, "remote_jpg": "/DCIM/100OLYMP/A.JPG", "time": "2026-09-22T06:22:14"})
            camera = root / "camera"
            camera.mkdir()
            photo = camera / "A.JPG"
            photo.write_bytes(b"\xff\xd8" + b"x" * 60_000 + b"\xff\xd9")
            other = camera / "B.JPG"
            other.write_bytes(b"keep")
            source = importer.SourceFile(photo, photo.name, photo.stat().st_size, photo.stat().st_mtime, None)
            args = argparse.Namespace(output_dirname="frames_full_jpeg", time_tolerance_seconds=600,
                                      allow_time_mismatch=True, dry_run=False, replace_thumbnails=False,
                                      delete_imported_from_sd=True, force=False)
            result = importer.import_run(run, {"A.JPG": [source]}, args)
            self.assertTrue(result["complete"])
            self.assertEqual(result["deleted_sd_jpegs"], 1)
            self.assertFalse(photo.exists())
            self.assertTrue(other.exists())
            self.assertTrue((run / "frames_full_jpeg/frame_000001.jpg").exists())

    def test_incomplete_import_never_deletes_camera_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "20260922_062214_sunrise_v5"
            run.mkdir()
            with (run / "telemetry.csv").open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["frame", "remote_jpg", "time"])
                writer.writeheader()
                writer.writerows([{"frame": n, "remote_jpg": f"/DCIM/{name}", "time": ""}
                                 for n, name in [(1, "A.JPG"), (2, "B.JPG")]])
            photo = root / "A.JPG"
            photo.write_bytes(b"\xff\xd8" + b"x" * 60_000 + b"\xff\xd9")
            source = importer.SourceFile(photo, photo.name, photo.stat().st_size, photo.stat().st_mtime, None)
            args = argparse.Namespace(output_dirname="frames_full_jpeg", time_tolerance_seconds=600,
                                      allow_time_mismatch=True, dry_run=False, replace_thumbnails=False,
                                      delete_imported_from_sd=True, force=False)
            result = importer.import_run(run, {"A.JPG": [source]}, args)
            self.assertFalse(result["complete"])
            self.assertEqual(result["deleted_sd_jpegs"], 0)
            self.assertTrue(photo.exists())

    def test_prune_requires_old_run_and_all_three_videos(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "20260801_062214_sunrise_v5"
            old.mkdir()
            for name in prune_old_frames.IMAGE_DIRS:
                (old / name).mkdir()
                (old / name / "frame_000001.jpg").write_bytes(b"frame")
            outputs = []
            for kind in ("clean", "brightness", "director"):
                video = old / f"{old.name}_{kind}.mp4"
                video.write_bytes(b"video")
                outputs.append({"kind": kind, "output": str(video), "returncode": 0})
            (old / "render_summary_v3.json").write_text(json.dumps({"status": "complete", "outputs": outputs}))
            (old / outputs[-1]["output"]).unlink()
            self.assertEqual(prune_old_frames.prune(root, 30, today=date(2026, 9, 22)), [])
            (old / outputs[-1]["output"]).write_bytes(b"video")
            self.assertEqual(len(prune_old_frames.prune(root, 30, dry_run=True, today=date(2026, 9, 22))), 3)
            self.assertTrue((old / "frames_full_jpeg").exists())
            self.assertEqual(len(prune_old_frames.prune(root, 30, today=date(2026, 9, 22))), 3)
            self.assertTrue((old / "render_summary_v3.json").exists())
            self.assertFalse((old / "frames_full_jpeg").exists())


if __name__ == "__main__":
    unittest.main()
