"""Replay a folder of frames or a video through Night Shift at Nx speed (recorded 'night' / offline fallback).

uv run python replay.py FOLDER_OR_VIDEO [--speed 10] [--every 5] [--start 2026-11-16T23:00]
Frames are stamped start + i*every seconds, so a 10x replay of 20 frames tells a 100 s story in ~10 s.
"""
import argparse, base64, io, json, pathlib, subprocess, tempfile, time, urllib.request
from datetime import datetime, timedelta

from PIL import Image

p = argparse.ArgumentParser()
p.add_argument("src")
p.add_argument("--speed", type=float, default=10)
p.add_argument("--every", type=float, default=5, help="seconds of story time between frames")
p.add_argument("--start", help="ISO time of the first frame (default: now)")
p.add_argument("--url", default="http://localhost:8000")
p.add_argument("--night", default="live")
a = p.parse_args()

src = pathlib.Path(a.src)
if src.is_dir():
    files = sorted(f for f in src.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png"})
else:  # video: one frame per `every` seconds of footage
    tmp = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-vf", f"fps=1/{a.every}",
                    str(tmp / "f_%05d.jpg")], check=True)
    files = sorted(tmp.glob("*.jpg"))

start = datetime.fromisoformat(a.start) if a.start else datetime.now()
kept = 0
for i, f in enumerate(files):
    t0 = time.time()
    im = Image.open(f).convert("RGB")
    im.thumbnail((640, 640))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=65)
    ts = (start + timedelta(seconds=i * a.every)).isoformat(timespec="seconds")
    body = json.dumps({"image": base64.b64encode(buf.getvalue()).decode(), "ts": ts, "night": a.night}).encode()
    req = urllib.request.Request(a.url + "/frame", body, {"content-type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    kept += not r.get("skipped")
    print(f"{i+1}/{len(files)} {ts} {'skipped (no change)' if r.get('skipped') else r['event']}", flush=True)
    time.sleep(max(0, a.every / a.speed - (time.time() - t0)))
print(f"done: {kept} kept, {len(files) - kept} skipped")
