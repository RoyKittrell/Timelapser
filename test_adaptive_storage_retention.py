import json
import tempfile
import unittest
from pathlib import Path

import storage_retention


class AdaptiveStorageRetentionTests(unittest.TestCase):
    def make_published_run(self, root: Path) -> Path:
        run = root / "20260101_060000_sunrise_v6_wired"
        run.mkdir()
        (run / "run_manifest.json").write_text("{}")
        (run / "telemetry.csv").write_text("frame\n1\n")
        (run / "instagram_publish_receipt.json").write_text(json.dumps({"status": "complete"}))
        (run / f"{run.name}_final_instagram-reel_clean.mp4").write_bytes(b"final")
        frames = run / "frames_smoothed_luma_4k"
        frames.mkdir()
        (frames / "frame_000001.jpg").write_bytes(b"x" * 100)
        return run

    def test_does_nothing_above_free_space_floor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_published_run(root)
            actions = storage_retention.prune(
                root, min_free_gb=1, target_free_gb=2, free_bytes=3 * 1024**3
            )
            self.assertEqual(actions, [])
            self.assertTrue((run / "frames_smoothed_luma_4k").exists())

    def test_prunes_intermediates_but_preserves_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = self.make_published_run(root)
            actions = storage_retention.prune(
                root, min_free_gb=1, target_free_gb=0.00000005, free_bytes=0
            )
            self.assertTrue(actions)
            self.assertFalse((run / "frames_smoothed_luma_4k").exists())
            self.assertTrue((run / "run_manifest.json").exists())
            self.assertTrue((run / "telemetry.csv").exists())
            self.assertTrue((run / "instagram_publish_receipt.json").exists())
            self.assertTrue((run / f"{run.name}_final_instagram-reel_clean.mp4").exists())


if __name__ == "__main__":
    unittest.main()
