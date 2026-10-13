"""Video DVD ripper (spec 7.1): scan, classify, rip, name, move."""
import itertools
import logging
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import discrip
import library
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
            # A long title next to a cluster: a "play all" if some of the cluster
            # adds up to it (those are the episodes; the rest are extras of
            # episode length), otherwise a film with similar-length extras. Ask.
            episodes = playall_subset(longest, cluster, vc["season_min_titles"])
            if episodes:
                return "tv", episodes
            return "ambiguous", []

    if dur(longest) >= vc["film_min"]:
        twins = [t for t in cands
                 if abs(dur(t) - dur(longest)) <= vc["duplicate_tolerance"] * dur(longest)]
        if len(twins) > 1:
            return "ambiguous", []  # same-length decoys or alternate versions
        return "film", [longest]
    return "ambiguous", []


def playall_subset(playall, cluster, min_titles, tolerance=0.02):
    """The titles of cluster whose durations add up to playall's (dropping at
    most 2 titles), in disc order; None if there is no such subset."""
    target = playall["duration_s"]
    for drop in range(min(3, len(cluster) - min_titles + 1)):
        for dropped in itertools.combinations(cluster, drop):
            subset = [t for t in cluster if t not in dropped]
            if abs(sum(t["duration_s"] for t in subset) - target) <= tolerance * target:
                return subset
    return None


class Progress:
    """Turns PRGV/PRGT/PRGC lines into throttled job state updates.

    part=(i, n): this run is title i of n, so the percentage covers the whole job.
    """

    def __init__(self, cfg, stage, part=(0, 1)):
        self.cfg, self.stage, self.part = cfg, stage, part
        self.task = self.subtask = None
        self.last = 0.0

    def __call__(self, kind, f):
        if kind == "PRGT":
            self.task = f[2]
        elif kind == "PRGC":
            self.subtask = f[2]
        elif kind == "PRGV" and time.monotonic() - self.last >= 2:
            self.last = time.monotonic()
            i, n = self.part
            fraction = int(f[1]) / (int(f[2]) or 1)
            # A rip starts with MakeMKV re-scanning the disc; only the save itself
            # moves the bar, so it doesn't run to 90% and fall back to 0.
            if self.stage == "ripping" and "saving" not in (self.task or "").lower():
                fraction = 0.0
            detail = self.subtask or self.task
            if n > 1:
                detail = f"Title {i + 1} of {n}: {detail}"
            discrip.update_job(self.cfg, stage=self.stage,
                               percent=round(100 * (i + fraction) / n, 1), detail=detail)


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


def rip_title(cfg, dev, title, workdir, part=(0, 1)):
    """Rip one title into workdir as title_NN.mkv and return its path."""
    vc = cfg["video"]
    out = workdir / f"t{title['index']:02d}"
    for attempt in (1, 2):
        out.mkdir()
        info = makemkv.Info()
        progress = Progress(cfg, "ripping", part)

        def on_line(kind, f):
            info.feed(kind, f)
            progress(kind, f)

        rc = makemkv_run(dev, [f"--minlength={vc['minlength']}", "mkv", f"dev:{dev}",
                               title["index"], out], on_line, vc["rip_timeout"])
        files = sorted(out.glob("*.mkv"))
        if rc == 0 and len(files) == 1:
            dest = workdir / f"title_{title['index']:02d}.mkv"
            files[0].rename(dest)
            dest.chmod(0o664)  # makemkvcon creates 0666 regardless of the umask
            out.rmdir()
            log.info("ripped title %d (%.2f GB)", title["index"], dest.stat().st_size / 1e9)
            return dest
        shutil.rmtree(out)
        # Only an early failure is worth a retry; a read error mid-rip will recur.
        if attempt == 1 and makemkv.failed_to_open(info):
            log.warning("MakeMKV failed to open the disc; retrying once")
            continue
        raise RipError(f"MakeMKV rip of title {title['index']} failed (rc={rc})")


def park(cfg, workdir, job_id):
    """Move _work/<job> to _inbox/<job>; returns the inbox path."""
    inbox = Path(cfg["paths"]["media"]) / "_inbox" / job_id
    os.rename(workdir, inbox)
    return inbox


