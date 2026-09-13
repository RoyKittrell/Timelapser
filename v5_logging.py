import csv
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


def _iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class RunLogger:
    """Persistent human + machine-readable forensic logging."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()

        self.run_log_path = self.run_dir / "run.log"
        self.events_path = self.run_dir / "events.jsonl"
        self.camera_commands_path = self.run_dir / "camera_commands.jsonl"
        self.ai_decisions_path = self.run_dir / "ai_decisions.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"
        self.telemetry_path = self.run_dir / "telemetry.csv"
        self.summary_path = self.run_dir / "run_summary.json"

        self.logger = logging.getLogger(f"timelapser_v5_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.FileHandler(self.run_log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s",
                                               datefmt="%H:%M:%S"))
        self.logger.addHandler(handler)

        self._telemetry_header_written = self.telemetry_path.exists() and self.telemetry_path.stat().st_size > 0

    def human(self, message: str):
        print(message, flush=True)
        self.logger.info(message)

    def _append_jsonl(self, path: Path, record: dict):
        record = dict(record)
        record.setdefault("time", _iso_now())
        with self.lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def event(self, event_type: str, **data):
        self._append_jsonl(self.events_path, {"type": event_type, **data})

    def camera_command(self, **data):
        self._append_jsonl(self.camera_commands_path, data)

    def ai_decision(self, **data):
        self._append_jsonl(self.ai_decisions_path, data)

    def error(self, where: str, exc: Exception | None = None, **data):
        record = {"where": where, **data}
        if exc is not None:
            record.update({
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": "".join(traceback.format_exception(exc)),
            })
        self._append_jsonl(self.errors_path, record)

    def telemetry(self, row: dict):
        with self.lock:
            write_header = not self._telemetry_header_written
            with self.telemetry_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if write_header:
                    writer.writeheader()
                    self._telemetry_header_written = True
                writer.writerow(row)

    def write_summary(self, summary: dict):
        payload = {"written_at": _iso_now(), **summary}
        with self.summary_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
