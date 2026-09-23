#!/usr/bin/env python3
"""Publish an ordered Instagram video carousel from public HTTPS URLs."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ENV_FILE = Path("/home/roy/Timelapser Sept2026/env/.env")
DEFAULT_API_VERSION = "v24.0"
DEFAULT_API_HOST = "https://graph.instagram.com"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(path: str, *, method: str = "GET", data: dict[str, str] | None = None) -> dict[str, Any]:
    version = os.environ.get("META_GRAPH_VERSION", DEFAULT_API_VERSION)
    host = os.environ.get("INSTAGRAM_GRAPH_HOST", DEFAULT_API_HOST).rstrip("/")
    url = f"{host}/{version}/{path.lstrip('/')}"
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instagram API HTTP {exc.code}: {payload}") from exc


def account_id(token: str) -> str:
    query = urllib.parse.urlencode({
        "fields": "id,user_id,username,account_type",
        "access_token": token,
    })
    profile = request(f"me?{query}")
    user_id = profile.get("user_id") or profile.get("id")
    if not user_id:
        raise RuntimeError(f"Instagram profile response had no account ID: {profile}")
    print(f"Authenticated Instagram account: {profile.get('username')} ({user_id})", flush=True)
    return str(user_id)


def create_video_item(user_id: str, token: str, video_url: str) -> str:
    result = request(
        f"{user_id}/media",
        method="POST",
        data={
            "media_type": "VIDEO",
            "video_url": video_url,
            "is_carousel_item": "true",
            "access_token": token,
        },
    )
    container = result.get("id")
    if not container:
        raise RuntimeError(f"Video item response had no container ID: {result}")
    return str(container)


def wait_until_ready(container_id: str, token: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        query = urllib.parse.urlencode({
            "fields": "id,status,status_code",
            "access_token": token,
        })
        status = request(f"{container_id}?{query}")
        code = str(status.get("status_code") or "").upper()
        print(f"Container {container_id}: {code or 'PROCESSING'}", flush=True)
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Container failed: {status}")
        time.sleep(8)
    raise TimeoutError(f"Container {container_id} was not ready after {timeout}s")


def create_carousel(user_id: str, token: str, children: list[str], caption: str) -> str:
    result = request(
        f"{user_id}/media",
        method="POST",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
            "access_token": token,
        },
    )
    container = result.get("id")
    if not container:
        raise RuntimeError(f"Carousel response had no container ID: {result}")
    return str(container)


def create_reel(user_id: str, token: str, video_url: str, caption: str) -> str:
    result = request(
        f"{user_id}/media",
        method="POST",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
    )
    container = result.get("id")
    if not container:
        raise RuntimeError(f"Reel response had no container ID: {result}")
    return str(container)


def publish(user_id: str, token: str, creation_id: str) -> str:
    result = request(
        f"{user_id}/media_publish",
        method="POST",
        data={"creation_id": creation_id, "access_token": token},
    )
    media_id = result.get("id")
    if not media_id:
        raise RuntimeError(f"Publish response had no media ID: {result}")
    return str(media_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_urls", nargs="+", help="Public HTTPS videos in carousel order")
    parser.add_argument("--caption", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--publish", action="store_true", help="Perform the final public publish call")
    parser.add_argument("--reel", action="store_true", help="Publish one URL as a 9:16 Reel")
    args = parser.parse_args()
    if args.reel and len(args.video_urls) != 1:
        parser.error("--reel requires exactly one video URL")
    if not args.reel and not 2 <= len(args.video_urls) <= 10:
        parser.error("Instagram carousels require 2 to 10 items")
    if any(not url.startswith("https://") for url in args.video_urls):
        parser.error("Every carousel item must use public HTTPS")

    load_env()
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise SystemExit(f"IG_ACCESS_TOKEN missing from {ENV_FILE}")

    user_id = account_id(token)
    if args.reel:
        print("Creating Reel", flush=True)
        container = create_reel(user_id, token, args.video_urls[0], args.caption)
        wait_until_ready(container, token, args.timeout)
        if not args.publish:
            print(json.dumps({"ready": True, "reel_id": container}, indent=2))
            return 0
        media_id = publish(user_id, token, container)
        print(json.dumps({"published": True, "media_id": media_id, "reel_id": container}, indent=2))
        return 0

    children = []
    for position, url in enumerate(args.video_urls, start=1):
        print(f"Creating video item {position}/{len(args.video_urls)}", flush=True)
        child = create_video_item(user_id, token, url)
        wait_until_ready(child, token, args.timeout)
        children.append(child)

    print("Creating ordered carousel", flush=True)
    carousel = create_carousel(user_id, token, children, args.caption)
    wait_until_ready(carousel, token, args.timeout)
    if not args.publish:
        print(json.dumps({"ready": True, "carousel_id": carousel, "children": children}, indent=2))
        return 0

    media_id = publish(user_id, token, carousel)
    print(json.dumps({"published": True, "media_id": media_id, "carousel_id": carousel}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
