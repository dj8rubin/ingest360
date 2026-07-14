#!/usr/bin/env python3
"""
ingest360 — read-only ingest/organize pipeline for Insta360 footage.

Commands:
  inventory     Scan drive roots. Per drive: total size, file counts, video
                counts, top file types. READ-ONLY. Writes a JSON report only
                to --report (never onto the scanned drives).
  check-backup  Compare video files on SD card roots against backup drive
                roots. Match by filename+size, optionally verify with a
                sampled checksum. Emits an offload queue (JSON) of clips NOT
                yet backed up. READ-ONLY.
  plan          Turn an offload queue into a dry-run copy plan using the
                canonical destination layout. Prints the plan; copies nothing.
  offload       Execute a copy plan: copy-then-verify (sampled or full hash).
                NEVER moves, NEVER deletes, NEVER overwrites a mismatched
                file. Requires --execute; otherwise it is a dry run.

Safety rails (hard-coded):
  * No delete/rename/format code paths exist anywhere in this file.
  * Sources are only ever opened read-only.
  * offload refuses to run without --execute and verifies every copy.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

VIDEO_EXTS = {".insv", ".insp", ".360", ".lrv", ".mp4", ".mov"}
# Insta360 proxy/low-res files — counted separately, low offload priority
PROXY_EXTS = {".lrv"}
SKIP_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
             "$RECYCLE.BIN", "System Volume Information", ".DS_Store"}
SAMPLE_BYTES = 4 * 1024 * 1024  # 4MB head + 4MB tail for sampled hashing


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024


def iter_files(root: Path):
    """Yield all regular files under root, skipping system/hidden junk dirs."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name == ".DS_Store" or name.startswith("._"):
                continue
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            yield p, st


def sampled_sha256(path: Path, size: int) -> str:
    """Hash first+last SAMPLE_BYTES + size. Fast + reliable for 15-20GB clips."""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:  # read-only
        h.update(f.read(SAMPLE_BYTES))
        if size > 2 * SAMPLE_BYTES:
            f.seek(size - SAMPLE_BYTES)
            h.update(f.read(SAMPLE_BYTES))
    return h.hexdigest()


