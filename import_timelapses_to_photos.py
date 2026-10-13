#!/usr/bin/env python3
"""Pull completed Pi renders to this Mac and import them into Apple Photos once."""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEST = ROOT / "rendered_videos"
LEDGER = ROOT / "photos_imports.tsv"
REMOTE = "timelapser"
REMOTE_ROOT = "/media/roy/My Passport/Timelapser"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
               "-o", "HostKeyAlias=timelapser128gb.local",
               "-i", str(Path.home() / ".ssh/timelapser")]
FIELDS = ("source", "filename", "sha256", "status", "photos_id", "updated_at")


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def remote_videos():
    code = """
import json, os, time
root = '/media/roy/My Passport/Timelapser'
if not os.path.ismount('/media/roy/My Passport'):
    raise SystemExit('Pi external drive is not mounted')
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('_')]
    for name in files:
        if not name.endswith('.mp4') or '_final' not in name or '_instagram-reel_' not in name:
            continue
        path = os.path.join(base, name)
        size = os.path.getsize(path)
        if size > 0 and time.time() - os.path.getmtime(path) > 120:
            print(json.dumps({'path': path, 'size': size, 'name': name}))
"""
    result = run(["ssh", *SSH_OPTIONS, REMOTE, "python3 -c " + shlex.quote(code)],
                 capture_output=True, timeout=45)
    return [json.loads(line) for line in result.stdout.splitlines()]


def remote_requests():
    code = """
import json, os
from pathlib import Path
root = Path('/media/roy/My Passport/Timelapser')
for request in root.glob('*/photos_import_request.json'):
    try:
        payload = json.loads(request.read_text())
        if payload.get('status') != 'requested':
            continue
        video = request.parent / payload['video_name']
        if video.is_file() and video.stat().st_size > 0:
            print(json.dumps({'path': str(video), 'size': video.stat().st_size,
                              'name': video.name, 'request_path': str(request)}))
    except (OSError, ValueError, KeyError, TypeError):
        continue
"""
    result = run(["ssh", *SSH_OPTIONS, REMOTE, "python3 -c " + shlex.quote(code)],
                 capture_output=True, timeout=45)
    return [json.loads(line) for line in result.stdout.splitlines()]


def update_remote_request(request_path, status, message=""):
    code = """
import json, os, sys
from datetime import datetime
from pathlib import Path
path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text())
except Exception:
    payload = {}
payload.update(status=sys.argv[2], message=sys.argv[3],
               updated_at=datetime.now().astimezone().isoformat(timespec='seconds'))
temporary = path.with_suffix('.json.tmp')
temporary.write_text(json.dumps(payload, indent=2) + '\\n')
os.replace(temporary, path)
"""
    run(["ssh", *SSH_OPTIONS, REMOTE,
         "python3 -c " + shlex.quote(code) + " " + " ".join(
             shlex.quote(value) for value in (request_path, status, message)
         )], capture_output=True, timeout=45)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ledger_rows():
    if not LEDGER.exists():
        return {}
    with LEDGER.open(newline="", encoding="utf-8") as stream:
        return {row["filename"]: row for row in csv.DictReader(stream, delimiter="\t")}


def save_ledger(rows):
    temp = LEDGER.with_suffix(".tsv.tmp")
    with temp.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows[name] for name in sorted(rows))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, LEDGER)


def photos_id(filename):
    script = '''on run argv
    set targetName to item 1 of argv
    tell application "Photos"
        set matches to (every media item whose filename is targetName)
        if (count of matches) > 0 then return id of item 1 of matches
    end tell
    return ""
end run'''
    return run(["osascript", "-e", script, filename], capture_output=True).stdout.strip()


def import_photo(path):
    script = '''on run argv
    set sourceFile to POSIX file (item 1 of argv) as alias
    tell application "Photos"
        set importedItems to import {sourceFile} with skip check duplicates
        if (count of importedItems) is not 1 then error "Photos did not return one imported item"
        return id of item 1 of importedItems
    end tell
end run'''
    return run(["osascript", "-e", script, str(path)], capture_output=True).stdout.strip()


def pull(item):
    DEST.mkdir(exist_ok=True)
    target = DEST / item["name"]
    if target.exists() and target.stat().st_size == item["size"]:
        return target
    part = target.with_name(target.name + ".part")
    if part.exists():
        part.unlink()
    run(["scp", *SSH_OPTIONS, f"{REMOTE}:{item['path']}", str(part)], timeout=1800)
    if part.stat().st_size != item["size"]:
        raise RuntimeError(f"Incomplete transfer: {item['name']}")
    os.replace(part, target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true", help="Use MP4s already in rendered_videos")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without copying or importing")
    parser.add_argument("--name", help="Import only the completed video with this exact filename")
    parser.add_argument("--requested", action="store_true", help="Process dashboard requests queued on the Pi")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Apple Photos import must run on the Mac")
    with (ROOT / ".photos_import.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            remote = [] if args.local_only else (remote_requests() if args.requested else remote_videos())
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f"Cannot list Pi videos: {error}", file=sys.stderr)
            return 1
        items = ({item["name"]: item for item in remote} if not args.local_only else
                 {p.name: {"name": p.name, "path": str(p), "size": p.stat().st_size}
                  for p in DEST.rglob("*.mp4") if "_final" in p.name and "_instagram-reel_" in p.name})
        if args.name:
            items = {name: item for name, item in items.items() if name == args.name}
            if not items:
                print(f"No completed video found named {args.name}", file=sys.stderr)
                return 1
        rows = ledger_rows()
        for name, item in sorted(items.items()):
            request_path = item.get("request_path")
            row = rows.get(name)
            if row and row["status"] == "imported":
                print(f"Already imported: {name}")
                if request_path:
                    update_remote_request(request_path, "imported", "Already present in Apple Photos")
                continue
            if args.dry_run:
                print(f"Would import: {name}")
                continue
            try:
                if request_path:
                    update_remote_request(request_path, "processing", "Downloading to Mac")
                path = Path(item["path"]) if args.local_only else pull(item)
                fingerprint = sha256(path)
                if row and row["sha256"] and row["sha256"] != fingerprint:
                    raise RuntimeError(f"Video changed since previous import attempt: {name}")
                rows[name] = dict(source=item["path"], filename=name, sha256=fingerprint,
                                  status="pending", photos_id="",
                                  updated_at=datetime.now().astimezone().isoformat())
                save_ledger(rows)
                found = photos_id(name)
                identifier = found or import_photo(path)
                if not identifier:
                    raise RuntimeError(f"Photos returned no media ID for {name}")
                rows[name].update(status="imported", photos_id=identifier,
                                  updated_at=datetime.now().astimezone().isoformat())
                save_ledger(rows)
                print(f"{'Already in Photos' if found else 'Imported'}: {name}")
                if request_path:
                    update_remote_request(request_path, "imported", "Imported into Apple Photos")
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
                print(f"FAILED {name}: {error}", file=sys.stderr)
                if request_path:
                    try:
                        update_remote_request(request_path, "failed", str(error))
                    except Exception:
                        pass
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
