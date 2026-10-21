import json
import tempfile
import unittest
from pathlib import Path

from erase_camera_sd_after_success import erase_dcim_media, verify_completed_run


def write_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8" + b"x" * 60_000 + b"\xff\xd9")


class CameraSdCleanupTests(unittest.TestCase):
    def test_refuses_incomplete_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "postprocess_workflow_summary.json").write_text(
                json.dumps({"status": "failed_render"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "not complete"):
                verify_completed_run(run)

    def test_erases_only_camera_media_under_dcim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            photo = root / "DCIM" / "100OLYMP" / "A.JPG"
            raw = root / "DCIM" / "100OLYMP" / "A.ORF"
            metadata = root / "DCIM" / "100OLYMP" / "NOTE.TXT"
            outside = root / "PRIVATE" / "KEEP.JPG"
            for path in (photo, raw, metadata, outside):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"data")

            deleted = erase_dcim_media(root)

            self.assertEqual(deleted, ["DCIM/100OLYMP/A.JPG", "DCIM/100OLYMP/A.ORF"])
            self.assertFalse(photo.exists())
            self.assertFalse(raw.exists())
            self.assertTrue(metadata.exists())
            self.assertTrue(outside.exists())

    def test_validates_imported_frames_and_render_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "postprocess_workflow_summary.json").write_text(
                json.dumps({"status": "complete"}), encoding="utf-8"
            )
            write_jpeg(run / "frames_full_jpeg" / "frame_000001.jpg")
            output = run / "finished.mp4"
            output.write_bytes(b"video")
            (run / "render_summary_v3.json").write_text(json.dumps({
                "status": "complete",
                "outputs": [{"output": str(output), "returncode": 0}],
            }), encoding="utf-8")

            result = verify_completed_run(run)

            self.assertEqual(result["validated_frames"], 1)
            self.assertEqual(result["validated_outputs"], 1)


if __name__ == "__main__":
    unittest.main()