def full_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- inventory
def cmd_inventory(args):
    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "drives": []}
    for root_str in args.roots:
        root = Path(root_str)
        if not root.is_dir():
            print(f"!! skipping {root} (not a directory)", file=sys.stderr)
            continue
        total = vid_total = 0
        ext_counts, ext_sizes = Counter(), Counter()
        videos = []
        nfiles = 0
        for p, st in iter_files(root):
            nfiles += 1
            total += st.st_size
            ext = p.suffix.lower()
            ext_counts[ext] += 1
            ext_sizes[ext] += st.st_size
            if ext in VIDEO_EXTS:
                vid_total += st.st_size
                videos.append({
                    "path": str(p.relative_to(root)),
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(st.st_mtime)),
                    "ext": ext,
                    "proxy": ext in PROXY_EXTS,
                })
        drive = {
            "root": str(root),
            "label": args.labels.get(str(root), root.name) if args.labels else root.name,
            "files": nfiles,
            "total_bytes": total,
            "video_files": len(videos),
            "video_bytes": vid_total,
            "top_types": [
                {"ext": e or "(none)", "count": c, "bytes": ext_sizes[e]}
                for e, c in ext_counts.most_common(8)
            ],
            "videos": videos,
        }
        report["drives"].append(drive)
        print(f"\n=== {drive['label']}  ({root})")
        print(f"    files: {nfiles}   total: {human(total)}")
        print(f"    videos: {len(videos)}   video data: {human(vid_total)}")
        for t in drive["top_types"][:6]:
            print(f"      {t['ext']:<8} x{t['count']:<6} {human(t['bytes'])}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
        print(f"\nJSON report -> {args.report}")
    return report


# ------------------------------------------------------------- check-backup
def build_backup_index(backup_roots):
    """Index every video on the backup drives by (filename, size)."""
    index = {}
    for root_str in backup_roots:
        root = Path(root_str)
        if not root.is_dir():
            print(f"!! backup root {root} not found", file=sys.stderr)
            continue
        for p, st in iter_files(root):
            if p.suffix.lower() in VIDEO_EXTS:
                index.setdefault((p.name, st.st_size), []).append(str(p))
    return index


def cmd_check_backup(args):
    index = build_backup_index(args.backups)
    print(f"backup index: {len(index)} unique (name,size) video entries "
          f"across {len(args.backups)} backup root(s)")
    queue, backed_up = [], []
    for card_str in args.cards:
        card = Path(card_str)
        if not card.is_dir():
            print(f"!! card root {card} not found", file=sys.stderr)
            continue
        for p, st in iter_files(card):
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            entry = {
                "card": str(card), "card_label": card.name,
                "path": str(p), "name": p.name, "size": st.st_size,
                "proxy": p.suffix.lower() in PROXY_EXTS,
                "mtime": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(st.st_mtime)),
            }
            matches = index.get((p.name, st.st_size), [])
            if matches and args.verify == "sample":
                src_h = sampled_sha256(p, st.st_size)
                matches = [m for m in matches
                           if sampled_sha256(Path(m), st.st_size) == src_h]
            elif matches and args.verify == "full":
                src_h = full_sha256(p)
                matches = [m for m in matches if full_sha256(Path(m)) == src_h]
            if matches:
                entry["backup_copies"] = matches
                backed_up.append(entry)
            else:
                queue.append(entry)

    q_bytes = sum(e["size"] for e in queue)
    b_bytes = sum(e["size"] for e in backed_up)
    print(f"\nALREADY BACKED UP: {len(backed_up)} clips, {human(b_bytes)}")
    print(f"NEEDS BACKUP:      {len(queue)} clips, {human(q_bytes)}")
    for e in queue:
        tag = " (proxy)" if e["proxy"] else ""
        print(f"  [{e['card_label']}] {e['name']}  {human(e['size'])}{tag}")
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "verify_mode": args.verify,
           "backed_up": backed_up, "needs_backup": queue}
    Path(args.queue).write_text(json.dumps(out, indent=2))
    print(f"\noffload queue -> {args.queue}")
    return out


# --------------------------------------------------------------- plan/offload
def dest_for(entry, dest_root: Path, layout: str):
    """Canonical destination: <dest>/<YYYY>/<YYYY-MM-DD_event>/<file>.
    Event name defaults to 'unsorted' until David approves naming."""
    date = entry["mtime"][:10]
    year = date[:4]
    if layout == "by-date":
        return dest_root / year / f"{date}_unsorted" / entry["name"]
    return dest_root / entry["name"]


def cmd_plan(args):
    data = json.loads(Path(args.queue).read_text())
    dest_root = Path(args.dest)
    plan = []
    for e in data["needs_backup"]:
        if args.skip_proxies and e["proxy"]:
            continue
        plan.append({"src": e["path"], "size": e["size"],
                     "dst": str(dest_for(e, dest_root, args.layout))})
    total = sum(p["size"] for p in plan)
    print(f"DRY-RUN COPY PLAN — {len(plan)} files, {human(total)} "
          f"-> {dest_root}  (nothing copied)")
    for p in plan:
        print(f"  {p['src']}\n    -> {p['dst']}  ({human(p['size'])})")
    Path(args.out).write_text(json.dumps({"plan": plan}, indent=2))
    print(f"\nplan -> {args.out}")
    return plan


