"""Moving finished rips into the library, and answering pending prompts (spec 8.4).

Used by the ripper (unattended moves), bin/discrip-pending and later the web app.
Files are moved with link + unlink, so an existing file is never overwritten,
except a file this same disc (fingerprint) wrote before, which a re-rip replaces.
"""
import contextlib
import fcntl
import functools
import hashlib
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


@contextlib.contextmanager
def filing_lock(cfg):
    """Serialises filing into the library (the ripper and the web app both file)."""
    with open(_store(cfg, ".filing.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- fingerprints
#
# library.json:      {fingerprint: {label, kind, outputs, job_id, ripped_at}}
# known_discs.json:  {fingerprint: {label, kind, name | show/season/first_episode/year}}
#                    the answers the user gave for that disc


def fingerprint(label, uuid, durations):
    """Stable id for one specific disc (spec 8.4): label, UDF/ISO UUID and the sorted
    durations of the titles MakeMKV lists (those >= [video] minlength)."""
    key = "|".join([label or "", uuid or "", ",".join(map(str, sorted(durations)))])
    return "sha1:" + hashlib.sha1(key.encode()).hexdigest()


def _store(cfg, name):
    return Path(cfg["paths"]["state_dir"]) / name


def _read(cfg, name):
    try:
        return json.loads(_store(cfg, name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def owner_of(cfg, path):
    """Fingerprint of the disc that wrote path, or None."""
    return next((fp for fp, e in _read(cfg, "library.json").items()
                 if str(path) in e["outputs"]), None)


def already_ripped(cfg, fp):
    """The library.json entry for fp if all its files are still there, else None."""
    entry = _read(cfg, "library.json").get(fp)
    if entry and entry["outputs"] and all(Path(o).exists() for o in entry["outputs"]):
        return entry
    return None


def record_rip(cfg, fp, label, kind, outputs, job_id):
    if not fp:
        return
    with discrip.json_update(_store(cfg, "library.json"), dict) as lib:
        lib[fp] = {"label": label, "kind": kind, "outputs": [str(o) for o in outputs],
                   "job_id": job_id, "ripped_at": discrip.now_iso()}


def known_discs(cfg):
    return _read(cfg, "known_discs.json")


def known(cfg, fp):
    return _read(cfg, "known_discs.json").get(fp) if fp else None


def remember(cfg, fp, label, **answer):
    """Remember the user's answer for this disc, so a re-rip needs no question."""
    if not fp:
        return
    with discrip.json_update(_store(cfg, "known_discs.json"), dict) as kd:
        kd[fp] = {"label": label, **answer, "updated_at": discrip.now_iso()}


def forget(cfg, fp):
    with discrip.json_update(_store(cfg, "known_discs.json"), dict) as kd:
        return kd.pop(fp, None)


def _place(cfg, src, dest, fp):
    """Move src to dest. If dest exists, replace it only if this disc wrote it.
    Returns True if placed."""
    try:
        _move(src, dest)
        return True
    except FileExistsError:
        if fp and owner_of(cfg, dest) == fp:
            os.replace(src, dest)
            log.info("replaced %s (same disc, re-ripped)", dest)
            return True
        return False


# ---------------------------------------------------------------- films


def place_film(cfg, src, name, fp=None):
    """Move src to Movies/<name>/<name>.mkv. A file there from a different disc is
    kept and this one is saved as '<name> - DVD.mkv' (spec 8.4: Jellyfin shows them
    as versions). Returns the path, or None if both names are taken."""
    folder = Path(cfg["paths"]["media"]) / "Movies" / name
    folder.mkdir(parents=True, exist_ok=True)
    for candidate in (f"{name}.mkv", f"{name} - DVD.mkv"):
        if _place(cfg, src, folder / candidate, fp):
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
    fp = item.get("fingerprint")
    dest = place_film(cfg, Path(item["titles"][0]["file"]), name, fp)
    if dest is None:
        raise PlanError(f"Movies/{name} already has two copies")
    _remove_inbox(cfg, item["inbox_path"])
    discrip.resolve_pending(cfg, pending_id, str(dest))
    record_rip(cfg, fp, item["disc_label"], "film", [dest], pending_id)
    remember(cfg, fp, item["disc_label"], kind="film", name=name)
    log.info("%s: filed as %s", pending_id, dest)
    jellyfin_refresh(cfg)
    return dest


# ---------------------------------------------------------------- TV


def _tvmaze_get(path, timeout=10):
    with urllib.request.urlopen("https://api.tvmaze.com" + path, timeout=timeout) as r:
        return json.load(r)


@functools.lru_cache(maxsize=32)
def tvmaze_show(show):
    """TVmaze's best match for show: {name, seasons: {s: {n: title}}, dvd_order_differs:
    {seasons}}, or None if offline or no match. Names only; never used for order."""
    try:
        data = _tvmaze_get("/singlesearch/shows?embed=episodes&q=" + urllib.parse.quote(show))
        seasons = {}
        for ep in data.get("_embedded", {}).get("episodes", []):
            if ep.get("season") and ep.get("number"):
                seasons.setdefault(ep["season"], {})[ep["number"]] = ep.get("name") or ""
        # Seasons whose DVD release order differs from broadcast order (e.g. Firefly).
        differs = set()
        for lst in _tvmaze_get(f"/shows/{data['id']}/alternatelists"):
            if not lst.get("dvd_release"):
                continue
            for alt in _tvmaze_get(f"/alternatelists/{lst['id']}/alternateepisodes?embed=episodes"):
                orig = (alt.get("_embedded", {}).get("episodes") or [{}])[0]
                if (orig.get("season"), orig.get("number")) != (alt["season"], alt["number"]):
                    differs.add(alt["season"])
    except (OSError, ValueError, KeyError) as e:
        log.info("TVmaze lookup for %r failed: %s", show, e)
        return None
    log.info("TVmaze: %r matched %r", show, data.get("name"))
    return {"name": data.get("name") or "", "seasons": seasons, "dvd_order_differs": differs}


def tvmaze_names(show, season):
    """{episode number: name} for show/season; {} if offline or no match."""
    t = tvmaze_show(show)
    return t["seasons"].get(season, {}) if t else {}


def _norm(name):
    return "".join(c for c in name.lower() if c.isalnum())


def unsure_reasons(cfg, show, season, season_on_label, disc, count):
    """Why the first disc of a season can't be filed without asking ([] = sure)."""
    reasons = []
    if not season_on_label:
        reasons.append("the disc label doesn't say which season (assumed 1)")
    if disc not in (None, 1):
        reasons.append(f"this is disc {disc}, but no earlier disc of the season is filed")
    if not cfg["video"]["tv_lookup"]:
        reasons.append("TVmaze lookup is off, so the show can't be checked")
        return reasons
    t = tvmaze_show(show)
    if t is None:
        reasons.append(f"TVmaze doesn't know {show!r}")
        return reasons
    if _norm(t["name"]) != _norm(show):
        reasons.append(f"TVmaze matched a different name: {t['name']!r}")
    eps = t["seasons"].get(season)
    if not eps:
        reasons.append(f"TVmaze has no season {season}")
    elif count > len(eps):
        reasons.append(f"{count} episodes on the disc but TVmaze lists {len(eps)} in season {season}")
    if season in t["dvd_order_differs"]:
        reasons.append("the DVD episode order differs from broadcast order: check the numbering")
    return reasons


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
    return _read(cfg, "tv_continuation.json").get(continuation_key(show, season))


def apply_tv_plan(cfg, pending_id, show, season, first_episode, year=None, label=None,
                  disc=None):
    """Move a parked TV rip into TV/<Show>/Season NN/ and remember where the season
    is up to. Checks every destination first, so a clash moves nothing."""
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "tv_plan":
        raise PlanError(f"no tv_plan prompt {pending_id}")
    fp = item.get("fingerprint")
    files = [Path(t["file"]) for t in item["titles"]]
    dests = proposed_plan(cfg, show, season, first_episode, len(files), year)
    clashes = [str(d) for d in dests if d.exists() and not (fp and owner_of(cfg, d) == fp)]
    if clashes:
        raise PlanError("already in the library: " + ", ".join(clashes))
    dests[0].parent.mkdir(parents=True, exist_ok=True)
    for src, dest in zip(files, dests):
        if not _place(cfg, src, dest, fp):
            raise PlanError(f"{dest} appeared while filing")
        log.info("%s -> %s", src.name, dest)
    _remove_inbox(cfg, item["inbox_path"])
    discrip.resolve_pending(cfg, pending_id, str(dests[0].parent))
    record_rip(cfg, fp, item["disc_label"], "tv", dests, pending_id)
    remember(cfg, fp, item["disc_label"], kind="tv", show=show, season=season,
             first_episode=first_episode, year=year)

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


def season_members(cfg, key):
    """Parked TV discs of one show+season, in disc order (disc number, unnumbered
    discs count as disc 1, then rip order)."""
    members = [p for p in discrip.read_state(cfg)["pending"]
               if p["prompt"] == "tv_plan" and p.get("group") == key]
    return sorted(members, key=lambda p: (p["suggested"].get("disc") or 1, p["id"]))


def _renumber(cfg, key, members, cont):
    """Give each waiting disc provisional episode numbers continuing from the one
    before it, and note discs that wait for a missing earlier disc."""
    start = cont["next_episode"] if cont else 1
    # Position of the disc before: 0 = none filed yet, None = unknown (an earlier
    # unnumbered disc was filed), so no gap check. Unnumbered discs take the next slot.
    prev = cont.get("last_disc") if cont else 0
    waiting = None
    updates = {}
    for m in members:
        sug, disc = m["suggested"], m["suggested"].get("disc")
        if prev is not None:
            if waiting is None and disc and disc > prev + 1:
                waiting = f"disc {prev + 1}"
            prev = disc or prev + 1
        else:
            prev = disc
        paths = proposed_plan(cfg, sug["show"], sug["season"], start, len(m["titles"]),
                              sug.get("year"))
        updates[m["id"]] = (start, [p.stem for p in paths], waiting)
        start += len(m["titles"])
    with discrip.state_update(cfg) as state:
        for p in state["pending"]:
            if p["id"] in updates:
                first, names, wait = updates[p["id"]]
                p["suggested"]["first_episode"] = first
                p["waiting_for"] = wait
                for t, n in zip(p["titles"], names):
                    t["proposed"] = n
    return updates


def settle_season(cfg, key):
    with filing_lock(cfg):
        return _settle_season(cfg, key)


def _settle_season(cfg, key):
    """File waiting discs of a season from the front for as long as each needs no
    answer. Returns the ids filed. Spec 8.4 / [video] tv_confirm:
      always       never file without an answer
      when_unsure  file if the season was already confirmed, or the first disc is
                   sure (unsure_reasons() is empty)
      never        always file with the best guess
    """
    mode = cfg["video"]["tv_confirm"]
    filed = []
    while True:
        cont = _read(cfg, "tv_continuation.json").get(key)
        members = season_members(cfg, key)
        if not members:
            break
        updates = _renumber(cfg, key, members, cont)
        head = members[0]
        first, _, waiting = updates[head["id"]]
        sug = head["suggested"]
        reasons = [] if cont else unsure_reasons(
            cfg, sug["show"], sug["season"], sug.get("season_on_label"), sug.get("disc"),
            len(head["titles"]))
        if cont and sug.get("disc") and sug.get("disc") == cont.get("last_disc"):
            reasons = [f"disc {sug['disc']} of this season was already filed"]
        with discrip.state_update(cfg) as state:
            for p in state["pending"]:
                if p["id"] == head["id"]:
                    p["reasons"] = reasons
        if waiting or mode == "always" or (reasons and mode != "never"):
            break
        try:
            apply_tv_plan(cfg, head["id"], sug["show"], sug["season"], first, sug.get("year"))
        except PlanError as e:
            log.warning("%s: could not file automatically: %s", head["id"], e)
            break
        filed.append(head["id"])
    return filed


def answer_season(cfg, pending_id, show=None, season=None, first_episode=None, year=None):
    with filing_lock(cfg):
        return _answer_season(cfg, pending_id, show, season, first_episode, year)


def _answer_season(cfg, pending_id, show=None, season=None, first_episode=None, year=None):
    """The user's answer for a season: applies to the first waiting disc of the
    season pending_id belongs to (overrides change the whole season), then files
    every later disc that follows on. Returns the ids filed."""
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "tv_plan":
        raise PlanError(f"no tv_plan prompt {pending_id}")
    key = item["group"]
    head = season_members(cfg, key)[0]
    sug = head["suggested"]
    show = naming.sanitise(show or sug["show"] or "")
    if not show:
        raise PlanError("no show name: give one")
    season = season or sug["season"]
    year = year or sug.get("year")
    new_key = continuation_key(show, season)
    if new_key != key or year != sug.get("year"):
        with discrip.state_update(cfg) as state:
            for p in state["pending"]:
                if p.get("group") == key:
                    p["group"] = new_key
                    p["suggested"].update(show=show, season=season, year=year)
    first = first_episode or (_read(cfg, "tv_continuation.json").get(new_key) or {}).get(
        "next_episode") or (sug["first_episode"] if new_key == key else 1)
    apply_tv_plan(cfg, head["id"], show, season, first, year)
    return [head["id"]] + _settle_season(cfg, new_key)


def preview_season(cfg, pending_id, show=None, season=None, first_episode=None, year=None):
    """What answer_season would do, without doing it: [{id, disc_label, files}]."""
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "tv_plan":
        raise PlanError(f"no tv_plan prompt {pending_id}")
    members = season_members(cfg, item["group"])
    sug = members[0]["suggested"]
    show = naming.sanitise(show or sug["show"] or "") or "Unknown"
    season = season or sug["season"]
    year = year or sug.get("year")
    cont = continuation(cfg, show, season) or {}
    first = first_episode or cont.get("next_episode") or sug["first_episode"] or 1
    out = []
    for m in members:
        paths = proposed_plan(cfg, show, season, first, len(m["titles"]), year)
        out.append({"id": m["id"], "disc_label": m["disc_label"],
                    "files": [str(p.relative_to(cfg["paths"]["media"])) for p in paths]})
        first += len(m["titles"])
    return out


def answer_choose_titles(cfg, pending_id, indexes, kind):
    """Record which titles to rip from an ambiguous disc, and as what. The rip runs
    when the disc is next ready (the caller starts it if the disc is in now)."""
    item = discrip.get_pending(cfg, pending_id)
    if not item or item["prompt"] != "choose_titles":
        raise PlanError(f"no choose_titles prompt {pending_id}")
    known_idx = {t["index"] for t in item["titles"]}
    indexes = sorted(set(int(i) for i in indexes))
    if not indexes or not set(indexes) <= known_idx:
        raise PlanError("choose at least one of the listed titles")
    if kind not in ("film", "tv"):
        raise PlanError("kind must be film or tv")
    if kind == "film" and len(indexes) != 1:
        raise PlanError("a film is one title")
    with discrip.state_update(cfg) as state:
        for p in state["pending"]:
            if p["id"] == pending_id:
                p["answer"] = {"titles": indexes, "kind": kind}
    log.info("%s: rip titles %s as %s", pending_id, indexes, kind)


# ---------------------------------------------------------------- notifications


def notify(cfg, text):
    """Pop text up in every open Jellyfin app (web, iOS, ...). Jellyfin keeps no
    inbox, so only people using it right now see it. Best effort.
    [notify] popup_seconds = 0 (default) gives a dialog that stays until dismissed."""
    jf = cfg["jellyfin"]
    if not (jf["api_key"] and cfg["notify"]["jellyfin_popup"]):
        return
    headers = {"Authorization": f'MediaBrowser Token="{jf["api_key"]}"',
               "Content-Type": "application/json"}
    base = jf["url"].rstrip("/")
    message = {"Header": "discrip", "Text": text}
    if cfg["notify"]["popup_seconds"] > 0:
        # Jellyfin's apps show a timed message as a brief toast (a few seconds,
        # whatever the timeout); without one it is a dialog that stays until OK.
        message["TimeoutMs"] = cfg["notify"]["popup_seconds"] * 1000
    body = json.dumps(message).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/Sessions", headers=headers),
                                    timeout=10) as r:
            sessions = [x for x in json.load(r) if x.get("SupportsRemoteControl")]
        for x in sessions:
            urllib.request.urlopen(urllib.request.Request(
                f"{base}/Sessions/{x['Id']}/Message", data=body, headers=headers,
                method="POST"), timeout=10).close()
        log.info("notified %d Jellyfin session(s): %s", len(sessions), text)
    except (OSError, ValueError) as e:
        log.warning("Jellyfin notification failed: %s", e)


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
