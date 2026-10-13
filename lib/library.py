"""Moving finished rips into the library, and answering pending prompts (spec 8.4).

Used by the ripper (unattended moves), bin/discrip-pending and later the web app.
Files are moved with link + unlink, so an existing file is never overwritten.
"""
import contextlib
import json
import logging
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

import discrip
import naming

log = logging.getLogger("library")


class PlanError(Exception):
    pass


def _move(src, dest):
    """Move src to dest on the same filesystem; FileExistsError if dest exists."""
    os.link(src, dest)
    os.unlink(src)


def _remove_inbox(cfg, inbox):
    """Delete an _inbox/<job> directory, refusing anything outside _inbox."""
    inbox = Path(inbox)
    if inbox.parent != Path(cfg["paths"]["media"]) / "_inbox":
        raise PlanError(f"refusing to delete {inbox}: not in _inbox")
    if inbox.exists():
        shutil.rmtree(inbox)


def jellyfin_refresh(cfg):
    """Ask Jellyfin to scan its libraries. Best effort: a failure is only logged."""
    jf = cfg["jellyfin"]
    if not jf["api_key"]:
        return
    req = urllib.request.Request(jf["url"].rstrip("/") + "/Library/Refresh", method="POST",
                                 headers={"Authorization": f'MediaBrowser Token="{jf["api_key"]}"'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("Jellyfin library refresh requested (HTTP %s)", r.status)
    except OSError as e:
        log.warning("Jellyfin library refresh failed: %s", e)


# ---------------------------------------------------------------- films


def place_film(cfg, src, name):
    """Move src to Movies/<name>/<name>.mkv without overwriting. Returns the path or None."""
    folder = Path(cfg["paths"]["media"]) / "Movies" / name
    folder.mkdir(parents=True, exist_ok=True)
    # Step 7 replaces this with fingerprint-aware duplicate handling (spec 8.4).
    for candidate in (f"{name}.mkv", f"{name} - DVD.mkv"):
        try:
            _move(src, folder / candidate)
        except FileExistsError:
            continue
        return folder / candidate
    with contextlib.suppress(OSError):
        folder.rmdir()
    return None


def answer_name_film(cfg, pending_id, name):
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "name_film":
        raise PlanError(f"no name_film prompt {pending_id}")
    name = naming.sanitise(name)
    if not name:
        raise PlanError("empty name")
    dest = place_film(cfg, Path(item["titles"][0]["file"]), name)
    if dest is None:
        raise PlanError(f"Movies/{name} already has two copies")
    _remove_inbox(cfg, item["inbox_path"])
    discrip.resolve_pending(cfg, pending_id, str(dest))
    log.info("%s: filed as %s", pending_id, dest)
    jellyfin_refresh(cfg)
    return dest


# ---------------------------------------------------------------- TV


def tvmaze_names(show, season, timeout=10):
    """{episode number: name} for show/season from TVmaze; {} if offline or no match."""
    url = ("https://api.tvmaze.com/singlesearch/shows?embed=episodes&q="
           + urllib.parse.quote(show))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except (OSError, ValueError) as e:
        log.info("TVmaze lookup for %r failed: %s", show, e)
        return {}
    log.info("TVmaze: %r matched %r", show, data.get("name"))
    return {ep["number"]: ep["name"] for ep in data.get("_embedded", {}).get("episodes", [])
            if ep.get("season") == season and ep.get("number") and ep.get("name")}


def episode_paths(cfg, show, season, first_episode, count, year=None, names=None):
    """Library paths for count episodes starting at first_episode."""
    show = naming.sanitise(show)
    folder = f"{show} ({year})" if year else show
    base = Path(cfg["paths"]["media"]) / "TV" / folder / f"Season {season:02d}"
    paths = []
    for n in range(first_episode, first_episode + count):
        title = naming.sanitise((names or {}).get(n, ""))
        stem = f"{show} - S{season:02d}E{n:02d}" + (f" - {title}" if title else "")
        paths.append(base / f"{stem}.mkv")
    return paths


def proposed_plan(cfg, show, season, first_episode, count, year=None):
    names = tvmaze_names(show, season) if cfg["video"]["tv_lookup"] else {}
    return episode_paths(cfg, show, season, first_episode, count, year, names)


def continuation_key(show, season):
    return f"{show.lower()}|{season}"


def continuation(cfg, show, season):
    path = Path(cfg["paths"]["state_dir"]) / "tv_continuation.json"
    try:
        return json.loads(path.read_text()).get(continuation_key(show, season))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def apply_tv_plan(cfg, pending_id, show, season, first_episode, year=None, label=None,
                  disc=None):
    """Move a parked TV rip into TV/<Show>/Season NN/ and remember where the season
    is up to. Checks every destination first, so a clash moves nothing."""
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "tv_plan":
        raise PlanError(f"no tv_plan prompt {pending_id}")
    files = [Path(t["file"]) for t in item["titles"]]
    dests = proposed_plan(cfg, show, season, first_episode, len(files), year)
    clashes = [str(d) for d in dests if d.exists()]
    if clashes:
        raise PlanError("already in the library: " + ", ".join(clashes))
    dests[0].parent.mkdir(parents=True, exist_ok=True)
    for src, dest in zip(files, dests):
        _move(src, dest)
        log.info("%s -> %s", src.name, dest)
    _remove_inbox(cfg, item["inbox_path"])
    discrip.resolve_pending(cfg, pending_id, str(dests[0].parent))

    with discrip.json_update(Path(cfg["paths"]["state_dir"]) / "tv_continuation.json",
                             dict) as cont:
        cont[continuation_key(show, season)] = {
            "show": show, "season": season, "year": year,
            "next_episode": first_episode + len(files),
            "last_label": label or item.get("disc_label"),
            "last_disc": disc if disc is not None else item.get("suggested", {}).get("disc"),
            "updated_at": discrip.now_iso(),
        }
    jellyfin_refresh(cfg)
    return dests


# ---------------------------------------------------------------- any prompt


def discard(cfg, pending_id):
    """Delete a parked rip and its prompt."""
    item = discrip.get_pending(cfg, pending_id)
    if not item:
        raise PlanError(f"no pending item {pending_id}")
    if item.get("inbox_path"):
        _remove_inbox(cfg, item["inbox_path"])
    discrip.resolve_pending(cfg, pending_id, None)
    log.info("%s: discarded", pending_id)
