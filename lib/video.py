"""Video DVD ripper (spec 7.1): scan, classify, rip, name, move."""
import contextlib
import logging
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import discrip
import makemkv
import naming

log = logging.getLogger("video")
BIN = Path(__file__).resolve().parent.parent / "bin"


class RipError(Exception):
    pass


def classify(titles, vc):
    """Return (kind, chosen titles). kind: film, tv, ambiguous or none (spec 7.1.1)."""
    dur = lambda t: t["duration_s"]  # noqa: E731
    cands = [t for t in titles if dur(t) >= vc["minlength"]]
    if not cands:
        return "none", []
    if len(cands) > vc["max_candidates"]:
        return "ambiguous", []
    longest = max(cands, key=dur)

    band = [t for t in cands if vc["episode_min"] <= dur(t) <= vc["episode_max"]]
    if len(band) >= vc["season_min_titles"]:
        med = statistics.median(map(dur, band))
        cluster = [t for t in band if abs(dur(t) - med) <= vc["episode_tolerance"] * med]
        if len(cluster) >= vc["season_min_titles"]:
            if longest in cluster or dur(longest) < vc["film_min"]:
                return "tv", cluster
            # A long title next to a cluster: a "play all" if it is the cluster's
            # sum, otherwise a film with similar-length extras. Ask.
            if abs(dur(longest) - sum(map(dur, cluster))) <= 0.05 * dur(longest):
                return "tv", cluster
            return "ambiguous", []

    if dur(longest) >= vc["film_min"]:
        twins = [t for t in cands
                 if abs(dur(t) - dur(longest)) <= vc["duplicate_tolerance"] * dur(longest)]
        if len(twins) > 1:
            return "ambiguous", []  # same-length decoys or alternate versions
        return "film", [longest]
    return "ambiguous", []


class Progress:
    """Turns PRGV/PRGT/PRGC lines into throttled job state updates."""

    def __init__(self, cfg, stage):
        self.cfg, self.stage = cfg, stage
        self.task = self.subtask = None
        self.last = 0.0

    def __call__(self, kind, f):
        if kind == "PRGT":
            self.task = f[2]
        elif kind == "PRGC":
            self.subtask = f[2]
        elif kind == "PRGV" and time.monotonic() - self.last >= 2:
            self.last = time.monotonic()
            total, maximum = int(f[1]), int(f[2]) or 1
            # A rip starts with MakeMKV re-scanning the disc; only the save itself
            # gets a percentage, so the bar doesn't run to 90% and fall back to 0.
            saving = self.stage != "ripping" or "saving" in (self.task or "").lower()
            discrip.update_job(self.cfg, stage=self.stage,
                               percent=round(100 * total / maximum, 1) if saving else None,
                               detail=self.subtask or self.task)


def makemkv_run(dev, args, on_line, timeout):
    """dvd-reset-auth, then makemkvcon. Stuck CSS AGIDs cause 'Failed to open disc'."""
    subprocess.run([BIN / "dvd-reset-auth", dev], capture_output=True, timeout=30)
    return makemkv.run(args, on_line, timeout)


def scan(cfg, dev):
    vc = cfg["video"]
    for attempt in (1, 2):
        info = makemkv.Info()
        progress = Progress(cfg, "scanning")

        def on_line(kind, f):
            info.feed(kind, f)
            progress(kind, f)

        rc = makemkv_run(dev, [f"--minlength={vc['minlength']}", "info", f"dev:{dev}"],
                         on_line, vc["scan_timeout"])
        if rc == 0 and info.titles:
            return info
        log.warning("scan attempt %d failed (rc=%s, %d titles)", attempt, rc, len(info.titles))
    raise RipError("MakeMKV could not read the disc")


