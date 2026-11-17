"""Short browser-playable clips of a parked rip, for the web app's Preview.

Browsers can't play DVD video (MPEG-2 in MKV), so each request makes a CLIP_S
second H.264/AAC MP4 from time t, small (288 lines, interlace-aware scaling: no
deinterlacer, which tripled the time) and quick: about 4 s per clip on a Pi 4.
Clips are cached in /run/discrip/preview (RAM), the newest KEEP of them.
"""
import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("preview")
CLIP_S = 30
KEEP = 12
DIR = Path("/run/discrip/preview")
_lock = threading.Lock()  # one ffmpeg at a time


def clip_start(t):
    """Clips start on CLIP_S boundaries, so seeks and prefetches share the cache."""
    return max(0, int(t) // CLIP_S * CLIP_S)


def _key(src):
    src = Path(src)
    return hashlib.sha1(f"{src}|{src.stat().st_mtime}".encode()).hexdigest()[:12]


def drop(src):
    """Delete src's clips (its prompt has been answered or discarded)."""
    try:
        key = _key(src)
    except OSError:
        return
    with _lock:
        for p in DIR.glob(f"{key}-*.mp4"):
            p.unlink(missing_ok=True)


def clip(src, t):
    """Path of the MP4 clip of src starting at clip_start(t); made if needed.
    Raises RuntimeError if ffmpeg fails."""
    src = Path(src)
    t = clip_start(t)
    key = _key(src)
    out = DIR / f"{key}-{t:05d}.mp4"
    with _lock:
        if out.exists():
            os.utime(out)
            return out
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        cmd = ["nice", "-n", "10", "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
               "-ss", str(t), "-i", str(src), "-t", str(CLIP_S),
               "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
               "-vf", "scale=-2:288:interl=1", "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "28", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ac", "2", "-b:a", "96k", "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip()
                               else f"ffmpeg failed ({r.returncode})")
        os.replace(tmp, out)
        old = sorted(DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[KEEP:]:
            p.unlink(missing_ok=True)
        log.info("preview clip %s at %d s", src.name, t)
        return out
