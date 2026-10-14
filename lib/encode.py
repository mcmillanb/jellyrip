"""Overnight re-encoding of DVD rips (MPEG-2) to H.264 or H.265, one file at a time.

Run by bin/encoder (discrip-encode.service). The ffmpeg process is paused
(SIGSTOP), not killed, while outside the time window, while anything plays in
Jellyfin, while a disc rips, or while the CPU is hot; it resumes (SIGCONT) where
it left off. A finished encode is checked against the original (duration and
stream counts) and then replaces it in place, so library paths never change.

Settings live in /var/lib/discrip/encode.json (edited from the web app),
defaulting to [encode] in discrip.toml.
"""
import datetime
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import discrip
import mkv

log = logging.getLogger("encode")

CODECS = {
    # name: (ffmpeg args, codec_name that ffprobe reports)
    "h264": (["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-profile:v", "high",
              "-pix_fmt", "yuv420p"], "h264"),
    "h265": (["-c:v", "libx265", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
              "-tag:v", "hvc1"], "hevc"),
}
SOURCE_CODECS = {"mpeg2video", "mpeg1video"}


# ---------------------------------------------------------------- settings


def settings_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "encode.json"


def settings(cfg):
    s = dict(cfg["encode"])
    try:
        s.update(json.loads(settings_path(cfg).read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return s


def save_settings(cfg, new):
    """Validate and store settings from the web app. Returns the stored settings."""
    s = settings(cfg)
    for key in ("window_start", "window_end"):
        if key in new:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(new[key])):
                raise ValueError(f"{key} must be HH:MM")
            s[key] = new[key]
    if "codec" in new:
        if new["codec"] not in CODECS:
            raise ValueError("codec must be h264 or h265")
        s["codec"] = new["codec"]
    for key in ("enabled", "keep_originals"):
        if key in new:
            s[key] = bool(new[key])
    with discrip.json_update(settings_path(cfg), dict) as stored:
        stored.clear()
        stored.update({k: s[k] for k in ("enabled", "window_start", "window_end", "codec",
                                         "keep_originals")})
    return s


def in_window(s, now=None):
    """True if now is inside [window_start, window_end), which may cross midnight.
    Equal start and end means all day."""
    now = (now or datetime.datetime.now()).strftime("%H:%M")
    start, end = s["window_start"], s["window_end"]
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


# ---------------------------------------------------------------- probing


def probe(path):
    """ffprobe summary: {duration, video, audio, subtitle} or None if unreadable."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,codec_name", "-of", "json",
                            str(path)], capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    streams = data.get("streams", [])
    video = [x["codec_name"] for x in streams if x.get("codec_type") == "video"]
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "video": video[0] if video else None,
        "audio": sum(1 for x in streams if x.get("codec_type") == "audio"),
        "subtitle": sum(1 for x in streams if x.get("codec_type") == "subtitle"),
    }


def interlaced(path, duration):
    """Decide from the frames (idet on ~40 s mid-file), not the stream's field-order
    flag: DVDs often flag progressive films as interlaced."""
    start = max(0, duration / 2 - 20)
    r = subprocess.run(["nice", "-n", "19", "ffmpeg", "-hide_banner", "-nostdin", "-ss",
                        str(start), "-t", "40", "-i", str(path), "-map", "0:v:0", "-vf",
                        "idet", "-an", "-sn", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    m = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) Progressive:\s*(\d+)",
                   r.stderr)
    if not m:
        return False
    tff, bff, prog = map(int, m[-1])
    return tff + bff > prog


# ---------------------------------------------------------------- queue


def index_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "encode_index.json"


def refresh_index(cfg):
    """Probe new or changed .mkv files under Movies/ and TV/ (cached by size and
    mtime). Returns the index {path: {size, mtime, video, status, ...}}."""
    media = Path(cfg["paths"]["media"])
    seen = set()
    with discrip.json_update(index_path(cfg), dict) as idx:
        for path in sorted(list((media / "Movies").rglob("*.mkv")) +
                           list((media / "TV").rglob("*.mkv"))):
            st = path.stat()
            key = str(path)
            seen.add(key)
            entry = idx.get(key)
            if entry and entry["size"] == st.st_size and entry["mtime"] == int(st.st_mtime):
                continue
            info = probe(path)
            idx[key] = {"size": st.st_size, "mtime": int(st.st_mtime),
                        "video": info["video"] if info else None,
                        "status": (entry or {}).get("status") if entry else None}
            if entry and entry.get("status") == "failed":
                idx[key]["status"] = None  # the file changed: worth another try
        for key in [k for k in idx if k not in seen]:
            del idx[key]
        return dict(idx)


def queue(cfg, idx=None):
    """Files still to encode, oldest first."""
    idx = idx if idx is not None else refresh_index(cfg)
    todo = [(e["mtime"], p) for p, e in idx.items()
            if e.get("video") in SOURCE_CODECS and e.get("status") != "failed"]
    return [p for _, p in sorted(todo)]


def mark(cfg, path, **fields):
    with discrip.json_update(index_path(cfg), dict) as idx:
        if path in idx:
            idx[path].update(fields)


# ---------------------------------------------------------------- blockers


def jellyfin_playing(cfg):
    """Titles playing (or paused mid-play) in Jellyfin right now; [] if none or
    Jellyfin can't be asked (then don't block on it)."""
    jf = cfg["jellyfin"]
    if not jf["api_key"]:
        return []
    req = urllib.request.Request(jf["url"].rstrip("/") + "/Sessions",
                                 headers={"Authorization": f'MediaBrowser Token="{jf["api_key"]}"'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return [x["NowPlayingItem"].get("Name", "?") for x in json.load(r)
                    if x.get("NowPlayingItem")]
    except (OSError, ValueError, KeyError):
        return []


def cpu_temp():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def cpu_mhz():
    try:
        return int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
                   .read_text()) // 1000
    except (OSError, ValueError):
        return None


def throttled():
    """vcgencmd get_throttled as an int, or None."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=5).stdout
        return int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------- temperature log
