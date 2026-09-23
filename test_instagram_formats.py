import tempfile
import unittest
from pathlib import Path

from instagram_formats import carousel_path, find_reel


class InstagramFormatsTests(unittest.TestCase):
    def test_carousel_name_preserves_kind(self):
        source = Path("run_final_instagram-reel_director.mp4")
        self.assertEqual(
            carousel_path(source).name,
            "run_final_instagram-carousel_director.mp4",
        )

    def test_find_reel_prefers_final_run_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "sample_run"
            run_dir.mkdir()
            expected = run_dir / "sample_run_final_instagram-reel_clean.mp4"
            expected.touch()
            (run_dir / "older_instagram-reel_clean.mp4").touch()
            self.assertEqual(find_reel(run_dir, "clean"), expected)


if __name__ == "__main__":
    unittest.main()
