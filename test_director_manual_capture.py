import ast
import __future__
import re
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("timelapser_director_v6.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
RUNNER_SOURCE = Path(__file__).with_name("timelapser_v6.py").read_text(encoding="utf-8")
POSTPROCESS_SOURCE = Path(__file__).with_name("timelapser_postprocess_v6.py").read_text(encoding="utf-8")
LEGACY_POSTPROCESS_SOURCE = Path(__file__).with_name("timelapser_postprocess_v5.py").read_text(encoding="utf-8")
FUNCTION_NAMES = {node.name for node in TREE.body if isinstance(node, ast.FunctionDef)}
PARSE_NODE = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "parse_manual_capture_request"
)
NAMESPACE = {"re": re}
exec(
    compile(
        ast.Module(body=[PARSE_NODE], type_ignores=[]),
        "director-parser",
        "exec",
        flags=__future__.annotations.compiler_flag,
    ),
    NAMESPACE,
)
parse_request = NAMESPACE["parse_manual_capture_request"]


class DirectorManualCaptureTests(unittest.TestCase):
    def test_manual_capture_helpers_exist(self):
        self.assertIn("parse_manual_capture_request", FUNCTION_NAMES)
        self.assertIn("prepare_manual_timelapse", FUNCTION_NAMES)
        self.assertIn("confirm_manual_timelapse", FUNCTION_NAMES)
        self.assertIn("cancel_manual_timelapse", FUNCTION_NAMES)
        self.assertIn("start_manual_timelapse", FUNCTION_NAMES)

    def test_chat_router_calls_manual_start_before_ai_fallback(self):
        router = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "handle_director_command"
        )
        calls = [
            node.func.id
            for node in ast.walk(router)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertIn("parse_manual_capture_request", calls)
        self.assertIn("prepare_manual_timelapse", calls)
        self.assertIn("confirm_manual_timelapse", calls)
        self.assertIn("cancel_manual_timelapse", calls)

    def test_parses_hour_and_second_cadence(self):
        self.assertEqual(
            parse_request("Start a general timelapse for 1hr at 1s cadence"),
            (60.0, 1.0, False),
        )

    def test_parses_hyphenated_duration_and_cadence(self):
        self.assertEqual(
            parse_request("start a manual 1-hour V6 wired timelapse at 1-second cadence"),
            (60.0, 1.0, False),
        )

    def test_parses_minutes_and_every_seconds(self):
        self.assertEqual(
            parse_request("Run a timelapse for 30 minutes every 2.5 seconds"),
            (30.0, 2.5, False),
        )

    def test_parses_do_with_compact_units_and_intervals(self):
        self.assertEqual(
            parse_request("Do a 2hr timelapse at 2s intervals now"),
            (120.0, 2.0, False),
        )

    def test_parses_more_natural_action_verbs(self):
        for request in (
            "Make a timelapse for 45min every 3s",
            "Create a 45-minute timelapse at 3-second cadence",
            "Set up a timelapse for 45 minutes at 3 second intervals",
        ):
            with self.subTest(request=request):
                self.assertEqual(parse_request(request), (45.0, 3.0, False))

    def test_negated_action_does_not_prepare_capture(self):
        self.assertIsNone(parse_request("Don't start a 2hr timelapse at 2s intervals"))

    def test_requires_explicit_duration_and_cadence(self):
        self.assertIsNone(parse_request("Start a timelapse now"))

    def test_explicit_instagram_request_is_reel_only(self):
        self.assertEqual(
            parse_request("Start a timelapse for 1hr at 2s and post it on IG when done"),
            (60.0, 2.0, True),
        )

    def test_instagram_remains_off_when_explicitly_denied(self):
        self.assertEqual(
            parse_request("Start a timelapse for 1hr at 2s; don't post to Instagram"),
            (60.0, 2.0, False),
        )

    def test_runner_is_general_and_does_not_request_instagram_publish(self):
        starter = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "start_manual_timelapse"
        )
        source = ast.get_source_segment(SOURCE, starter)
        self.assertIn('"--mode", "general"', source)
        self.assertIn('"--defer-postprocess"', source)
        self.assertIn('"--publish-instagram-reel"', source)

    def test_preview_includes_required_settings_and_confirmation(self):
        preview = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "prepare_manual_timelapse"
        )
        source = ast.get_source_segment(SOURCE, preview)
        for required in (
            "Capture time", "Cadence", "Expected triggers", "Resulting video",
            "output_fps", "autofocus once", "Storage", "Instagram", "Confirm start",
        ):
            self.assertIn(required, source)

    def test_start_requires_pending_confirmation_route(self):
        router = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "handle_director_command"
        )
        source = ast.get_source_segment(SOURCE, router)
        self.assertIn("PENDING_MANUAL_CAPTURE.exists()", source)
        self.assertIn("confirm_manual_timelapse()", source)

    def test_reel_only_policy_reaches_publisher(self):
        self.assertIn('"instagram_reel_only": bool(args.publish_instagram_reel)', RUNNER_SOURCE)
        self.assertIn('deferred.get("instagram_reel_only", False)', POSTPROCESS_SOURCE)
        self.assertIn('command.append("--skip-carousel")', LEGACY_POSTPROCESS_SOURCE)
        self.assertNotIn('command.append("--skip-reel")', LEGACY_POSTPROCESS_SOURCE)


if __name__ == "__main__":
    unittest.main()
