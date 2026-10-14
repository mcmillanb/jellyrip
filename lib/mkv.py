"""Fix MKV track flags in place with mkvpropedit (no remux, takes a second).

MakeMKV marks the first subtitle track "default", so Jellyfin turns DVD
subtitles on. They are pictures a browser can't overlay, so Jellyfin then burns
them into the video: a full live transcode on every core. And an H.264 encode of
a progressive film keeps the DVD's interlaced field-order flag, so Jellyfin adds
a deinterlacer too.
"""
import json
import logging
import subprocess

log = logging.getLogger("mkv")


def tidy_flags(path, progressive=False):
    """Clear "default" on subtitle tracks (forced ones keep it). progressive: also
    mark the video track progressive. Returns the changes made (empty if none).
    Raises RuntimeError if mkvpropedit fails."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,field_order:stream_disposition=default,forced",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    streams = json.loads(r.stdout or "{}").get("streams", [])
    args, changes = [], []
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    for n, s in enumerate(subs, 1):
        d = s.get("disposition", {})
        if d.get("default") and not d.get("forced"):
            args += ["--edit", f"track:s{n}", "--set", "flag-default=0"]
            changes.append(f"subtitle {n} not default")
    video = [s for s in streams if s.get("codec_type") == "video"]
    if progressive and video and video[0].get("field_order", "progressive") not in ("progressive", "unknown"):
        args += ["--edit", "track:v1", "--set", "field-order=0"]
        changes.append(f"video progressive (was {video[0]['field_order']})")
    if args:
        p = subprocess.run(["mkvpropedit", str(path), *args], capture_output=True, text=True,
                           timeout=120)
        if p.returncode:
            raise RuntimeError(f"mkvpropedit failed: {(p.stdout + p.stderr).strip()[-300:]}")
        log.info("%s: %s", path, ", ".join(changes))
    return changes
