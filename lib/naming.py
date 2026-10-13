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