def file_film(cfg, job_id, label, disc_name, title, mkv, workdir):
    vc = cfg["video"]
    name = naming.sanitise(naming.tidy_label(disc_name, vc["label_noise"]))
    dest = None
    if naming.is_junk(disc_name, name, vc["junk_labels"]):
        log.info("label %r is junk; parking in _inbox for a name", disc_name)
    else:
        dest = library.place_film(cfg, mkv, name)
        if dest is None:
            log.warning("%s already exists twice; parking in _inbox", name)
    discrip.update_job(cfg, title=name or disc_name)
    if dest:
        workdir.rmdir()
        log.info("moved to %s", dest)
        library.jellyfin_refresh(cfg)
        return str(dest)

    inbox = park(cfg, workdir, job_id)
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
    return str(inbox)


def file_tv(cfg, job_id, label, disc_name, chosen, mkvs, workdir):
    """Continue a known season automatically, or park with a tv_plan prompt."""
    show, season, disc = naming.parse_tv_label(disc_name, cfg["video"]["label_noise"])
    show = naming.sanitise(show)
    season = season or 1
    discrip.update_job(cfg, title=f"{show} season {season}")
    cont = library.continuation(cfg, show, season) if show else None
    # Spec 8.4: a following disc (same show and season, different disc number)
    # continues the numbering without asking. Anything else asks.
    auto = bool(cont) and disc is not None and disc != cont.get("last_disc")
    first = cont["next_episode"] if cont else 1
    year = cont.get("year") if cont else None

    inbox = park(cfg, workdir, job_id)
    titles = [{"index": t["index"], "duration_s": t["duration_s"],
               "file": str(inbox / m.name)} for t, m in zip(chosen, mkvs)]
    proposed = library.proposed_plan(cfg, show or "Unknown", season, first, len(titles), year)
    for t, p in zip(titles, proposed):
        t["proposed"] = p.stem
    discrip.add_pending(cfg, {
        "id": job_id,
        "prompt": "tv_plan",
        "disc_label": label,
        "fingerprint": None,  # build step 7
        "suggested": {"show": show, "year": year, "season": season, "first_episode": first,
                      "disc": disc, "continues": bool(cont)},
        "titles": titles,
        "inbox_path": str(inbox),
    })
    if auto:
        log.info("continuing %s season %d from episode %d (disc %s after disc %s)",
                 show, season, first, disc, cont["last_disc"])
        try:
            dests = library.apply_tv_plan(cfg, job_id, show, season, first, year, label, disc)
            return str(dests[0].parent)
        except library.PlanError as e:
            log.warning("automatic continuation failed (%s); asking instead", e)
    log.info("parked in %s, waiting for a TV plan (suggested %s S%02d from E%02d)",
             inbox, show, season, first)
    return str(inbox)


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
    if kind == "ambiguous":
        # Spec 7.1.1: don't guess. List the titles for the user; leave the disc in.
        discrip.add_pending(cfg, {
            "id": job_id,
            "prompt": "choose_titles",
            "disc_label": label,
            "fingerprint": None,
            "suggested": {},
            "titles": [{"index": t["index"], "duration_s": t["duration_s"],
                        "size": t["size"], "chapters": t["chapters"]}
                       for t in titles if t["duration_s"] >= vc["minlength"]],
            "inbox_path": None,
        })
        return "skipped", "not sure which titles to rip: choose them", None, False

    if err := discrip.check_free_space(cfg, need_bytes=sum(t["size"] for t in chosen)):
        return "error", err, None, True

    workdir.mkdir(parents=True)
    try:
        mkvs = [rip_title(cfg, dev, t, workdir, (i, len(chosen)))
                for i, t in enumerate(chosen)]
        discrip.update_job(cfg, stage="moving", percent=None, detail=None)
        if kind == "film":
            output = file_film(cfg, job_id, label, disc_name, chosen[0], mkvs[0], workdir)
        else:
            output = file_tv(cfg, job_id, label, disc_name, chosen, mkvs, workdir)
        return "ok", None, output, True
    finally:
        # Anything left in _work/<job> is a failed or cancelled rip.
        if workdir.exists():
            assert workdir.parent == media / "_work"
            shutil.rmtree(workdir)
            log.info("removed partial output %s", workdir)
