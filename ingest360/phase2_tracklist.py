#!/usr/bin/env python3
"""
phase2_tracklist.py — Phase 2 prototype: 360 video -> timestamped tracklist.

Extracts audio from a big .insv/.mp4/.mov, samples it every N seconds,
identifies each sample with Shazam (via the free `shazamio` library),
and writes a timestamped tracklist (markdown + CSV) so editing a set
means jumping straight to the drop.

Setup (once, on wifi):
    brew install ffmpeg              # or: https://evermeet.cx/ffmpeg/
    pip3 install shazamio            # may need: pip3 install shazamio --user

Usage:
    python3 phase2_tracklist.py "path/to/VID_..._00_123.insv"
    python3 phase2_tracklist.py clip.insv --step 45 --sample 12

Offline mode (train with no wifi): add --extract-only. It saves the
audio samples to a folder; rerun later with --from-samples to do the
song IDs when you're back online.
"""
import argparse
import asyncio
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def hms(sec):
    sec = int(sec)
    return f"{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}"


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return float(out.stdout.strip())


def extract_sample(src, at, length, dst):
    """Pull one mono mp3 sample starting at `at` seconds. Read-only on src."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(src),
         "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100",
         "-t", str(length), "-y", str(dst)],
        check=True)


async def identify(samples):
    from shazamio import Shazam
    shazam = Shazam()
    hits = []
    for at, wav in samples:
        try:
            r = await shazam.recognize(str(wav))
            track = r.get("track") or {}
            title, artist = track.get("title"), track.get("subtitle")
        except Exception as e:
            title = artist = None
            print(f"  [{hms(at)}] lookup failed ({e}) — skipping")
        if title:
            print(f"  [{hms(at)}] {artist} — {title}")
            hits.append({"t": at, "artist": artist, "title": title})
        else:
            print(f"  [{hms(at)}] no match (talking / crowd / transition?)")
            hits.append({"t": at, "artist": None, "title": None})
        await asyncio.sleep(1)   # be polite to the API
    return hits


def merge(hits):
    """Collapse consecutive identical songs into one entry with a start time."""
    out = []
    for h in hits:
        if not h["title"]:
            continue
        if out and out[-1]["title"] == h["title"] and out[-1]["artist"] == h["artist"]:
            out[-1]["end"] = h["t"]
        else:
            out.append({**h, "start": h["t"], "end": h["t"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--step", type=float, default=60,
                    help="seconds between samples (default 60)")
    ap.add_argument("--sample", type=float, default=12,
                    help="seconds per sample (default 12)")
    ap.add_argument("--extract-only", action="store_true",
                    help="offline: extract samples now, identify later")
    ap.add_argument("--from-samples", metavar="DIR",
                    help="identify previously extracted samples")
    args = ap.parse_args()

    video = Path(args.video)
    stem = video.stem

    if args.from_samples:
        sdir = Path(args.from_samples)
        samples = sorted((float(p.stem.split("_at_")[1]), p)
                         for p in sdir.glob("*_at_*.mp3"))
    else:
        dur = duration_of(video)
        print(f"{video.name}: {hms(dur)} long -> sampling every {args.step:.0f}s")
        sdir = Path(tempfile.mkdtemp(prefix="tracklist_")) if not args.extract_only \
            else Path(f"{stem}_samples")
        sdir.mkdir(exist_ok=True)
        samples = []
        t = 0
        while t < dur - args.sample:
            dst = sdir / f"{stem}_at_{t:.0f}.mp3"
            extract_sample(video, t, args.sample, dst)
            samples.append((t, dst))
            t += args.step
        print(f"extracted {len(samples)} samples -> {sdir}")
        if args.extract_only:
            print("offline mode: rerun with  --from-samples "
                  f"'{sdir}'  when online.")
            return

    print("identifying songs (needs internet) ...")
    hits = asyncio.run(identify(samples))
    tracks = merge(hits)

    md = [f"# Tracklist — {stem}", ""]
    rows = []
    for tr in tracks:
        md.append(f"- **{hms(tr['start'])}**  {tr['artist']} — {tr['title']}")
        rows.append([hms(tr["start"]), hms(tr["end"]), tr["artist"], tr["title"]])
    Path(f"{stem}_tracklist.md").write_text("\n".join(md))
    with open(f"{stem}_tracklist.csv", "w", newline="") as f:
        csv.writer(f).writerows([["start", "last_heard", "artist", "title"], *rows])
    print(f"\n{len(tracks)} tracks -> {stem}_tracklist.md / .csv")


if __name__ == "__main__":
    main()
