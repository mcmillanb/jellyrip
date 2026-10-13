# discrip: Pi disc ripping appliance — SPEC

Status: draft v2 (adds automatic naming and TV box sets). Written to be handed to Claude Code running on the target Pi.
Name `discrip` is a placeholder; rename freely.

## 1. Goal

Insert a disc into a USB drive, walk away, and later find it in Jellyfin with sensible names.

- Film DVD (and Blu-ray if the drive supports it): lossless rip to MKV with MakeMKV, named automatically from the disc.
- TV season box sets: rip every episode, numbered in disc order and named for Jellyfin.
- Audio CD: rip to FLAC with abcde, tagged from MusicBrainz.
- A mobile-friendly web page shows progress, asks the occasional question, and offers basic controls.
- Jellyfin serves the library from the same box. Samba exposes the files.

## 2. Non-goals (v1)

- No HandBrake or other re-encoding (may be added later as a separate stage).
- No CYD or other hardware display (the status JSON is designed so one could be added later).
- No RAID, no multi-drive support, no remote access from outside the LAN.
- No film metadata lookup: films are named from the disc and Jellyfin does the matching. The only online lookup is optional TV episode names (TVmaze, see 8.4).
- No splitting of a whole season welded into one title (flag it, rip it as one file).

## 3. Target hardware and OS

- Raspberry Pi 4 Model B, 4 GB, official 3 A PSU.
- Raspberry Pi OS Lite (64-bit, Debian 13 trixie), headless. **Boots from the SD card**; the Kingston XS1000 (USB SSD, ~1 TB) is the data disk only. Decided 2026-10-13 after four crashes where a USB dropout took the root filesystem (on the XS1000) with it: with the OS on SD, a USB glitch fails a rip, not the system. EEPROM `BOOT_ORDER=0xf41` (SD first, then USB).
- USB DVD drive: Pioneer DVRTD11RS in an enclosure with an Initio INIC-1618L bridge (`13fd:0840`, USB 2 only), region 2. Device appears as `/dev/sr0`. **Its auxiliary micro-USB power lead must be on a separate USB charger, and must only be plugged or unplugged while the Pi is off** (see `docs/hardware-notes.md`).
- Wired Ethernet. Hostname `jellyfin`, user `jellyfin`, reachable as `jellyfin.local`.
- Case: custom 3D-printed (PETG/ASA). Thermal and power health must be visible in the UI (see 9).

Assumptions to verify on first boot, not to trust blindly:

- Verified 2026-10-13: with the DVD drive bus-powered from the Pi, its spin-up tripped the Pi's shared USB over-current limiter (all ports) while `get_throttled` stayed `0x0`. With the aux lead on a charger, spin-ups, a full rip and a disc swap were clean. If over-current events return, use a powered hub with backfeed protection.
- The XS1000 only links at USB 2 (480M), even on a Mac, so it is a drive or cable fault. About 40 MB/s is plenty for this job.

## 4. Storage layout

OS on the SD card; everything that writes a lot lives on the XS1000, formatted as one ext4 filesystem mounted at `/srv` (`setup/00-data-disk.sh`, fstab by UUID, `nofail`):

- `/srv/media`: the library and rip work areas (below). `_work` and the libraries share this filesystem, so moves are atomic renames.
- `/srv/discrip`: bind-mounted at `/var/lib/discrip`.
- `/srv/docker`: Docker `data-root`.
- `/srv/jellyfin`: Jellyfin config and cache (step 9).

Every discrip service, Docker and Jellyfin has `RequiresMountsFor=/srv`, and the rippers refuse to start unless `/srv` is a mountpoint, so a missing SSD can never fill the SD card.

```
/srv/media/
  _work/             # in-progress rips and temp files (not in Jellyfin)
  _inbox/            # finished rips waiting for a name or confirmation (not in Jellyfin)
  Movies/            # Jellyfin Movies library: Name/Name.mkv  (Name (Year)/ only if a year is known)
  TV/                # Jellyfin Shows library: Show/Season 01/Show - S01E01 - Title.mkv
  Music/             # Jellyfin Music library: Artist/Album/NN - Title.flac
  _unknown_music/    # CD rips with no MusicBrainz match (manual tagging)
/opt/discrip/        # code, scripts, config
/var/lib/discrip/    # state.json, known_discs.json, library.json, logs
```