def rip_title(cfg, dev, title, workdir):
    vc = cfg["video"]
    for attempt in (1, 2):
        info = makemkv.Info()
        progress = Progress(cfg, "ripping")

        def on_line(kind, f):
            info.feed(kind, f)
            progress(kind, f)

        rc = makemkv_run(dev, [f"--minlength={vc['minlength']}", "mkv", f"dev:{dev}",
                               title["index"], workdir], on_line, vc["rip_timeout"])
        files = sorted(workdir.glob("*.mkv"))
        if rc == 0 and len(files) == 1:
            return files[0]
        # Only an early failure is worth a retry; a read error mid-rip will recur.
        if attempt == 1 and makemkv.failed_to_open(info):
            log.warning("MakeMKV failed to open the disc; retrying once")
            for f in files:
                f.unlink()
            continue
        raise RipError(f"MakeMKV rip failed (rc={rc}, {len(files)} files)")


def place_film(cfg, src, name):
    """Move src to Movies/<name>/<name>.mkv without overwriting. Returns the path or None."""
    folder = Path(cfg["paths"]["media"]) / "Movies" / name
    folder.mkdir(parents=True, exist_ok=True)
    # Step 7 replaces this with fingerprint-aware duplicate handling (spec 8.4).
    for candidate in (f"{name}.mkv", f"{name} - DVD.mkv"):
        dest = folder / candidate
        try:
            os.link(src, dest)  # fails if dest exists: never overwrite
        except FileExistsError:
            continue
        src.unlink()
        return dest
    with contextlib.suppress(OSError):
        folder.rmdir()
    return None


def rip_dvd(cfg, dev, job_id, label):
    """Rip a video DVD. Returns (result, error, output, eject)."""
    vc = cfg["video"]
    media = Path(cfg["paths"]["media"])
    workdir = media / "_work" / job_id

    discrip.update_job(cfg, stage="scanning", detail="Scanning disc")
    info = scan(cfg, dev)
    log.info("MakeMKV: %s", info.version)
    titles = info.title_list()
    for t in titles:
        log.info("title %d: %d:%02d:%02d, %.2f GB, %d chapters", t["index"],
                 t["duration_s"] // 3600, t["duration_s"] // 60 % 60, t["duration_s"] % 60,
                 t["size"] / 1e9, t["chapters"])
    kind, chosen = classify(titles, vc)
    disc_name = info.name or label
    log.info("classified as %s: titles %s; disc name %r", kind, [t["index"] for t in chosen],
             disc_name)
    discrip.update_job(cfg, kind=kind, title=disc_name)

    if kind == "none":
        return "error", f"no title longer than {vc['minlength']} s", None, True
    if kind != "film":
        # TV plans and the title picker arrive in build step 6; leave the disc in.
        return "skipped", f"{kind} discs not handled yet", None, False

    title = chosen[0]
    if err := discrip.check_free_space(cfg, need_bytes=title["size"]):
        return "error", err, None, True

    workdir.mkdir(parents=True)
    try:
        mkv = rip_title(cfg, dev, title, workdir)
        log.info("ripped %s (%.2f GB)", mkv.name, mkv.stat().st_size / 1e9)

        discrip.update_job(cfg, stage="moving", percent=None, detail=None)
        name = naming.sanitise(naming.tidy_label(disc_name, vc["label_noise"]))
        if naming.is_junk(disc_name, name, vc["junk_labels"]):
            log.info("label %r is junk; parking in _inbox for a name", disc_name)
            dest = None
        else:
            dest = place_film(cfg, mkv, name)
            if dest is None:
                log.warning("%s already exists twice; parking in _inbox", name)
        discrip.update_job(cfg, title=name or disc_name)
        if dest:
            workdir.rmdir()
            log.info("moved to %s", dest)
            return "ok", None, str(dest), True

        inbox = media / "_inbox" / job_id
        os.rename(workdir, inbox)
        discrip.add_pending(cfg, {
            "id": job_id,
            "prompt": "name_film",
            "disc_label": label,
            "fingerprint": None,  # build step 7
            "suggested": {"name": name},
            "titles": [{"index": title["index"], "duration_s": title["duration_s"],
                        "file": str(inbox / mkv.name)}],
            "inbox_path": str(inbox),
        })
        log.info("parked in %s, waiting for a name", inbox)
        return "ok", None, str(inbox), True
    finally:
        # Anything left in _work/<job> is a failed or cancelled rip.
        if workdir.exists():
            assert workdir.parent == media / "_work"
            shutil.rmtree(workdir)
            log.info("removed partial output %s", workdir)
