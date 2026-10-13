import unittest
from pathlib import Path

from instagram_queue import build_caption


class InstagramCaptionTests(unittest.TestCase):
    def test_sunset_caption_has_camera_and_landmark_tags(self):
        caption = build_caption(Path("run"), {"mode": "sunset"})

        for tag in ("#sunset", "#Merdeka118", "#KL118", "#Olympus", "#OMSystem", "#EM5MarkIII"):
            self.assertIn(tag, caption)
        self.assertNotIn("#sunrise", caption)

    def test_custom_caption_keeps_permanent_tags(self):
        caption = build_caption(Path("run"), {"mode": "sunrise"}, "Clouds over KL")

        self.assertTrue(caption.startswith("Clouds over KL\n\n#sunrise"))
        self.assertIn("#Merdeka118", caption)
        self.assertIn("#OMSystem", caption)


if __name__ == "__main__":
    unittest.main()