# One line a minute, encoding or not, so a night's encode can be compared with
# the idle baseline: "time,temp_c,mhz,throttled,state". Kept for TEMPS_DAYS.

TEMPS_DAYS = 7


def temps_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "temps.csv"


def record_temp(cfg, state):
    t = cpu_temp()
    if t is None:
        return None
    th = throttled()
    path = temps_path(cfg)
    with open(path, "a") as f:
        f.write(f"{discrip.now_iso()},{t:.1f},{cpu_mhz() or ''},"
                f"{'' if th is None else hex(th)},{state}\n")
    return t


def trim_temps(cfg):
    path = temps_path(cfg)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=TEMPS_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        lines = path.read_text().splitlines(keepends=True)
    except FileNotFoundError:
        return
    keep = [ln for ln in lines if ln[:20] >= cutoff]  # same format as discrip.now_iso
    if len(keep) < len(lines):
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(keep))
        os.replace(tmp, path)


def read_temps(cfg, hours=24):
    """Samples from the last `hours`: [{t, c, mhz, throttled, state}], oldest first."""
    cutoff = time.time() - hours * 3600
    out = []
    try:
        lines = temps_path(cfg).read_text().splitlines()
    except FileNotFoundError:
        return out
    for ln in lines:
        parts = ln.split(",")
        if len(parts) != 5:
            continue
        try:
            when = datetime.datetime.fromisoformat(parts[0])
            if when.timestamp() < cutoff:
                continue
            out.append({"t": parts[0], "c": float(parts[1]),
                        "mhz": int(parts[2]) if parts[2] else None,
                        "throttled": parts[3] or None, "state": parts[4]})
        except ValueError:
            continue
    return out


def rip_running(cfg):
    st = subprocess.run(["systemctl", "is-active", cfg["dispatch"]["rip_unit"]],
                        capture_output=True, text=True).stdout.strip()
    return st in ("active", "activating", "deactivating")


def pause_reason(cfg, s, hot):
    """Why encoding must not run now, or None. hot: currently paused for heat."""
    if not s["enabled"]:
        return "turned off"
    if not in_window(s):
        return f"outside the {s['window_start']}-{s['window_end']} window"
    if rip_running(cfg):
        return "a disc is ripping"
    playing = jellyfin_playing(cfg)
    if playing:
        return f"someone is watching ({playing[0]})"
    t = cpu_temp()
    limit = cfg["encode"]["max_temp"]
    if t is not None and (t >= limit or (hot and t >= limit - 5)):
        return f"CPU at {t:.0f} °C"
    return None