- Refuse to start a rip if free space < configurable threshold (default 25 GB). Show the reason in the UI.
- Nothing reaches `Movies/` or `TV/` until the rip has finished and its name is resolved. Files are written to `_work/` and moved with an atomic rename (same filesystem).

## 5. Components

1. **watcher service**: polls the drive and triggers a rip on disc insert (section 6).
2. **dispatcher**: classifies the disc type (audio / video) and calls the right ripper.
3. **ripper-video**: MakeMKV wrapper, disc classification (film vs season), naming.
4. **ripper-audio**: abcde wrapper.
5. **state file + lock**: single source of truth for status.
6. **web app**: Flask or FastAPI, status page, prompts and controls.
7. **Jellyfin**: Docker container.
8. **Samba**: network access to `/srv/media`.

## 6. Disc detection and triggering

Decision (2026-10-13): detection uses a **polling watcher service**, not a udev rule. On this drive (Pioneer DVRTD11RS behind an Initio INIC-1618L USB bridge), udev media properties (`ID_CDROM_MEDIA`, `ID_FS_LABEL`) were missing twice after a disc insert until `udevadm trigger --action=change /dev/sr0` was run by hand. The drive has no asynchronous media notification, so the kernel's own uevents come from it polling every 2 s (`events_dfl_poll_msecs=2000`) anyway; asking the drive directly is no less reliable and does not depend on udev's cached properties.

### 6.1 Watcher (`discrip-watcher.service`)

