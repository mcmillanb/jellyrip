"""Turn disc labels into library names (spec 8.4)."""
import re

SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
               "on", "or", "the", "to", "vs", "with"}
ROMAN = re.compile(r"^(?=[ivxlc]+$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)


def tidy_label(label, noise):
    """BLADE_RUNNER_2049_D1 -> 'Blade Runner 2049'.

    Underscores/dots become spaces; trailing noise words or phrases (config list,
    case-insensitive, plus DISC<n>/D<n>) are stripped; then title case. A year
    survives only if it was in brackets.
    """
    text = re.sub(r"[_.]+", " ", label or "").strip()
    year = None
    m = re.search(r"\((\d{4})\)", text)
    if m:
        year = m.group(1)
        text = (text[:m.start()] + text[m.end():]).strip()
    words = text.split()
    phrases = sorted((n.upper().replace("_", " ").split() for n in noise), key=len, reverse=True)
    while words:
        if re.fullmatch(r"(DIS[CK]|D)\d*", words[-1], re.I):
            words.pop()
            continue
        if len(words) > 1 and words[-1].isdigit() and re.fullmatch(r"DIS[CK]|D", words[-2], re.I):
            del words[-2:]  # DISC 1
            continue
        tail = next((p for p in phrases if [w.upper() for w in words[-len(p):]] == p), None)
        if tail is None or len(tail) == len(words):
            break
        del words[-len(tail):]
    out = []
    for i, w in enumerate(words):
        if ROMAN.match(w):
            out.append(w.upper())
        elif i and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        elif any(c.islower() for c in w) and any(c.isupper() for c in w):
            out.append(w)  # already mixed case: trust it (e.g. McQueen)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    name = " ".join(out)
    return f"{name} ({year})" if year and name else name


def is_junk(label, tidied, junk):
    """True if the label can't be used as a film name."""
    junk = {j.upper().replace("_", " ") for j in junk}
    raw = (label or "").upper().replace("_", " ").strip()
    if not tidied or len(tidied) < 2:
        return True
    if raw in junk or tidied.upper() in junk:
        return True
    return bool(re.fullmatch(r"[\d\s]+", tidied))


def sanitise(name):
    """Safe as a single path component: no '/', control chars or trailing dots."""
    name = re.sub(r"[/\x00-\x1f\x7f]", " ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".").strip()
    return name


def parse_tv_label(label, noise):
    """FATHER_TED_S1_D2 -> ('Father Ted', 1, 2). Season and disc are None if absent.

    Season/disc tokens are taken from the end of the label in any order:
    S1, S01, SEASON_1, SERIES1, S1D2, D2, DISC_2, DISK2.
    """
    words = re.sub(r"[_.]+", " ", label or "").split()
    season = disc = None
    while words:
        w = words[-1]
        two = len(words) > 1 and w.isdigit()
        if m := re.fullmatch(r"S0*(\d{1,2})D0*(\d{1,2})", w, re.I):
            season, disc = int(m[1]), int(m[2])
            words.pop()
        elif m := re.fullmatch(r"(?:DIS[CK]|D)0*(\d{1,2})", w, re.I):
            disc = int(m[1])
            words.pop()
        elif two and re.fullmatch(r"DIS[CK]|D", words[-2], re.I):
            disc = int(w)
            del words[-2:]
        elif m := re.fullmatch(r"S(?:EASON|ERIES)?0*(\d{1,2})", w, re.I):
            season = int(m[1])
            words.pop()
        elif two and re.fullmatch(r"S|SEASON|SERIES", words[-2], re.I):
            season = int(w)
            del words[-2:]
        else:
            break
    return tidy_label(" ".join(words), noise), season, disc
