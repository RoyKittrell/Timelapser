import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import timelapser_postprocess_v5 as legacy
from timelapser_scheduler_v6 import cleanup_interrupted_stage, pause_postprocess_for_capture


class V6PostprocessPreemptionTests(unittest.TestCase):
    def test_render_cleanup_removes_partial_outputs_but_preserves_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "test_general_v6_wired"
            run.mkdir()
            frames = run / "frames_smoothed_luma_v2"
            frames.mkdir()
            (frames / "frame_000001.jpg").write_bytes(b"frame")
            partial = run / f"{run.name}_final_instagram-reel_clean.mp4"
            partial.write_bytes(b"partial")
            (run / "_render_stage").mkdir()
            (run / "render_summary_v3_final.json").write_text("{}")

            cleanup_interrupted_stage(run, "render_videos")

            self.assertFalse(partial.exists())
            self.assertFalse((run / "_render_stage").exists())
            self.assertFalse((run / "render_summary_v3_final.json").exists())
            self.assertTrue((frames / "frame_000001.jpg").exists())

    def test_legacy_resume_skips_completed_expensive_stages(self):
        completed = (
            "validate_and_repair_jpegs",
            "smooth_exposure_v2",
            "validate_smoothed_jpegs",
            "render_videos",
            "create_instagram_carousel_formats",
            "prune_rendered_images_older_than_30_days",
            "update_timelapse_catalog",
        )
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            with mock.patch.object(legacy, "_run_step") as run_step, mock.patch.object(
                legacy, "_repair_unreadable_frames"
            ) as repair:
                result = legacy.run_postprocess(
                    run,
                    download_fullres=False,
                    smooth_exposure=True,
                    render=True,
                    queue_instagram=False,
                    publish_instagram=False,
                    copy_videos_to_mac=False,
                    initial_completed_steps=completed,
                )
            self.assertEqual(result["status"], "complete")
            run_step.assert_not_called()
            repair.assert_not_called()
            live = json.loads((run / "postprocess_live_status.json").read_text())
            self.assertEqual(live["status"], "complete")

    def test_pause_terminates_process_group_and_records_resumable_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            partial = run / f"{run.name}_final_instagram-reel_clean.mp4"
            partial.write_bytes(b"partial")
            live_path = run / "postprocess_live_status.json"
            live_path.write_text(json.dumps({
                "status": "running",
                "current_step": "render_videos",
                "completed_steps": ["smooth_exposure_v2"],
            }))
            request_path = run / "postprocess_deferred.json"
            request = {"status": "running", "run_dir": str(run)}
            request_path.write_text(json.dumps(request))
            process = subprocess.Popen(["sleep", "60"], start_new_session=True)

            pause_postprocess_for_capture(
                process,
                run,
                request_path,
                request,
                mock.Mock(key="scheduled-sunset"),
            )

            self.assertIsNotNone(process.poll())
            self.assertFalse(partial.exists())
            saved_request = json.loads(request_path.read_text())
            self.assertEqual(saved_request["status"], "pending")
            self.assertEqual(saved_request["interrupted_step"], "render_videos")
            saved_live = json.loads(live_path.read_text())
            self.assertEqual(saved_live["status"], "paused_for_capture")
            self.assertEqual(saved_live["completed_steps"], ["smooth_exposure_v2"])


if __name__ == "__main__":
    unittest.main()
