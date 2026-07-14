# ingest360 — the self-organizing media library

**For anyone with a drawer full of SD cards.** Built by a wedding DJ who had
7TB of 360 footage scattered across three drives and ten unlabeled cards —
and no idea which cards were safe to erase.

Insert a card. Your footage sorts itself into the right event folder,
renamed consistently, every copy verified, everything logged.
**It never deletes anything. Ever.**

## What it does

```
SD card in  →  scan (read-only)
            →  "do I already have this?" (checksum, not just filename)
            →  match shoot date to your event calendar
            →  route to the right event folder (unknowns go to _INBOX)
            →  rename: EventName_Date_OriginalFileName
            →  copy, then VERIFY the copy, then — and only then — count it
            →  audit log + verdict: is this card safe to reformat?
```

## Quickstart

1. **Requirements:** macOS or Linux, Python 3.9+. No dependencies to install.
2. **Tell it about your events** — copy `events.example.json` to `events.json`
   and map your shoot dates to event names and folders:
   ```json
   {"2026-06-13": {"prefix": "Esther-Tom-Wedding",
                   "dest": "Clients/2026/Esther and Tom/360 Video"}}
   ```
   Dates the file doesn't know go to `_INBOX/` with original names — nothing
   is ever mis-filed, just parked for you to review.
3. **Dry run** (nothing is copied, you just see the plan):
   ```
   python3 ingest360.py ingest --card /Volumes/YOUR_CARD --library /Volumes/YOUR_DRIVE
   ```
4. **Real run** — add `--execute`.
5. **Full auto** — leave it watching; every card you insert gets ingested:
   ```
   python3 ingest360.py watch --library /Volumes/YOUR_DRIVE --execute
   ```

## Other commands

| Command | What it does |
|---|---|
| `inventory <drives...>` | Read-only map of any drive: sizes, video counts, file types |
| `check-backup --cards X --backups Y` | Which clips on a card are already safely stored (checksum-verified) vs. at risk |
| `plan` / `offload` | Build and execute a copy-then-verify migration plan between drives |
| `ingest` | The one-shot: scan → match → route → rename → copy-verify |
| `watch` | Auto-run `ingest` whenever a new card mounts |

## Safety rails (hard-coded, not optional)

- There is **no delete, no format, and no in-place rename code path** in this
  tool. Sources are opened read-only.
- Every copy is verified with a sampled SHA-256 (first 4MB + last 4MB + size —
  fast enough for 20GB clips, strong enough to catch same-name/same-size
  files with different content, which happens more than you'd think).
- Dry-run is the default everywhere. `--execute` is always opt-in.
- Every run writes a manifest so you can see — and undo — exactly what happened.
- A card is only called "safe to reformat" when every clip has a verified
  copy elsewhere. The tool tells you; *you* do the reformatting.

## Hard-won edge cases this handles

- **Same filename, same size, different video** → caught by checksum.
- **Cameras with wrong clocks** (a GoPro convinced it was 2016) → shoot dates
  come from filenames and your calendar, never blindly from file dates.
- **Personal and client footage on one card** → unknown dates park in
  `_INBOX/`, they never pollute your client library.
- **Renamed library copies** → the original camera filename is preserved in
  the tail of every rename, so re-inserting an old card still matches.

## Roadmap

- Song identification: extract audio → fingerprint → timestamped tracklist
  per set (prototype in `phase2_tracklist.py`)
- Booking-calendar API integration (auto event names, no JSON editing)
- More sources: iPhone dumps, Meta glasses exports, drone cards
- Scheduled NAS mirror + notification to your editor when footage lands

---
Built with [Claude](https://claude.com) Cowork as an AI-course capstone —
the workflow was executed manually first (on real client footage, carefully),
then everything we learned got baked into this tool.
