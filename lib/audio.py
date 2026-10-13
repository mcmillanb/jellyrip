"""Audio CD ripper (spec 7.2): abcde -> FLAC, MusicBrainz tags, Music/<Artist>/<Album>/."""
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import discrip
import library

log = logging.getLogger("audio")


class RipError(Exception):
    pass


def musicbrainz_discid(dev):
    """MusicBrainz disc id (audio session only), or None."""
    r = subprocess.run(["abcde-musicbrainz-tool", "--command", "id", "--device", dev],
                       capture_output=True, text=True, timeout=60)
    parts = r.stdout.split()
    return parts[0] if r.returncode == 0 and parts else None


def progress_from_status(workdir, tracks):
    """(percent, detail) from abcde's status file (readtrack-NN / encodetrack-NN lines)."""
    status = next(workdir.glob("abcde.*/status"), None)
    if not status:
        return 0.0, "Looking up the CD on MusicBrainz"
    lines = status.read_text().splitlines()
    read = sum(1 for x in lines if re.fullmatch(r"readtrack-\d+", x))
    encoded = sum(1 for x in lines if re.fullmatch(r"encodetrack-\d+", x))
    percent = round(100 * (read + encoded) / (2 * tracks), 1)
    return percent, f"Read {read} of {tracks} tracks, encoded {encoded}"


def run_abcde(cfg, dev, workdir, tracks):
    """Run abcde for audio tracks 1..tracks; output under workdir. Returns abcde's log."""
    ac = cfg["audio"]
    logfile = workdir / "abcde.log"
    env = {**os.environ, "DISCRIP_WORKDIR": str(workdir)}
    cmd = ["abcde", "-N", "-c", ac["abcde_conf"], "-d", dev, f"1-{tracks}"]
    log.info("run: %s", " ".join(cmd))
    t0 = time.monotonic()
    with open(logfile, "w") as out:
        # cwd=workdir: abcde leaves stray files in its working directory on errors.
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=workdir)
        try:
            while proc.poll() is None:
                if time.monotonic() - t0 > ac["rip_timeout"]:
                    raise RipError(f"abcde timed out after {ac['rip_timeout']} s")
                percent, detail = progress_from_status(workdir, tracks)
                discrip.update_job(cfg, stage="ripping", percent=percent, detail=detail)
                time.sleep(3)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    text = logfile.read_text(errors="replace")
    logfile.unlink()
    for line in text.splitlines():
        if re.search(r"error|warn|fail|skip|not found|unknown", line, re.I):
            log.warning("  abcde: %s", line.rstrip())
        elif line.strip():
            log.debug("  abcde: %s", line.rstrip())
    log.info("abcde rc=%s in %.0fs", proc.returncode, time.monotonic() - t0)
    if proc.returncode != 0:
        raise RipError(f"abcde failed (rc={proc.returncode})")
    return text


def file_album(cfg, workdir, job_id, fp):
    """Move workdir/<Artist>/<Album>/ into Music/ (or _unknown_music/ without a
    MusicBrainz match). Returns (output dir, title)."""
    media = Path(cfg["paths"]["media"])
    albums = [p for p in workdir.glob("*/*") if p.is_dir() and any(p.glob("*.flac"))]
    if len(albums) != 1:
        raise RipError(f"expected one album folder from abcde, found {len(albums)}")
    album = albums[0]
    artist = album.parent.name
    title = f"{artist} - {album.name}"
    if artist.lower().startswith("unknown artist"):
        dest = media / "_unknown_music" / job_id
        log.info("no MusicBrainz match: parking in %s for manual tagging", dest)
        os.rename(album, dest)
        return dest, None

    base = media / "Music" / artist / album.name
    base.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 10):
        dest = base if n == 1 else base.with_name(f"{album.name} ({n})")
        if dest.exists() and fp and all(library.owner_of(cfg, f) == fp
                                        for f in dest.glob("*.flac")):
            shutil.rmtree(dest)  # this same CD ripped again: replace its own album
            log.info("replacing %s (same CD, re-ripped)", dest)
        try:
            os.rename(album, dest)  # fails if dest exists and is not empty
        except OSError:
            continue  # another disc's album of the same name (e.g. disc 2 of a set)
        return dest, title
    raise RipError(f"{base} and 8 more numbered folders already exist")


def rip_cd(cfg, dev, job_id, audio_tracks, force=False):
    """Rip an audio CD. Returns (result, error, output, eject)."""
    media = Path(cfg["paths"]["media"])
    discid = musicbrainz_discid(dev)
    fp = f"mbdiscid:{discid}" if discid else None
    log.info("MusicBrainz disc id %s, %d audio tracks", discid, audio_tracks)
    if fp and not force:
        done = library.already_ripped(cfg, fp)
        if done:
            log.info("already ripped (job %s): %s", done["job_id"], done["outputs"][0])
            return "skipped", "already in the library", str(Path(done["outputs"][0]).parent), True
    if err := discrip.check_free_space(cfg, need_bytes=audio_tracks * 60_000_000):
        return "error", err, None, True

    workdir = media / "_work" / job_id
    workdir.mkdir(parents=True)
    try:
        run_abcde(cfg, dev, workdir, audio_tracks)
        discrip.update_job(cfg, stage="moving", percent=None, detail=None)
        dest, title = file_album(cfg, workdir, job_id, fp)
        if title:
            discrip.update_job(cfg, title=title)
            library.record_rip(cfg, fp, discid, "audio", sorted(dest.glob("*.flac")), job_id)
            library.jellyfin_refresh(cfg)
            log.info("moved to %s", dest)
            return "ok", None, str(dest), True
        return "ok", "no MusicBrainz match: tag it by hand", str(dest), True
    finally:
        if workdir.exists():
            assert workdir.parent == media / "_work"
            shutil.rmtree(workdir)
