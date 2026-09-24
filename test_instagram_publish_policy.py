#!/usr/bin/env python3

import unittest

from timelapser_postprocess_v5 import should_auto_publish_instagram


class InstagramPublishPolicyTests(unittest.TestCase):
    def test_sunrise_and_sunset_publish_automatically(self):
        self.assertTrue(should_auto_publish_instagram("sunrise"))
        self.assertTrue(should_auto_publish_instagram("sunset"))

    def test_general_and_unknown_modes_do_not_publish(self):
        self.assertFalse(should_auto_publish_instagram("general"))
        self.assertFalse(should_auto_publish_instagram("manual"))
        self.assertFalse(should_auto_publish_instagram(""))

    def test_mode_matching_is_normalized(self):
        self.assertTrue(should_auto_publish_instagram(" Sunrise "))


if __name__ == "__main__":
    unittest.main()
