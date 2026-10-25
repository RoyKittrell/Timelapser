import ast
import __future__
import re
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("timelapser_director_v6.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
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
            (60.0, 1.0),
        )

    def test_parses_minutes_and_every_seconds(self):
        self.assertEqual(
            parse_request("Run a timelapse for 30 minutes every 2.5 seconds"),
            (30.0, 2.5),
        )

    def test_requires_explicit_duration_and_cadence(self):
        self.assertIsNone(parse_request("Start a timelapse now"))

    def test_runner_is_general_and_does_not_request_instagram_publish(self):
        starter = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "start_manual_timelapse"
        )
        source = ast.get_source_segment(SOURCE, starter)
        self.assertIn('"--mode", "general"', source)
        self.assertIn('"--defer-postprocess"', source)
        self.assertNotIn("publish-instagram", source)

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


if __name__ == "__main__":
    unittest.main()
