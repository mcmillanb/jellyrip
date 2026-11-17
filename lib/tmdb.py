"""Film names from TMDb (themoviedb.org) at rip time.

The disc label gives a search phrase; TMDb gives the film's proper title and year,
and its id goes in the folder name so Jellyfin matches the right film for sure:
  Movies/Up (2009) [tmdbid-14160]/Up (2009).mkv
Jellyfin then fetches posters, descriptions etc. itself when it scans the library.

Only a confident match is used: the title must equal the search phrase (ignoring
case, punctuation and the small words the/a/an/and/of), and TMDb's running time must agree
with the disc's main title. Otherwise the caller keeps the label name, as before.

The key: set in the web app (stored in state_dir/tmdb.json, 0600) or [tmdb] api_key
in discrip.toml. Either a v3 API key or a v4 read access token works.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("tmdb")
API = "https://api.themoviedb.org/3"
CANDIDATES = 5  # search results whose details are fetched to compare running times


class TmdbError(Exception):
    pass


# ---------------------------------------------------------------- key

def _key_file(cfg):
    return Path(cfg["paths"]["state_dir"]) / "tmdb.json"


def get_key(cfg):
    """(key, source): the web app's key first, then discrip.toml's; ("", None) if none."""
    try:
        key = json.loads(_key_file(cfg).read_text()).get("api_key", "").strip()
        if key:
            return key, "web"
    except (FileNotFoundError, ValueError):
        pass
    key = cfg.get("tmdb", {}).get("api_key", "").strip()
    return (key, "config") if key else ("", None)


def save_key(cfg, key):
    """Check the key with TMDb, then store it ("" removes it). Raises TmdbError."""
    key = key.strip()
    path = _key_file(cfg)
    if not key:
        path.unlink(missing_ok=True)
        return
    _get(key, "/configuration")  # raises if TMDb refuses it
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"api_key": key}, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------- API

def _get(key, path, params=None, timeout=10):
    params = dict(params or {})
    headers = {"Accept": "application/json"}
    if key.startswith("eyJ"):  # v4 read access token (a JWT)
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise TmdbError("TMDb refused the key (401)") from None
        raise TmdbError(f"TMDb error {e.code}") from None
    except (OSError, ValueError) as e:
        raise TmdbError(f"TMDb unreachable: {e}") from None


# ---------------------------------------------------------------- matching

def _norm(title):
    t = re.sub(r"['\u2019`]", "", title.lower())  # It's / It’s / Its alike
    t = re.sub(r"[^\w\s]", " ", t.replace("&", " and "))
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


SMALL_WORDS = {"the", "a", "an", "and", "of"}


def _loose(title):
    """_norm without small words: disc labels often drop them
    (MONTY_PYTHON_THE_HOLY_GRAIL for 'Monty Python and the Holy Grail')."""
    return " ".join(w for w in _norm(title).split() if w not in SMALL_WORDS)


EDITION = re.compile(
    r"\s*[\(\[][^\)\]]*[\)\]]\s*$"                      # trailing (...) or [...]
    r"|\s*[-:]?\s*\b((special|collector'?s|anniversary|extended|ultimate|deluxe|"
    r"limited|definitive|director'?s cut|platinum|remastered)\b.*)$", re.I)


def search_phrase(query):
    """Drop edition tags: 'Monty Python and the Holy Grail (Special Edition)' ->
    'Monty Python and the Holy Grail'."""
    q = query.strip()
    while True:
        new = EDITION.sub("", q).strip()
        if new == q or not new:
            return q
        q = new


def runtime_ok(tmdb_min, disc_s):
    """TMDb's running time (minutes) against the disc's main title (seconds). PAL
    DVDs play 4% fast, and listed times vary, so allow 6 min or 8%."""
    if not tmdb_min or not disc_s:
        return False
    disc_min = disc_s / 60
    slack = max(6, 0.08 * tmdb_min)
    return any(abs(disc_min - m) <= slack for m in (tmdb_min, tmdb_min * 24 / 25))


def folder_safe(title):
    """A title as a path component that also works over Samba (Windows/macOS):
    'Star Wars: Episode IV' -> 'Star Wars - Episode IV'."""
    t = re.sub(r"\s*:\s*", " - ", title)
    t = re.sub(r'[<>"\\|?*/\x00-\x1f]', "", t)
    return re.sub(r"\s+", " ", t).strip().rstrip(".").strip()


def match_film(cfg, query, disc_s, year=None):
    """The film for search phrase `query` (e.g. a tidied disc label) whose running
    time fits a disc title of disc_s seconds, or None if no confident match.
    Returns {"id", "title", "year", "runtime", "name", "folder"}: name is
    'Title (Year)' for the file, folder adds '[tmdbid-N]'. Never raises."""
    key, _ = get_key(cfg)
    if not key or not query:
        return None
    tried = []
    for q in (query, search_phrase(query)):  # the full name first, then without
        if q not in tried:                    # an edition tag
            tried.append(q)
            match = _match(cfg, key, q, disc_s, year)
            if match:
                return match
    return None


def _match(cfg, key, query, disc_s, year):
    lang = cfg.get("tmdb", {}).get("language", "en-GB")
    try:
        params = {"query": query, "include_adult": "false", "language": lang}
        if year:
            params["year"] = year
        results = _get(key, "/search/movie", params).get("results", [])[:CANDIDATES]
        want = _loose(query)
        found = []
        for r in results:
            titles = {_loose(r.get("title", "")), _loose(r.get("original_title", ""))}
            if not want or want not in titles or not r.get("release_date"):
                continue
            details = _get(key, f"/movie/{r['id']}", {"language": lang})
            if runtime_ok(details.get("runtime"), disc_s):
                found.append((r.get("popularity") or 0, details))
    except TmdbError as e:
        log.warning("TMDb lookup of %r failed: %s", query, e)
        return None
    if not found:
        log.info("TMDb: no film called %r with a running time near %d min",
                 query, disc_s // 60)
        return None
    found.sort(key=lambda x: x[0], reverse=True)
    if len(found) > 1 and found[1][0] > 0.5 * found[0][0]:
        log.info("TMDb: %r is ambiguous (%s)", query,
                 ", ".join(f"{d['title']} ({d['release_date'][:4]})" for _, d in found))
        return None
    d = found[0][1]
    yr = d["release_date"][:4]
    name = f"{folder_safe(d['title'])} ({yr})"
    match = {"id": d["id"], "title": d["title"], "year": yr, "runtime": d.get("runtime"),
             "name": name, "folder": f"{name} [tmdbid-{d['id']}]"}
    log.info("TMDb: %r -> %s (id %s, %s min)", query, match["folder"], d["id"],
             d.get("runtime"))
    return match


# ---------------------------------------------------------------- choosing by hand

POSTER = "https://image.tmdb.org/t/p/w92"
SEARCH_MAX = 8


def _film(d):
    """TMDb movie details -> what the rest of discrip files a film by."""
    yr = (d.get("release_date") or "")[:4]
    name = f"{folder_safe(d['title'])} ({yr})" if yr else folder_safe(d["title"])
    return {"id": d["id"], "title": d["title"], "year": yr or None,
            "runtime": d.get("runtime"), "name": name, "folder": f"{name} [tmdbid-{d['id']}]"}


def search_films(cfg, query, disc_s=None):
    """Candidates for the web app's Look up: up to SEARCH_MAX films for `query`,
    those whose running time fits the disc first. Each is _film() plus overview,
    poster (URL or None), popularity and fits. Raises TmdbError (no key, refused,
    unreachable)."""
    key, _ = get_key(cfg)
    if not key:
        raise TmdbError("no TMDb key: add one under Settings → Film details")
    lang = cfg.get("tmdb", {}).get("language", "en-GB")
    results = []
    for q in dict.fromkeys([query.strip(), search_phrase(query)]):
        if q:
            results = _get(key, "/search/movie", {"query": q, "include_adult": "false",
                                                  "language": lang}).get("results", [])
            if results:
                break
    out = []
    for r in results[:SEARCH_MAX]:
        try:
            d = _get(key, f"/movie/{r['id']}", {"language": lang})
        except TmdbError:
            d = r
        f = _film(d)
        f.update(overview=(d.get("overview") or "")[:240],
                 poster=POSTER + d["poster_path"] if d.get("poster_path") else None,
                 popularity=r.get("popularity") or 0,
                 fits=bool(disc_s) and runtime_ok(d.get("runtime"), disc_s))
        out.append(f)
    out.sort(key=lambda f: (not f["fits"], -f["popularity"]))
    return out


def film_by_id(cfg, tmdb_id):
    """The film with this TMDb id, as _film(). Raises TmdbError."""
    key, _ = get_key(cfg)
    if not key:
        raise TmdbError("no TMDb key")
    lang = cfg.get("tmdb", {}).get("language", "en-GB")
    return _film(_get(key, f"/movie/{int(tmdb_id)}", {"language": lang}))