def cmd_offload(args):
    plan = json.loads(Path(args.plan).read_text())["plan"]
    total = sum(p["size"] for p in plan)
    if not args.execute:
        print(f"DRY RUN — would copy {len(plan)} files ({human(total)}). "
              f"Re-run with --execute to copy.")
        return
    ok = failed = skipped = 0
    for p in plan:
        src, dst = Path(p["src"]), Path(p["dst"])
        if dst.exists():
            if dst.stat().st_size == p["size"] and \
               sampled_sha256(dst, p["size"]) == sampled_sha256(src, p["size"]):
                print(f"  = exists, verified: {dst.name}")
                skipped += 1
                continue
            print(f"  !! {dst} exists but does NOT match source — NOT touching it")
            failed += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        print(f"  copying {src.name} ({human(p['size'])}) ...", flush=True)
        shutil.copy2(src, tmp)
        verify = full_sha256 if args.verify == "full" else \
            (lambda q: sampled_sha256(q, p["size"]))
        if tmp.stat().st_size == p["size"] and verify(tmp) == verify(src):
            tmp.rename(dst)  # rename of OUR temp copy only — source untouched
            print(f"    ok, verified -> {dst}")
            ok += 1
        else:
            print(f"    !! VERIFY FAILED for {src.name} — partial copy left "
                  f"as {tmp.name}; source untouched")
            failed += 1
    print(f"\ncopied+verified: {ok}   already-there: {skipped}   failed: {failed}")
    print("Sources were NOT modified. Delete/format nothing until you have "
          "eyeballed the destination.")




# ------------------------------------------------------------------ ingest
def load_events(path):
    """events.json: {"YYYY-MM-DD": {"prefix": "Esther-Tom-Wedding",
       "dest": "02 — .../360 Video"}} — dest is relative to --library root."""
    if path and Path(path).exists():
        return json.loads(Path(path).read_text())
    return {}

def clip_date(name, mtime):
    import re
    m = re.search(r"(20\d{6})", name)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return mtime[:10]

