import unittest

from render_timelapse import encoder_args, filter_chain, profile_defaults


class HighQualityInstagramProfileTests(unittest.TestCase):
    def test_profile_uses_slow_constrained_bt709_encode(self):
        cfg = profile_defaults("instagram-reel-hq")
        args = encoder_args(cfg)

        self.assertEqual(cfg["crf"], 16)
        self.assertEqual(cfg["preset"], "slow")
        self.assertIn("20M", args)
        self.assertIn("open-gop=0", args)
        self.assertIn("bt709", args)

    def test_filter_converts_full_range_jpegs_to_limited_bt709(self):
        chain = filter_chain("crop", 1080, 1920, "bt709")

        self.assertIn("in_range=full", chain)
        self.assertIn("out_range=tv", chain)
        self.assertIn("out_color_matrix=bt709", chain)
        self.assertIn("format=yuv420p", chain)
        self.assertIn("color_primaries=bt709", chain)
        self.assertIn("color_trc=bt709", chain)


if __name__ == "__main__":
    unittest.main()