# ---------------------------------------------------------------- one job


class Job:
    """One ffmpeg encode of src into _work, pausable with SIGSTOP/SIGCONT."""

    def __init__(self, cfg, src, codec):
        self.cfg, self.src, self.codec = cfg, Path(src), codec
        self.work = Path(cfg["paths"]["media"]) / "_work" / f"encode-{int(time.time())}"
        self.out = self.work / self.src.name
        st = self.src.stat()
        self.src_id = (st.st_ino, st.st_size, st.st_mtime_ns)  # to notice a re-rip
        self.info = probe(self.src)
        self.duration = self.info["duration"] if self.info else 0
        self.done_s = 0.0
        self.paused = False
        self.started = time.time()
        self.proc = None

    def start(self):
        if not self.info or not self.duration:
            raise RuntimeError("ffprobe could not read the file")
        self.work.mkdir(parents=True)
        deint = interlaced(self.src, self.duration)
        args, _ = CODECS[self.codec]
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(self.src),
               "-map", "0", "-c", "copy", *args]
        if deint:
            cmd += ["-vf", "bwdif=mode=send_frame"]
        cmd += ["-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats",
                str(self.out)]
        log.info("encoding %s (%s%s, %.0f min)", self.src, self.codec,
                 ", deinterlacing" if deint else "", self.duration / 60)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, start_new_session=True)
        threading.Thread(target=self._read_progress, daemon=True).start()

    def _read_progress(self):
        # -progress pipe:1 writes key=value blocks; out_time_us is the position.
        for line in self.proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                self.done_s = int(value) / 1e6

    @property
    def percent(self):
        return round(min(100.0, 100 * self.done_s / self.duration), 1) if self.duration else 0

    def pause(self):
        if not self.paused:
            os.killpg(self.proc.pid, signal.SIGSTOP)
            self.paused = True

    def resume(self):
        if self.paused:
            os.killpg(self.proc.pid, signal.SIGCONT)
            self.paused = False

    def kill(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGCONT)
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self.cleanup()

    def cleanup(self):
        if self.work.exists():
            assert self.work.parent == Path(self.cfg["paths"]["media"]) / "_work"
            shutil.rmtree(self.work)

    def finish(self, keep_original):
        """Check the encode and put it in place of the original. Returns
        (ok, message); ok is None if the original changed meanwhile (not a failure)."""
        rc = self.proc.wait()
        try:
            if rc != 0:
                return False, f"ffmpeg failed (rc={rc})"
            new = probe(self.out)
            want = CODECS[self.codec][1]
            if not new or new["video"] != want:
                return False, f"output video is {new and new['video']}, expected {want}"
            if abs(new["duration"] - self.duration) > 2:
                return False, (f"duration {new['duration']:.1f} s differs from the "
                               f"original's {self.duration:.1f} s")
            if (new["audio"], new["subtitle"]) != (self.info["audio"], self.info["subtitle"]):
                return False, "audio/subtitle streams differ from the original"
            if not self.src.exists():
                return False, "the original disappeared while encoding"
            st = self.src.stat()
            if (st.st_ino, st.st_size, st.st_mtime_ns) != self.src_id:
                # e.g. the disc was ripped again meanwhile: keep the new rip; it is
                # queued again because its size and mtime changed.
                return None, "the original changed while encoding; result discarded"
            # The output is progressive (deinterlaced, or progressive to start with).
            try:
                mkv.tidy_flags(self.out, progressive=True)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                return False, str(e)
            before, after = self.src.stat().st_size, self.out.stat().st_size
            os.chmod(self.out, 0o664)
            if keep_original:
                keep = Path(self.cfg["paths"]["media"]) / "_originals"
                keep.mkdir(exist_ok=True)
                rel = self.src.relative_to(self.cfg["paths"]["media"])
                dest = keep / str(rel).replace("/", " - ")
                os.link(self.src, dest)
            os.replace(self.out, self.src)  # same filesystem: atomic, path unchanged
            took = time.time() - self.started
            took = f"{took / 3600:.1f} h" if took >= 3600 else f"{took / 60:.0f} min"
            return True, f"{before / 1e9:.2f} GB -> {after / 1e9:.2f} GB in {took}"
        finally:
            self.cleanup()