- Long-running Python service (`/opt/discrip/bin/watcher`), `Type=notify`, `Restart=always`, `WatchdogSec=30`, running as the unprivileged user (`cdrom` group). The poll runs in a worker thread, so a poll stuck in the kernel is reported as `unresponsive` rather than freezing the loop (a process stuck in an uninterruptible ioctl couldn't be restarted anyway); the main loop sends `WATCHDOG=1` every iteration, so a hung loop gets restarted. A polkit rule (`systemd/50-discrip.rules`) lets that user start/stop `discrip-rip.service`.
- Every 2 s: open the drive by a stable path (`/dev/disk/by-path/...` or by-id, falling back to `/dev/sr0`) with `O_RDONLY|O_NONBLOCK`, call `ioctl(CDROM_DRIVE_STATUS)`, close it. Reopen on every poll so a USB re-enumeration (renumbered or recreated `/dev/sr0`) is handled without special cases. The status query does not spin the disc.
- Drive states: `1` no disc, `2` tray open, `3` not ready (spinning up, about 8 s after the tray closes), `4` disc ready. A swap looks like 4 → 2 → 3 → 4.
- **Trigger:** a transition into `4` from any other state starts `discrip-rip.service` (oneshot, runs `/opt/discrip/bin/dispatch`). The watcher never rips itself, so a rip can't block polling.
- **Startup:** if the drive is already at `4` when the watcher starts (disc inserted while the Pi was off, or after a service restart), trigger anyway; `dispatch` skips discs already ripped (fingerprint check, section 7/known discs).
- **Missing or unresponsive drive:** device absent, open fails, or the ioctl errors or times out for more than about 30 s → state `drive_missing` / `drive_unresponsive`, shown in the UI as "Drive not responding: power-cycle it". Keep polling; recover automatically when it answers again. (A wedged USB bridge needs the drive's own power cycled; software can't clear it.)
- **Busy:** a new disc while a rip runs is visible immediately; queue or reject per section 11.
- Writes drive state and the time of the last successful poll into the state file (section 4/5), so the web page shows tray open / reading / ready and a stale timestamp exposes a dead watcher.
- USB autosuspend must stay off for the drive (`power/control` = `on`, which is the case now); check this in `01-base.sh`, or add a udev rule only to pin it.

### 6.2 Dispatch

- `dispatch` reads the disc itself and does not trust udev properties: before anything else run `bin/dvd-reset-auth` (stuck CSS AGIDs make MakeMKV fail with "Failed to open disc"), then classify with `cdrom_id` / `blkid -p` (or `udevadm trigger --action=change` + `udevadm settle` first if udev properties are wanted). Candidate fields: `ID_CDROM_MEDIA_TRACK_COUNT_AUDIO` (audio CD), `ID_CDROM_MEDIA_DVD`, `ID_CDROM_MEDIA_BD`, `ID_FS_LABEL`.
- Handle slow spin-up: retry reading media state for a short period before declaring "no readable disc".
- Unknown or unreadable disc: record an error in state and eject (configurable).
- Allow long scans: discs with corrupt-IFO protection (e.g. Mirrormask) take 9-14 min to scan before ripping starts.

## 7. Rip pipelines

### 7.1 Video (MakeMKV)

- MakeMKV on arm64: `makemkv-bin` ships a prebuilt arm64 `makemkvcon`; `makemkv-oss` is built from source. Follow the official Linux build instructions from the MakeMKV forum (headless: no GUI needed). Document the exact install steps in `/opt/discrip/INSTALL.md` as they are done.
- Licensing: per makemkv.com/buy, converting DVD (and AVCHD) discs is freeware with no time limit; only Blu-ray is shareware. This build is DVD-only, so no registration key or beta key handling is needed. If Blu-ray is ever added, a key (purchased, or the free monthly beta key) would be required, so keep the config path for one but leave it unused and out of the UI.
- Detect a Blu-ray in the drive and fail with a clear message ("Blu-ray not supported") instead of attempting a rip.
- Check `makemkvcon` still starts after upgrades. MakeMKV can refuse to run old versions, so if it reports the version is too old, rebuild from the current release. Log the installed version on each job.
- Use robot mode (`-r`) throughout. Progress comes from `PRGV`, `PRGT`, `PRGC` and `MSG` lines.

Implementation notes (build step 5, `lib/video.py`, `lib/makemkv.py`):

- Thresholds, label noise and junk lists are in `[video]` in `discrip.toml`. Defaults: `minlength` 600 s, film >= 60 min, episodes 18-70 min within 20%.
- MakeMKV re-scans the disc for every `mkv` run, so a rip costs two scans. Home Alone (45 titles, structure protection) scans in 8.5 min; budget ~17 min of scanning on such discs. `info` and `mkv` use the same `--minlength`, so title indexes match.
- `dvd-reset-auth` runs before every `makemkvcon`. A failed scan is retried once; a rip is retried once only if MakeMKV failed to open the disc (a read error mid-rip would recur).
- Progress: `PRGV` total/max becomes `job.percent`, `PRGC`/`PRGT` become `job.detail`; state writes are throttled to one per 2 s.
- On error or cancel, `_work/<job-id>` is removed. Files move into the library with `link` + `unlink`, so an existing file is never overwritten.

Flow for each video disc:

1. **Info scan**: `makemkvcon -r info disc:0`. Collect the disc name and, per title: index, duration, size, chapter count (`CINFO` / `TINFO` lines).
2. **Classify** the disc (7.1.1) and choose which titles to rip.
3. **Rip** the chosen titles to `_work/<job-id>/`, with `--minlength` as a floor.
4. **Resolve naming** (8.4). If the name is resolved, move into the library. If not, move to `_inbox/<job-id>/` and create a pending entry.
5. **Eject** (configurable, default on). The drive is then free for the next disc even if a name is still pending.

#### 7.1.1 Classification

All thresholds live in config. When unsure, ask the user rather than guess.

- **Film**: the longest title is >= 60 min and there is no cluster of similar-length titles. Rip that title only. Extras and trailers are skipped (setting to include them).
- **Season disc**: 3 or more titles with durations in a band of about 18-70 min and within about 20% of the group median. Rip all of them, in disc title order.
- **Long title next to a season cluster**: if the durations of the cluster, or of the cluster minus up to 2 titles, add up to it (within 2%), it is a "play all": those titles are the episodes and the rest are episode-length extras (Father Ted S1: 7 titles of 22-25 min, a 2:25:17 play-all = titles 1-6 exactly). Otherwise it could be a film with similar-length extras, so ask (ambiguous).
- **Twins**: more than one title within 2% of the longest (decoys, alternate cuts, language versions): ask.
- **Ambiguous**: anything else, or more than 12 candidate titles (some Blu-rays carry dozens of near-identical decoy playlists). Do not rip automatically. Create a pending `choose_titles` prompt listing the titles so the user can pick, and leave the disc in the drive.
- A whole season welded into one very long title is ripped as one file and flagged in history.

### 7.2 Audio CD (abcde)

- `abcde` with cdparanoia, FLAC output, MusicBrainz lookup. Config in `/opt/discrip/abcde.conf`.
- Output pattern: `Music/<Artist>/<Album>/<NN> - <Title>.flac`.
- No metadata match: rip to `_unknown_music/<timestamp>/` and flag it in the UI.
- Progress: parse abcde/cdparanoia output as best as practical (track N of M is sufficient).
- Eject on completion (configurable).

Implementation (build step 8, `lib/audio.py`, `config/abcde.conf`):

- `abcde -N -c /opt/discrip/abcde.conf -d <dev> 1-<audio tracks>` into `_work/<job-id>`; only the audio tracks are passed, so an enhanced CD's trailing data track is never read. FLAC `--best`, cdparanoia in its default (paranoid) mode, two processes (read the next track while encoding). Names keep spaces; only `/`, `:*?"<>|` and control characters are replaced.
- The MusicBrainz disc id (`abcde-musicbrainz-tool --command id`) is the CD's fingerprint (`mbdiscid:<id>`): an already-ripped CD is skipped and ejected like a DVD. With several matching releases abcde takes the first (they normally share the track list).
- Progress comes from abcde's `status` file (`readtrack-NN`, `encodetrack-NN`).
- The finished `<Artist>/<Album>` folder is renamed into `Music/`. If that album folder exists from another CD (e.g. disc 2 of a set), it becomes `<Album> (2)`; the same CD re-ripped replaces its own folder. No match (`Unknown Artist`): moved to `_unknown_music/<job-id>/` and flagged.
- Measured: KT Tunstall, Drastic Fantastic (11 tracks, 39:24) in 14 min, about 2x read speed in paranoia mode; 266 MB FLAC.

## 8. State and locking

### 8.1 Locking

- One rip at a time: `dispatch` holds `flock` on `/run/discrip/lock` (tmpfs, so a crash or reboot can't leave it held) for the whole job. A disc that becomes ready while a rip runs is **rejected** (decided 2026-10-13): no second rip starts and `message` in the state file says so.
- **Stale jobs:** a `job` in the state file while nobody holds the lock was cut short by a reboot, crash or kill. The watcher (at startup) and `dispatch` (before starting) move it into history as `error` ("interrupted at stage ...").
- `systemctl stop discrip-rip` sends SIGTERM to the whole unit; `dispatch` records the job as `cancelled`.
- Pending naming prompts do not hold the lock.

### 8.2 State file

`/var/lib/discrip/state.json`, written atomically (write to temp file, then rename). Schema v2:

```json
{
  "schema_version": 2,
  "updated_at": "2026-10-13T09:00:00Z",
  "drive": { "present": true, "status": "ready", "media": "dvd", "label": "SOME_DISC", "last_poll": "2026-10-13T09:00:00Z" },
  "message": null,
  "job": {
    "id": "20261013-090000",
    "type": "video",
    "kind": "tv",
    "stage": "ripping",
    "title": "Some Show",
    "percent": 42.5,
    "detail": "Title 2 of 6",
    "started_at": "2026-10-13T08:50:00Z",
    "error": null
  },
  "pending": [
    {
      "id": "20261012-211500",
      "prompt": "tv_plan",
      "disc_label": "SOME_SHOW_S1_D1",
      "fingerprint": "sha1:...",
      "suggested": { "show": "Some Show", "season": 1, "first_episode": 1 },
      "titles": [
        { "index": 0, "duration_s": 2520, "proposed": "Some Show - S01E01" }
      ],
      "inbox_path": "/srv/media/_inbox/20261012-211500"
    }
  ],
  "history": [
    {
      "id": "20261012-180000",
      "type": "audio",
      "title": "Artist - Album",
      "result": "ok",
      "finished_at": "2026-10-12T18:30:00Z",
      "output": "/srv/media/Music/Artist/Album",
      "error": null
    }
  ]
}
```

- `job.stage` values: `idle`, `detecting`, `scanning`, `ripping`, `moving`, `ejecting`, `done`, `error`, `cancelled`.
- `job` is `null` when idle. `history` is newest first and keeps the last N jobs (`[state] history_max`, default 50). `history[].result` values: `ok`, `error`, `cancelled`, `skipped`.
- `drive` is written by the watcher: `status` is `no_disc`, `tray_open`, `not_ready`, `ready`, `missing` or `unresponsive`, and `last_poll` is refreshed at least every 30 s (a stale value means the watcher is dead). `media` (`dvd`, `bluray`, `audio`, `data_cd`, `none`, `unknown`) and `label` are filled in by `dispatch`.
- `message` is `null` or `{"text": ..., "at": ...}`: a notice not tied to a job, such as a rejected disc. Cleared when the next job starts.
- `pending[].prompt` values: `name_film`, `tv_plan`, `choose_titles`. A pending item survives reboots and does not block the next disc.
- System metrics (temperature, throttling, free space) are computed by the web app at request time, not stored in this file.

### 8.3 Logs

- Per-job log at `/var/lib/discrip/logs/<job-id>.log` plus the systemd journal. Rotate or cap total size.

### 8.4 Naming

Goal: unattended when the disc gives a usable name, and a single short question when it doesn't.

**Disc fingerprint.** A stable id for "this specific disc", e.g. a sha1 of `ID_FS_LABEL`, `ID_FS_UUID` and the sorted title durations. Used to remember corrections and to tell two different discs of the same film apart. Corrections are stored in `/var/lib/discrip/known_discs.json`; the output path of every rip is recorded against its fingerprint in `/var/lib/discrip/library.json`.

Implementation (build step 7, `lib/library.py`): `fingerprint` = `sha1:` of label, UUID (from `blkid`) and the sorted durations of the titles MakeMKV lists, so changing `[video] minlength` changes fingerprints. `library.json` records each disc's output files; `known_discs.json` holds the answers given for it (film name, or TV show/season/first episode). After the scan, a disc whose files are all still in the library is **skipped and ejected** ("already in the library"), as is a disc already waiting in `_inbox` for an answer. To rip it again anyway, create `/run/discrip/rerip` before starting the rip (the web app's "rip again" will do this); the re-rip replaces only the files that disc wrote. A known disc is filed with its remembered answer and no question. `discrip-pending known` / `forget <fingerprint>` list and drop remembered answers.

**Films**

- Take the disc name (MakeMKV `CINFO` name, falling back to `ID_FS_LABEL`) and tidy it: underscores to spaces, strip trailing disc/format noise (`DISC1`, `D1`, `WS`, `PAL`, `NTSC`, `BLURAY`), title-case. Example: `BLADE_RUNNER_2049` becomes `Blade Runner 2049`.
- Output: `Movies/<Name>/<Name>.mkv`.
- **Never guess a year.** A year appears only if it was in brackets in the label (`Film (1982)`) or the user typed it. `Film.mkv` is acceptable to Jellyfin; `Film (2049).mkv` would be a confident lie.
- **Junk labels**: DVD labels are often junk (`LOGICAL_VOLUME_ID`, `DVD_VIDEO`, `DVDVOLUME`, all digits, a single generic word). Keep a configurable junk list. For a junk label, create a pending `name_film` prompt; the rip still proceeds and parks in `_inbox/` until answered.
- **Remember corrections** by fingerprint. The same disc later needs no question.

**Duplicates**

- If the destination already exists and was written by a different fingerprint, do not overwrite. Save alongside with the source on the end: `Name.mkv` and `Name - DVD.mkv` (or `- Bluray`). Jellyfin shows these as versions of one film.
- Re-ripping the same fingerprint replaces its own file.

**TV season discs** (classification in 7.1.1)

- On the first disc of a season, create a pending `tv_plan` prompt: show name (prefilled from the tidied label), optional year, season number, first episode number, and the proposed list of files in disc order. Nothing is moved into the library until the user confirms.
- **Disc order is kept.** Title order on the disc is the numbering source. Optionally, on Blu-ray, a "play all" playlist, when present, is the better record of episode order. Disc order and broadcast order can differ (the user can shift the first episode number, which renumbers the whole disc).
- **Continuation:** after a filed disc, remember `show`, `season` and `next_episode`. A following disc of the same show and season continues automatically from `next_episode`.
- **Batching (decided 2026-10-13, after Riparr):** discs can be ripped one after another and answered later. Waiting discs of one show+season form a chain in disc order (disc number, then rip order), each numbered provisionally from the one before. One answer confirms the season: the first waiting disc is filed with it (show/season/first-episode overrides apply to the whole chain) and every later disc follows on. A disc that arrives before an earlier disc number waits for it, and files itself once the gap is filled.
- **When to ask (`[video] tv_confirm`, default `when_unsure`):** a season that already has a filed disc continues without asking. Its first disc is filed without asking only if nothing was guessed: the season is on the label, it is disc 1 (or unnumbered), TVmaze matches the show name, the episode count fits TVmaze's season, and TVmaze has no DVD order for that season that differs from broadcast order (otherwise Jellyfin, which matches by SxxEyy in broadcast order, would show the wrong episode details). The reasons are stored in the prompt (`reasons`) and shown. `always` asks for every season; `never` files every disc with the best guess.
- **Output:** `TV/<Show>/Season 01/<Show> - S01E01 - <Episode Title>.mkv`. Year in the folder name (`Show (1990)`) only if known and confirmed.
- **Episode titles (optional, config `tv_lookup = true`):** look up names from TVmaze (free, no account, `https://api.tvmaze.com`), taking names only and never the order. If offline or no match, name files `Show - S01E01.mkv`.
- Double-length episodes, or episodes carried twice (with and without a "next time" trailer), are not handled specially in v1. They show up in the plan so the user can spot them; extra files can be removed over Samba.

**Implementation (build step 6):** `lib/library.py` holds the moves and prompt answers (shared by the ripper, `bin/discrip-pending` and the web app). Season discs are ripped title by title (each `mkv` run re-scans the disc), parked in `_inbox/<job-id>/title_NN.mkv`, and get a `tv_plan` prompt whose `suggested` holds `show`, `season` and `disc` parsed from the label (`FATHER_TED_S1_D2` -> Father Ted, 1, 2; season defaults to 1), `first_episode` and `year` from the continuation, and each title's `proposed` file name. Continuations live in `/var/lib/discrip/tv_continuation.json`, keyed by show and season. A disc continues automatically only if its label carries a disc number different from the last disc of that season; otherwise it asks, with the continuation as the default. Ambiguous discs get a `choose_titles` prompt listing the titles and stay in the drive (answering it arrives with the web app). Until the web app exists, answer prompts with `bin/discrip-pending`.

**Sanitising:** strip `/`, control characters and trailing dots from all names.

**Notifications:** when a job fails or leaves a prompt to answer, `dispatch` pops a message up in every open Jellyfin app (`POST /Sessions/{id}/Message`, `[notify] jellyfin_popup`). Jellyfin keeps no inbox, so only people using it at that moment see it; the web app's queue is the durable place. A phone push (e.g. ntfy) was considered and left out for now.

## 9. Web app

- Python, single service under systemd, listening on port 8080 (configurable).
- Mobile-first single page: large tap targets, readable on an iPhone in Safari, works as an Add to Home Screen web app (viewport and `apple-mobile-web-app-*` meta tags, icon).
- Polls `/api/status` every 2 s (SSE is optional).
- Shows: current stage and progress bar, disc title, drive state, last result, recent history, system health, and a **needs attention** card for each pending prompt with simple forms (film name; TV show, season, first episode with the proposed file list; title picker).
- System health: SoC temperature (`vcgencmd measure_temp`), throttle/undervoltage flags (`vcgencmd get_throttled`, decoded), free space on `/srv/media`. Show a clear warning banner on undervoltage or throttling.

Endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/status` | State JSON plus system health |
| GET | `/api/history` | Job history |
| GET | `/api/log/<job-id>?lines=200` | Tail of a job log |
| POST | `/api/eject` | Eject the disc (refused mid-rip unless forced) |
| POST | `/api/cancel` | Cancel the current job (signal process group, clean up) |
| POST | `/api/start` | Start a rip of the inserted disc (for when auto-start is disabled) |
| POST | `/api/pending/<id>/answer` | Answer a prompt (film name, TV plan, or chosen titles); moves the rip into the library |
| POST | `/api/pending/<id>/discard` | Delete a pending rip from `_inbox` |
| GET | `/api/known-discs` | List remembered corrections |
| DELETE | `/api/known-discs/<fingerprint>` | Forget a remembered correction |

Implementation (build step 10): `bin/web` (Flask, `discrip-web.service`, port 8080) and `web/index.html` (one page, no build step). `/api/status` returns the state file (without history) plus `last_job`, `health` (temperature from sysfs, decoded `get_throttled` flags, free space), `rip_unit` and `auto_start`, and allows any origin so the Jellyfin header pill can read it. Extra endpoint: `GET /api/pending/<id>/preview?show=&season=&first_episode=&year=` returns the files a TV answer would produce, so the page shows renumbering before confirming. Answers: `tv_plan` takes `{show, season, first_episode, year}` and confirms the whole season; `name_film` takes `{name}`; `choose_titles` takes `{titles: [...], kind: "film"|"tv"}`, stores the choice, and starts the rip if the disc is in (otherwise it rips at the next insert). `/api/start {"rerip": true}` sets `/run/discrip/rerip`, so the next rip of that disc replaces its files even if the disc is out now. The token is generated by `03-discrip.sh` into `[web] token`; open `http://jellyfin.local:8080/#token=<token>` once per device to save it in the browser.

**Jellyfin header pill:** `web/pill.js`, served by the web app, polls `/api/status` every 5 s and shows a pill in Jellyfin's header (`.headerRight`, floating if absent) only when a rip runs, prompts wait, the drive is missing, power/thermal flags are set, or a rip failed in the last 6 h; tapping it opens the web app. It is loaded into Jellyfin's web client (and the iOS/Android apps that wrap it, not TV apps) by a one-line snippet in the JavaScript Injector plugin (n00bcodr, repository `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/12/manifest.json`, targetAbi 12.0).

Auth and exposure:

- LAN only, never port-forwarded. POST/DELETE endpoints require a shared token (in config, entered once in the UI and stored in the browser). Read-only status may be unauthenticated.
- Remote access, if wanted later, via Tailscale or WireGuard, not by opening ports.

## 10. Jellyfin and Samba

- Jellyfin in Docker (official arm64 image), config and cache in `/srv/jellyfin` (XS1000). Mount `/srv/media/Movies`, `/srv/media/TV` and `/srv/media/Music` read-only. Libraries: Movies, Shows, Music. `_inbox`, `_work` and `_unknown_music` are not libraries.
- Expect direct play only. No hardware transcoding on the Pi 4. Raw DVD MPEG-2 may force transcoding on some clients (notably browsers), so test on the real devices.
- Enable the "Allow media downloads" permission for the user(s) who need browser downloads.
- Trigger a library scan after a successful move (Jellyfin API with an API key from config), or rely on the scheduled/real-time monitor. Decide in 11.
- Samba shares `/srv/media` (read/write for one authenticated user, so renames and manual tagging work from a laptop). No guest write.

## 11. Open decisions (resolve with the user before or during build)

1. Default `--minlength` and the film/season duration thresholds in 7.1.1. Validate against a few real discs and tune. (Defaults set in step 5; still to validate against a real season disc.)
2. If a disc is inserted while busy: queue, or reject with a message? **Decided 2026-10-13:** reject with a message (8.1).
3. Auto-start on insert, or require a tap on `/api/start`? **Decided 2026-10-13:** auto-start, switchable with `[dispatch] auto_start`.
4. Jellyfin refresh: API call after move, or rely on its own scanning? **Decided 2026-10-13:** API call (`/Library/Refresh`) after every move; the folder watcher skips libraries that were empty at the last scan.
5. TVmaze lookup on by default? Defaulted to on in step 6 (`[video] tv_lookup`); confirm with the user.
6. Include DVD extras by default? (Proposed: no.)
7. Hostname, web port, Samba username. (Hostname `jellyfin`; Samba user `jellyfin`, share `media` (setup/09-samba.sh); web port 8080.)
8. Whether to keep a copy of the MakeMKV log with each rip.

## 12. Build order

1. Base OS on SD: SSH, hostname, data disk at `/srv` (`00-data-disk.sh`), `get_throttled` clean under load, Docker (data on `/srv`), Samba, directory layout (`01-base.sh`).
2. MakeMKV arm64 install and a manual rip of one DVD by hand to prove the drive and key work.
3. Watcher service, `dispatch` script and systemd units. Prove detection (including swaps, startup with a disc in, and drive unplug/replug), classification and eject with logging only.
4. State file writer and lock, used by all scripts.
5. Video ripper: info scan, film path (tidy label, junk detection, rip, move), progress parsing.
6. Season-disc classification, TV plan prompt, numbering, continuation, optional TVmaze names.
7. Fingerprints, `known_discs.json`, duplicate handling.
8. Audio ripper wrapper (abcde), output to `Music/`.
9. Jellyfin container and libraries, then Samba.
10. Web app: `/api/status` and page first, then controls, then prompts and pending answers.
11. Thermal test with the 3D-printed case: rip while streaming for 20-30 min and watch temperature and throttle flags.

## 13. Acceptance tests

- Film DVD with a good label: rips unattended to `Movies/<Name>/<Name>.mkv`, ejects, appears in Jellyfin.
- Film DVD with a junk label: rip completes and parks in `_inbox`; a prompt appears on the iPhone; answering moves it into `Movies/` with the chosen name.
- Same disc re-inserted after a correction: no prompt, correct name.
- Different disc of the same film (e.g. DVD then Blu-ray): second file gets a ` - DVD` / ` - Bluray` suffix, no overwrite.
- TV season disc: all episodes ripped in disc order; the plan prompt appears; after confirming, files are named `Show - S01E0N - Title.mkv` under `TV/Show/Season 01/` and show up as episodes in Jellyfin.
- Second disc of the same season: continues numbering with no question.
- Ambiguous disc (many decoy titles): no automatic rip; title picker shown.
- Insert an audio CD: FLAC files with correct tags appear in `Music/` and show up in Jellyfin.
- Cancel mid-rip: partial output removed, state returns to idle, disc can be ejected.
- Unreadable or scratched disc: job ends in `error` with a log, no partial files left, system stays responsive.
- Power-cycle during a rip: on reboot, state is not stuck on `ripping` (stale job detected and marked `error`); pending items in `_inbox` are still listed.
- Full disk threshold: rip is refused with a clear message.
- Undervoltage or throttling: warning banner appears in the UI.

## 14. Constraints and notes for the implementer

- Keep dependencies small: Python stdlib plus Flask/FastAPI, no frontend build step.
- Everything configurable lives in one config file; no hard-coded paths in scripts.
- Scripts must be safe to re-run and must never delete anything outside `_work/` and `_inbox/`.
- Prefer small, readable shell or Python over frameworks; the owner is an experienced engineer who wants to be able to debug it.
- Reference for the naming behaviour: the Riparr project (https://github.com/jackharvest/riparr, `docs/guide/05-library-layout.md`). It is GPL-3.0, so implement the behaviour described here independently rather than copying its code, unless the intent is to make this project GPL too.
