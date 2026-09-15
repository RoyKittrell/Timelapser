#!/usr/bin/env python3
"""Dry-run-first Instagram publishing scaffold for Timelapser.

The live path uses Dropbox as a temporary public video host, then asks the
Instagram Graph API to create and publish a Reel. Without credentials this
script still validates the queue item/video and reports exactly what is missing.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ENV_FILE = Path("/home/roy/Timelapser Sept2026/env/.env")
DEFAULT_GRAPH_VERSION = "v24.0"
DEFAULT_DROPBOX_FOLDER = "/Timelapser/instagram_queue"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def ffprobe_video(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate,duration",
        "-show_entries",
        "format=size,duration,format_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe not found"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip()}
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return {"ok": False, "error": f"ffprobe JSON parse failed: {exc}"}
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = data.get("format") or {}
    problems: list[str] = []
    if stream.get("codec_name") != "h264":
        problems.append("video codec is not H.264")
    if int(stream.get("width") or 0) < 540 or int(stream.get("height") or 0) < 960:
        problems.append("video is smaller than a practical Reel resolution")
    try:
        duration = float(stream.get("duration") or fmt.get("duration") or 0)
        if duration <= 0:
            problems.append("video duration is missing")
    except Exception:
        problems.append("video duration is not numeric")
    return {"ok": not problems, "problems": problems, "raw": data}


def resolve_queue_item(path: Path) -> tuple[Path, Path, Path]:
    path = path.expanduser().resolve()
    if path.is_dir():
        metadata = read_json(path / "metadata.json")
        video = Path(metadata.get("queued_video") or "")
        if not video.exists():
            videos = sorted(path.glob("*.mp4"))
            if not videos:
                raise FileNotFoundError(f"No MP4 found in queue item: {path}")
            video = videos[0]
        caption = Path(metadata.get("caption_file") or path / "caption.txt")
        status = path / "publish_status.json"
        return video, caption, status
    caption = path.with_suffix(".txt")
    status = path.with_name(path.stem + "_publish_status.json")
    return path, caption, status


def required_env() -> dict[str, list[str]]:
    return {
        "dropbox": ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"],
        "instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN"],
    }


def missing_env() -> dict[str, list[str]]:
    return {
        group: [key for key in keys if not os.environ.get(key)]
        for group, keys in required_env().items()
    }


def http_json(url: str, *, method: str = "POST", headers: dict[str, str] | None = None, data: Any = None) -> dict[str, Any]:
    body: bytes | None = None
    final_headers = dict(headers or {})
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    elif data is not None:
        body = str(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw}
        raise RuntimeError(f"HTTP {exc.code} from {url}: {payload}") from exc


def dropbox_access_token() -> str:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"],
        "client_id": os.environ["DROPBOX_APP_KEY"],
        "client_secret": os.environ["DROPBOX_APP_SECRET"],
    }
    data = http_json("https://api.dropboxapi.com/oauth2/token", data=payload)
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Dropbox token response did not include access_token: {data}")
    return str(token)


def dropbox_upload_and_link(video: Path, folder: str) -> str:
    token = dropbox_access_token()
    remote_path = f"{folder.rstrip('/')}/{video.name}"
    args = {"path": remote_path, "mode": "overwrite", "autorename": False, "mute": True}
    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps(args),
    }
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload",
        data=video.read_bytes(),
        headers=upload_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Dropbox upload failed HTTP {exc.code}: {raw}") from exc

    link_payload = {"path": remote_path}
    link_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = http_json(
        "https://api.dropboxapi.com/2/files/get_temporary_link",
        headers=link_headers,
        data=json.dumps(link_payload),
    )
    link = data.get("link")
    if not link:
        raise RuntimeError(f"Dropbox temporary-link response did not include link: {data}")
    return str(link)


def graph_url(path: str) -> str:
    version = os.environ.get("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION
    return f"https://graph.facebook.com/{version}/{path.lstrip('/')}"


def instagram_create_container(video_url: str, caption: str, *, share_to_feed: bool) -> str:
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": os.environ["IG_ACCESS_TOKEN"],
    }
    data = http_json(graph_url(f"{os.environ['IG_USER_ID']}/media"), data=payload)
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError(f"Instagram media container response did not include id: {data}")
    return str(creation_id)


def instagram_container_status(creation_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "fields": "id,status,status_code",
        "access_token": os.environ["IG_ACCESS_TOKEN"],
    })
    return http_json(graph_url(f"{creation_id}?{query}"), method="GET")


def instagram_publish(creation_id: str) -> str:
    payload = {"creation_id": creation_id, "access_token": os.environ["IG_ACCESS_TOKEN"]}
    data = http_json(graph_url(f"{os.environ['IG_USER_ID']}/media_publish"), data=payload)
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"Instagram publish response did not include id: {data}")
    return str(media_id)


def check_auth() -> dict[str, Any]:
    missing = missing_env()
    usable = {group: not keys for group, keys in missing.items()}
    return {
        "env_file": str(ENV_FILE),
        "usable": usable,
        "missing": missing,
        "graph_version": os.environ.get("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a queued Timelapser Reel to Instagram.")
    parser.add_argument("queue_item_or_video", nargs="?", type=Path)
    parser.add_argument("--caption", default=None)
    parser.add_argument("--caption-file", type=Path, default=None)
    parser.add_argument("--dropbox-folder", default=DEFAULT_DROPBOX_FOLDER)
    parser.add_argument("--check-auth", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-publish", action="store_true", help="Create/poll container but do not publish.")
    parser.add_argument("--publish", action="store_true", help="Allow the final Instagram media_publish call.")
    parser.add_argument("--share-to-feed", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=8)
    parser.add_argument("--poll-attempts", type=int, default=60)
    args = parser.parse_args()

    load_env()
    if args.check_auth:
        print(json.dumps(check_auth(), indent=2))
        return 0
    if args.queue_item_or_video is None:
        parser.error("queue_item_or_video is required unless --check-auth is used")

    video, default_caption_file, status_path = resolve_queue_item(args.queue_item_or_video)
    caption_file = args.caption_file or default_caption_file
    caption = args.caption
    if caption is None and caption_file.exists():
        caption = caption_file.read_text(encoding="utf-8").strip()
    if caption is None:
        caption = "Timelapser V5"

    probe = ffprobe_video(video)
    auth = check_auth()
    status: dict[str, Any] = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "video": str(video),
        "caption_file": str(caption_file),
        "video_probe": probe,
        "auth": auth,
        "publish_requested": bool(args.publish),
        "published": False,
    }

    if not probe.get("ok"):
        status["status"] = "blocked_video_validation"
        write_json(status_path, status)
        print(json.dumps(status, indent=2, default=str))
        return 2

    missing = auth["missing"]
    if any(missing.values()) or args.dry_run:
        status["status"] = "dry_run" if args.dry_run else "blocked_missing_credentials"
        status["caption_preview"] = caption
        write_json(status_path, status)
        print(json.dumps(status, indent=2, default=str))
        return 0 if args.dry_run else 2

    video_url = dropbox_upload_and_link(video, args.dropbox_folder)
    status["dropbox_temporary_link_created"] = True
    status["dropbox_folder"] = args.dropbox_folder

    creation_id = instagram_create_container(video_url, caption, share_to_feed=args.share_to_feed)
    status["instagram_creation_id"] = creation_id
    write_json(status_path, status)

    final_container_status: dict[str, Any] = {}
    for _ in range(max(1, args.poll_attempts)):
        final_container_status = instagram_container_status(creation_id)
        code = str(final_container_status.get("status_code") or "").upper()
        if code == "FINISHED":
            break
        if code == "ERROR":
            status["status"] = "blocked_container_error"
            status["instagram_container_status"] = final_container_status
            write_json(status_path, status)
            print(json.dumps(status, indent=2, default=str))
            return 3
        time.sleep(max(1, args.poll_seconds))
    status["instagram_container_status"] = final_container_status

    if args.no_publish or not args.publish:
        status["status"] = "container_ready_no_publish"
        write_json(status_path, status)
        print(json.dumps(status, indent=2, default=str))
        return 0

    media_id = instagram_publish(creation_id)
    status["status"] = "published"
    status["published"] = True
    status["instagram_media_id"] = media_id
    write_json(status_path, status)
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