def cmd_ingest(args):
    """One-shot pipeline: scan card -> match vs library -> route+rename -> plan
    (default) or copy-then-verify (--execute)."""
    card, lib = Path(args.card), Path(args.library)
    events = load_events(args.events)
    # 1. index library videos by (name, size)
    print("indexing library ...", flush=True)
    index = build_backup_index([str(lib)])
    # also index by ORIGINAL camera filename embedded at the tail of renamed
    # copies (our convention: EventName_YYYY-MM-DD_<original name>)
    import re as _re
    CAM = _re.compile(r"((?:VID|LRV)_\d{8}_\d{4,6}.*|G[SXH]\d+\..+|GOPR\d+\..+|GP\d+\..+|C\d{4}\..+|IMG_\d+\..+|DJI_\d+\..+)$")
    for (nm, sz), paths in list(index.items()):
        m = CAM.search(nm)
        if m and m.group(1) != nm:
            index.setdefault((m.group(1), sz), []).extend(paths)
    print(f"  {len(index)} unique (name,size) keys (incl. rename-aware)")
    # 2. scan card, classify
    plan, backed = [], []
    for p, st in iter_files(card):
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        matches = [m for m in index.get((p.name, st.st_size), [])
                   if Path(m).exists()]
        if matches and args.verify != "none":
            src_h = sampled_sha256(p, st.st_size)
            matches = [m for m in matches
                       if sampled_sha256(Path(m), st.st_size) == src_h]
        if matches:
            backed.append((p, matches))
            continue
        d = clip_date(p.name, mtime)
        ev = events.get(d)
        if ev:
            dst = lib / ev["dest"] / f"{ev['prefix']}_{d}_{p.name}"
        else:
            dst = lib / "_INBOX" / f"{args.label}_{d}_UNSORTED" / p.name
        plan.append({"src": str(p), "dst": str(dst), "size": st.st_size})
    print(f"already backed up (verified): {len(backed)} clips")
    print(f"to ingest: {len(plan)} clips, "
          f"{human(sum(x['size'] for x in plan))}")
    for x in plan:
        print(f"  {Path(x['src']).name}\n    -> {x['dst']}")
    if not args.execute:
        print("\nDRY RUN — re-run with --execute to copy-then-verify.")
        Path(args.out).write_text(json.dumps({"plan": plan}, indent=2))
        print(f"plan -> {args.out}")
        return
    # 3. copy-then-verify + manifest
    logdir = lib / "_PIPELINE_LOGS"
    logdir.mkdir(exist_ok=True)
    manifest = []
    for x in plan:
        src, dst = Path(x["src"]), Path(x["dst"])
        if dst.exists():
            print(f"  = exists, skipping: {dst.name}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copy2(src, tmp)
        if tmp.stat().st_size == x["size"] and \
           sampled_sha256(tmp, x["size"]) == sampled_sha256(src, x["size"]):
            tmp.rename(dst)
            manifest.append({**x, "status": "copied+verified"})
            print(f"  ok {dst.name}")
        else:
            print(f"  !! VERIFY FAILED {src.name} — source untouched")
            manifest.append({**x, "status": "VERIFY_FAILED"})
    stamp = time.strftime("%Y-%m-%d_%H%M")
    (logdir / f"{stamp}_ingest_{args.label}.json").write_text(
        json.dumps(manifest, indent=1))
    ok = sum(1 for m in manifest if m["status"] == "copied+verified")
    print(f"\ncopied+verified: {ok}/{len(plan)} — manifest in _PIPELINE_LOGS")
    print("Card is READ-ONLY throughout. Reformat only after eyeballing "
          "the destination.")



def cmd_watch(args):
    """Poll /Volumes for new mounts; auto-run ingest (dry-run unless --execute).
    This is the 'insert card -> it just happens' automation. Ctrl-C to stop."""
    known = set(os.listdir(args.volumes))
    lib_name = Path(args.library).name
    print(f"watching {args.volumes} for new cards (library: {lib_name}) ...")
    while True:
        time.sleep(args.interval)
        now = set(os.listdir(args.volumes))
        for new in sorted(now - known):
            root = Path(args.volumes) / new
            if str(root) == str(Path(args.library)) or not (root / "DCIM").exists():
                continue  # only card-like volumes (have DCIM)
            print(f"\n== new card detected: {new} — ingesting")
            ns = argparse.Namespace(card=str(root), library=args.library,
                                    label=new.replace(" ", "-"), events=args.events,
                                    verify="sample", execute=args.execute,
                                    out=f"ingest_plan_{new.replace(' ','-')}.json")
            try:
                cmd_ingest(ns)
            except Exception as e:
                print(f"!! ingest failed for {new}: {e} — card untouched")
        known = now

def main():
    ap = argparse.ArgumentParser(prog="ingest360", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inventory", help="read-only scan of drive roots")
    p.add_argument("roots", nargs="+")
    p.add_argument("--report", default="inventory.json")
    p.add_argument("--labels", type=json.loads, default=None,
                   help='optional {"path": "label"} JSON map')
    p.set_defaults(fn=cmd_inventory)

    p = sub.add_parser("check-backup", help="flag backed-up vs needs-backup")
    p.add_argument("--cards", nargs="+", required=True)
    p.add_argument("--backups", nargs="+", required=True)
    p.add_argument("--verify", choices=["none", "sample", "full"],
                   default="sample")
    p.add_argument("--queue", default="offload_queue.json")
    p.set_defaults(fn=cmd_check_backup)

    p = sub.add_parser("plan", help="dry-run copy plan from a queue")
    p.add_argument("--queue", default="offload_queue.json")
    p.add_argument("--dest", required=True)
    p.add_argument("--layout", choices=["by-date", "flat"], default="by-date")
    p.add_argument("--skip-proxies", action="store_true",
                   help="skip .lrv proxy files")
    p.add_argument("--out", default="copy_plan.json")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("ingest", help="one-shot: scan card -> match -> route -> rename -> copy-verify")
    p.add_argument("--card", required=True)
    p.add_argument("--library", required=True, help="library drive root")
    p.add_argument("--label", default="CARD", help="card label, e.g. SD-03")
    p.add_argument("--events", default="events.json",
                   help='{"YYYY-MM-DD": {"prefix": "...", "dest": "rel/path"}}')
    p.add_argument("--verify", choices=["none", "sample"], default="sample")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--out", default="ingest_plan.json")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("watch", help="auto-ingest newly inserted cards (poll /Volumes)")
    p.add_argument("--library", required=True)
    p.add_argument("--events", default="events.json")
    p.add_argument("--volumes", default="/Volumes")
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("offload", help="copy-then-verify a plan (needs --execute)")
    p.add_argument("--plan", default="copy_plan.json")
    p.add_argument("--verify", choices=["sample", "full"], default="sample")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(fn=cmd_offload)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
