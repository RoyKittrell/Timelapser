# Import completed timelapses to Apple Photos

Run on the Mac:

```sh
python3 import_timelapses_to_photos.py
```

Use your normal Python 3 installation. If macOS's `/usr/bin/python3` asks for
an Xcode license, run the command from your Conda environment instead.

The script lists completed `_final_instagram-reel_*.mp4` renders on the Pi's
external drive, copies missing videos into `rendered_videos/`, and imports them
into Photos. Each imported video is also added to the **Timelapser.118** album,
which is created automatically when needed. Existing imported videos are added
to the album without being imported again. It writes `photos_imports.tsv` next
to the script. That ledger is
local state and is not committed to Git. Never delete it casually.

Each transfer uses a temporary `.part` file and checks the final byte count.
An interrupted transfer starts over on the next run. Each Photos import is
recorded as `pending` before calling Photos; on restart, the script checks the
Photos library by exact filename and records an existing match rather than
importing it twice. Completed entries store the Photos media ID and SHA-256.

`--dry-run` lists pending Pi videos without copying/importing. `--local-only`
uses files already in `rendered_videos/`, useful when the Pi is unreachable.
The first Photos call may ask macOS to allow Terminal/Codex to control Photos.
