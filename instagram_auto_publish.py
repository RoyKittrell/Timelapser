#!/usr/bin/env python3
"""Publish a rendered run or manually prepared folder to Instagram safely."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from instagram_carousel_publish import (
    ENV_FILE,
    account_id,
    create_carousel,
    create_reel,
    create_video_item,
    load_env,
    publish,
    request,
    wait_until_ready,
)
from post_metadata import write_post_files


RECEIPT_NAME = "instagram_publish_receipt.json"
CLOUDFLARED = Path.home() / ".local/bin/cloudflared"
KINDS = ("clean", "director", "brightness")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def find_one(folder: Path, patterns: list[str]) -> Path:
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(folder.glob(pattern))
    matches = sorted(set(path.resolve() for path in matches if path.is_file()))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one match in {folder} for {patterns}; found {len(matches)}")
    return matches[0]


def resolve_videos(folder: Path) -> dict[str, Any]:
    carousel = {
        kind: find_one(folder, [f"*_instagram-carousel_{kind}.mp4", f"*carousel*{kind}*.mp4"])
        for kind in KINDS
    }
    reel = find_one(folder, ["*_instagram-reel_clean.mp4", "*reel*clean*.mp4"])
    return {"carousel": carousel, "reel": reel}


def caption_for(folder: Path, override: str | None) -> str:
    if override:
        return override.strip()
    caption_path = folder / "caption.txt"
    if caption_path.exists():
        return caption_path.read_text(encoding="utf-8").strip()
    if (folder / "telemetry.csv").exists():
        return str(write_post_files(folder)["caption"]).strip()
    raise RuntimeError("No caption.txt found; provide --caption for a manual upload folder")


class TemporaryHost:
    def __init__(self, files: dict[str, Path]):
        self.files = files
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.server: subprocess.Popen[str] | None = None
        self.tunnel: subprocess.Popen[str] | None = None
        self.log_handle: Any = None
        self.base_url = ""

    def __enter__(self) -> dict[str, str]:
        if not CLOUDFLARED.is_file():
            raise RuntimeError(f"cloudflared is not installed at {CLOUDFLARED}")
        self.temp = tempfile.TemporaryDirectory(prefix="timelapser-instagram-")
        root = Path(self.temp.name)
        for name, source in self.files.items():
            (root / name).symlink_to(source)
        self.server = subprocess.Popen(
            ["python3", "-m", "http.server", "18765", "--bind", "127.0.0.1"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        log_path = root / "cloudflared.log"
        self.log_handle = log_path.open("w", encoding="utf-8")
        self.tunnel = subprocess.Popen(
            [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:18765"],
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pattern = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            match = pattern.search(text)
            if match:
                self.base_url = match.group(0)
                break
            if self.tunnel.poll() is not None:
                raise RuntimeError(f"cloudflared exited early: {text[-2000:]}")
            time.sleep(1)
        if not self.base_url:
            raise TimeoutError("Timed out waiting for temporary public URL")
        urls = {name: f"{self.base_url}/{name}" for name in self.files}
        try:
            for url in urls.values():
                deadline = time.monotonic() + 90
                last_error: Exception | None = None
                while time.monotonic() < deadline:
                    try:
                        request_obj = urllib.request.Request(url, method="HEAD")
                        with urllib.request.urlopen(request_obj, timeout=30) as response:
                            if response.status == 200:
                                break
                            last_error = RuntimeError(
                                f"Temporary host returned HTTP {response.status}: {url}"
                            )
                    except urllib.error.URLError as exc:
                        if isinstance(exc.reason, socket.gaierror):
                            print(
                                "Local DNS cannot resolve the fresh trycloudflare hostname; "
                                "continuing because Meta resolves the public media URL independently.",
                                flush=True,
                            )
                            break
                        last_error = exc
                    except Exception as exc:
                        last_error = exc
                    if self.tunnel.poll() is not None:
                        raise RuntimeError(f"cloudflared exited while verifying {url}")
                    time.sleep(2)
                else:
                    raise TimeoutError(
                        f"Temporary host did not become reachable within 90 seconds: "
                        f"{url}: {last_error}"
                    )
            return urls
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_: object) -> None:
        for process in (self.tunnel, self.server):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        if self.log_handle:
            self.log_handle.close()
        if self.temp:
            self.temp.cleanup()


def permalink(media_id: str, token: str) -> str:
    from urllib.parse import urlencode

    data = request(f"{media_id}?{urlencode({'fields': 'permalink', 'access_token': token})}")
    return str(data.get("permalink") or "")


def publish_folder(
    folder: Path,
    *,
    caption: str | None,
    skip_carousel: bool,
    skip_reel: bool,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    folder = folder.expanduser().resolve()
    videos = resolve_videos(folder)
    text = caption_for(folder, caption)
    receipt_path = folder / RECEIPT_NAME
    receipt = {} if force else read_json(receipt_path)
    receipt.setdefault("folder", str(folder))
    receipt.setdefault("caption", text)
    receipt.setdefault("carousel", {})
    receipt.setdefault("reel", {})
    receipt["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    uncertain = [
        name
        for name in ("carousel", "reel")
        if receipt[name].get("state") == "publishing" and not receipt[name].get("media_id")
    ]
    if uncertain and not force:
        raise RuntimeError(
            "Previous publish result is uncertain for "
            + ", ".join(uncertain)
            + "; inspect Instagram, then rerun with --force only if no post exists"
        )

    pending_carousel = not skip_carousel and not receipt["carousel"].get("media_id")
    pending_reel = not skip_reel and not receipt["reel"].get("media_id")
    if dry_run:
        return {"status": "validated", "videos": videos, "pending_carousel": pending_carousel, "pending_reel": pending_reel}
    if not pending_carousel and not pending_reel:
        receipt["status"] = "complete"
        atomic_json(receipt_path, receipt)
        return receipt

    load_env()
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(f"IG_ACCESS_TOKEN missing from {ENV_FILE}")
    user_id = account_id(token)
    hosted: dict[str, Path] = {}
    if pending_carousel:
        hosted.update({f"carousel-{kind}.mp4": videos["carousel"][kind] for kind in KINDS})
    if pending_reel:
        hosted["clean-reel.mp4"] = videos["reel"]

    try:
        with TemporaryHost(hosted) as urls:
            if pending_carousel:
                children = []
                for kind in KINDS:
                    child = create_video_item(user_id, token, urls[f"carousel-{kind}.mp4"])
                    wait_until_ready(child, token, 900)
                    children.append(child)
                container = create_carousel(user_id, token, children, text)
                wait_until_ready(container, token, 900)
                receipt["carousel"]["state"] = "publishing"
                atomic_json(receipt_path, receipt)
                media_id = publish(user_id, token, container)
                receipt["carousel"] = {"state": "published", "media_id": media_id, "permalink": permalink(media_id, token)}
                atomic_json(receipt_path, receipt)

            if pending_reel:
                container = create_reel(user_id, token, urls["clean-reel.mp4"], text, share_to_feed=False)
                wait_until_ready(container, token, 900)
                receipt["reel"]["state"] = "publishing"
                atomic_json(receipt_path, receipt)
                media_id = publish(user_id, token, container)
                receipt["reel"] = {"state": "published", "media_id": media_id, "permalink": permalink(media_id, token)}
                atomic_json(receipt_path, receipt)
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        atomic_json(receipt_path, receipt)
        raise

    receipt["status"] = "complete"
    receipt.pop("error", None)
    receipt["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_json(receipt_path, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-publish one completed run or manually prepared video folder.")
    parser.add_argument("folder", type=Path, help="Completed run folder or manual inbox folder")
    parser.add_argument("--caption")
    parser.add_argument("--skip-carousel", action="store_true")
    parser.add_argument("--skip-reel", action="store_true")
    parser.add_argument("--force", action="store_true", help="Ignore the local receipt; may create duplicate posts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = publish_folder(
        args.folder,
        caption=args.caption,
        skip_carousel=args.skip_carousel,
        skip_reel=args.skip_reel,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
