import base64
import json
import queue
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from v5_config import (
    ENABLE_AI_COMMANDER,
    AI_MODEL,
    AI_IMAGE_DETAIL,
    AI_TIMEOUT_SECONDS,
    MIN_ISO,
    MAX_ISO,
    MIN_APERTURE,
    MAX_APERTURE,
    MIN_SHUTTER_SECONDS,
    MAX_SHUTTER_SECONDS,
    PREFERRED_MAX_SHUTTER_SECONDS,
    LENS_PROFILE_NAME,
    LENS_MIN_APERTURE,
    MAX_AI_EXPOSURE_STEP_EV,
    AI_EXPOSURE_DEADBAND_EV,
)


ALLOWED_ACTIONS = {
    "NOOP",
    "SET_EXPOSURE",
    "WAIT",
    "ABORT",
}


@dataclass
class CommanderAction:
    action: str
    reason: str = ""
    iso: int | None = None
    aperture: float | None = None
    shutter_seconds: float | None = None
    wait_seconds: float | None = None


class AICommander:
    """AI thinks asynchronously; camera execution remains deterministic.

    AI NEVER receives an object capable of talking to the camera.
    It can only put validated intentions into action_queue.
    """

    def __init__(self, logger):
        self.logger = logger
        self.request_queue = queue.Queue(maxsize=2)
        self.action_queue = queue.Queue(maxsize=20)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.enabled = ENABLE_AI_COMMANDER

    def start(self):
        if not self.enabled:
            return
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def startup_review(self, state_snapshot: dict, image_path: str):
        """Synchronously force a starting exposure choice before frame 1.

        V5.0.5 does NOT allow NOOP at startup. The scout exists specifically to
        establish exposure. AI is retried once if it fails to return a valid
        SET_EXPOSURE action.
        """
        if not self.enabled:
            return []

        last_exc = None
        for attempt in (1, 2):
            try:
                actions, raw = self._ask_startup_ai(state_snapshot, image_path)
                self.logger.ai_decision(
                    trigger="startup_scout",
                    attempt=attempt,
                    raw=raw,
                    actions=[asdict(a) for a in actions],
                )

                valid = [a for a in actions if a.action == "SET_EXPOSURE"]
                if valid:
                    return [valid[0]]

                self.logger.event(
                    "startup_ai_invalid_no_exposure",
                    attempt=attempt,
                    raw=raw,
                )
            except Exception as exc:
                last_exc = exc
                self.logger.error(
                    "ai_commander_startup_review",
                    exc,
                    attempt=attempt,
                )

        if last_exc is not None:
            self.logger.event(
                "startup_ai_failed_after_retry",
                error=str(last_exc),
            )
        return []

    def _ask_startup_ai(self, state, image_path):
        """Strict startup scout evaluation: exactly one full exposure choice."""
        from openai import OpenAI

        prompt = f"""
You are setting the INITIAL exposure for an Olympus E-M1 Mark III
timelapse from a real scout image. The operator may label the run sunrise or sunset, but judge ONLY the scene as it exists now.

You MUST choose a starting exposure before frame 1.
NOOP is NOT allowed.
WAIT is NOT allowed.
RECONCILE_SD is NOT allowed.
ABORT is NOT allowed unless the image cannot be interpreted at all.

Return exactly ONE SET_EXPOSURE action containing ALL THREE:
- iso
- aperture
- shutter_seconds

Goal:
- protect bright sky/highlights from clipping;
- keep useful midtone detail;
- keep ISO low where possible;
- use the real widest lens aperture before treating the aperture as maxed out;
- prefer ISO increases over shutter speeds longer than the preferred limit;
- shutter is limited to {MAX_SHUTTER_SECONDS:g}s; after that, use ISO rather
  than longer exposures;
- do not preserve an obviously overexposed baseline merely because it was the
  current camera setting.

Lens profile: {LENS_PROFILE_NAME}; physical widest f/{LENS_MIN_APERTURE:g}; commandable V5 floor f/{MIN_APERTURE:g}
Hard limits:
ISO {MIN_ISO}-{MAX_ISO}
aperture f/{MIN_APERTURE}-f/{MAX_APERTURE}; f/2.8 and f/4.8 are not commandable over Olympus Wi-Fi for this lens profile
shutter {MIN_SHUTTER_SECONDS}-{MAX_SHUTTER_SECONDS} seconds
Operator preference: after about {PREFERRED_MAX_SHUTTER_SECONDS:.3g}s, raise ISO before making shutter longer unless ISO is already near its limit.

Current state and scout metrics:
{json.dumps(state, indent=2)}

Return ONLY JSON:
{{
  "summary": "brief assessment of scout exposure",
  "actions": [
    {{
      "action": "SET_EXPOSURE",
      "iso": 200,
      "aperture": 6.3,
      "shutter_seconds": 0.002,
      "reason": "brief reason"
    }}
  ]
}}
""".strip()

        content = [{"type": "input_text", "text": prompt}]
        if image_path and Path(image_path).exists():
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            content.append({
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
                "detail": AI_IMAGE_DETAIL,
            })

        client = OpenAI(timeout=AI_TIMEOUT_SECONDS)
        response = client.responses.create(
            model=AI_MODEL,
            input=[{"role": "user", "content": content}],
        )

        raw_text = response.output_text.strip()
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        data = json.loads(raw_text)

        item = (data.get("actions") or [{}])[0]
        if str(item.get("action", "")).upper() != "SET_EXPOSURE":
            return [], data

        iso = max(MIN_ISO, min(MAX_ISO, int(round(float(item["iso"])))))
        aperture = max(MIN_APERTURE, min(MAX_APERTURE, float(item["aperture"])))
        shutter = max(
            MIN_SHUTTER_SECONDS,
            min(MAX_SHUTTER_SECONDS, float(item["shutter_seconds"]))
        )

        action = CommanderAction(
            action="SET_EXPOSURE",
            reason=str(item.get("reason", ""))[:300],
            iso=iso,
            aperture=aperture,
            shutter_seconds=shutter,
        )
        return [action], data

    def request_review(self, state_snapshot: dict, image_path: str | None, trigger: str):
        if not self.enabled:
            return False
        payload = {
            "state": state_snapshot,
            "image_path": image_path,
            "trigger": trigger,
        }

        # Keep newest review request; stale AI deliberation is worse than skipping.
        try:
            self.request_queue.put_nowait(payload)
            self.logger.event("ai_review_queued", trigger=trigger)
            return True
        except queue.Full:
            self.logger.event("ai_review_queue_full", trigger=trigger)
            return False

    def get_actions_nowait(self):
        actions = []
        while True:
            try:
                actions.append(self.action_queue.get_nowait())
            except queue.Empty:
                break
        return actions

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                req = self.request_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                actions, raw = self._ask_ai(req)
                self.logger.ai_decision(
                    trigger=req["trigger"],
                    raw=raw,
                    actions=[asdict(a) for a in actions],
                )
                for action in actions:
                    try:
                        self.action_queue.put_nowait(action)
                    except queue.Full:
                        self.logger.event("ai_action_queue_full", action=asdict(action))
                        break
            except Exception as exc:
                self.logger.error("ai_commander_worker", exc, trigger=req.get("trigger"))

    def _ask_ai(self, req):
        from openai import OpenAI

        state = req["state"]
        trigger = req["trigger"]
        image_path = req.get("image_path")

        prompt = f"""
You are the AI Director of an Olympus E-M1 Mark III timelapse.

CRITICAL INTERPRETATION RULE:
- The configured mode ("sunset" or "sunrise") describes the OPERATOR'S INTENDED
  type of session. It is NOT evidence that the light is currently falling or rising.
- NEVER write reasoning such as "as the sunset darkens" unless the supplied
  scene_trend telemetry actually shows sustained measured dimming.
- The image and scene_trend measurements are authoritative. If they conflict with
  the mode label, trust the measurements.
- A stable or brightening scene normally means NOOP, even during a "sunset" run.
- Exposure changes should respond to measured need, not to an expected future event.

CAMERA / CONTROL FACTS:
- The deterministic Python executor is the ONLY component allowed to talk to the camera.
- Capture cadence is owned by Python. Do NOT request CAPTURE.
- Olympus Wi-Fi can occasionally respond slowly; do not treat one slow cycle as a
  photographic reason to change exposure.
- Use the real widest lens aperture before treating aperture as maxed out.
- Prefer ISO increases over shutter speeds longer than the preferred limit.
- If current exposure is healthy, return NOOP.
- Ordinary changes should be very small and smooth. Prefer NOOP unless there is
  a clear measured exposure need.
- Aim for no more than {MAX_AI_EXPOSURE_STEP_EV:.2f} EV per decision and ignore
  tiny changes below about {AI_EXPOSURE_DEADBAND_EV:.2f} EV; Python applies a
  second deterministic exposure governor.

HOW TO USE scene_trend:
- delta_1m / delta_5m are changes in normalized median brightness.
- Negative = measured scene became darker.
- Positive = measured scene became brighter.
- "stable" means there is no convincing measured directional trend.
- Do not infer a trend from a single image when trend telemetry is available.

You may return 0 to 3 actions, chosen ONLY from:
1. {{"action":"NOOP","reason":"..."}}
2. {{"action":"SET_EXPOSURE","iso":200,"aperture":6.3,
    "shutter_seconds":0.002,"reason":"..."}}
3. {{"action":"WAIT","wait_seconds":5.0,"reason":"..."}}
4. {{"action":"ABORT","reason":"only if continuing risks corrupting the run"}}

Lens profile: {LENS_PROFILE_NAME}; physical widest f/{LENS_MIN_APERTURE:g}; commandable V5 floor f/{MIN_APERTURE:g}
Hard limits:
ISO {MIN_ISO}-{MAX_ISO}
aperture f/{MIN_APERTURE}-f/{MAX_APERTURE}; f/2.8 and f/4.8 are not commandable over Olympus Wi-Fi for this lens profile
shutter {MIN_SHUTTER_SECONDS}-{MAX_SHUTTER_SECONDS} seconds
Operator preference: after about {PREFERRED_MAX_SHUTTER_SECONDS:.3g}s, raise ISO before making shutter longer unless ISO is already near its limit.

Return ONLY JSON:
{{
  "summary": "short state assessment",
  "actions": [ ... ]
}}

Review trigger: {trigger}

Current state:
{json.dumps(state, indent=2)}
""".strip()

        content = [{"type": "input_text", "text": prompt}]
        if image_path and Path(image_path).exists():
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            content.append({
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{b64}",
                "detail": AI_IMAGE_DETAIL,
            })

        client = OpenAI(timeout=AI_TIMEOUT_SECONDS)
        response = client.responses.create(
            model=AI_MODEL,
            input=[{"role": "user", "content": content}],
        )

        raw = response.output_text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)

        actions = []
        for item in data.get("actions", [])[:3]:
            action = str(item.get("action", "NOOP")).upper()
            if action not in ALLOWED_ACTIONS:
                continue

            if action == "SET_EXPOSURE":
                iso = item.get("iso")
                aperture = item.get("aperture")
                shutter = item.get("shutter_seconds")
                if iso is not None:
                    iso = max(MIN_ISO, min(MAX_ISO, int(round(float(iso)))))
                if aperture is not None:
                    aperture = max(MIN_APERTURE, min(MAX_APERTURE, float(aperture)))
                if shutter is not None:
                    shutter = max(MIN_SHUTTER_SECONDS, min(MAX_SHUTTER_SECONDS, float(shutter)))
                actions.append(CommanderAction(
                    action=action,
                    reason=str(item.get("reason", ""))[:300],
                    iso=iso,
                    aperture=aperture,
                    shutter_seconds=shutter,
                ))
            elif action == "WAIT":
                wait_s = max(0.0, min(60.0, float(item.get("wait_seconds", 0.0))))
                actions.append(CommanderAction(
                    action=action,
                    reason=str(item.get("reason", ""))[:300],
                    wait_seconds=wait_s,
                ))
            else:
                actions.append(CommanderAction(
                    action=action,
                    reason=str(item.get("reason", ""))[:300],
                ))

        return actions, data
