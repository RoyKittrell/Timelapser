import tempfile
import unittest
from pathlib import Path

from instagram_auto_publish import publish_folder


class InstagramAutoPublishTests(unittest.TestCase):
    def test_manual_folder_is_discovered_in_publishing_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in (
                "sample_instagram-carousel_clean.mp4",
                "sample_instagram-carousel_director.mp4",
                "sample_instagram-carousel_brightness.mp4",
                "sample_instagram-reel_clean.mp4",
            ):
                (folder / name).touch()
            (folder / "caption.txt").write_text("Manual test\n", encoding="utf-8")
            result = publish_folder(
                folder,
                caption=None,
                skip_carousel=False,
                skip_reel=False,
                force=False,
                dry_run=True,
            )
            self.assertEqual(result["status"], "validated")
            self.assertTrue(result["pending_carousel"])
            self.assertTrue(result["pending_reel"])
            self.assertTrue(str(result["videos"]["carousel"]["clean"]).endswith("carousel_clean.mp4"))


if __name__ == "__main__":
    unittest.main()
