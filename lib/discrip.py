"""Shared helpers for the discrip scripts: config, drive access, state file, sd_notify.

Everything here is Python stdlib only.
"""
import contextlib
import copy
import datetime
import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
import tomllib
from pathlib import Path

CONFIG_PATH = os.environ.get("DISCRIP_CONFIG", "/opt/discrip/discrip.toml")

DEFAULTS = {
    "paths": {
        "media": "/srv/media",
        "state_dir": "/var/lib/discrip",
        "run_dir": "/run/discrip",
    },
    "drive": {
        # Candidates, first existing one wins. A stable by-id/by-path link survives
        # a USB re-enumeration that renumbers /dev/sr0.
        "device": ["/dev/sr0"],
        "poll_interval": 2,
        "unresponsive_after": 30,
        "ready_timeout": 60,
    },
    "dispatch": {
        "auto_start": True,
        "rip_unit": "discrip-rip.service",
        "eject_when_done": True,
        "min_free_gb": 25,
    },
    "state": {
        "history_max": 50,
    },
    "audio": {
        "abcde_conf": "/opt/discrip/abcde.conf",
        "rip_timeout": 3600,
    },
    "encode": {
        # Defaults; the web app stores changes in /var/lib/discrip/encode.json.
        "enabled": True,
        "window_start": "01:00",   # HH:MM; a window may cross midnight
        "window_end": "07:00",
        "codec": "h264",           # h264 (plays everywhere) or h265 (smaller, slower)
        "keep_originals": False,   # True: keep the MPEG-2 file in _originals/
        "max_temp": 75,            # pause above this CPU temperature (resume 5 below)
    },
    "web": {
        "port": 8080,
        "token": "",               # shared secret for controls; setup/03-discrip.sh makes one
    },
    "notify": {
        "jellyfin_popup": True,    # pop up prompts and failures in open Jellyfin apps
        "popup_seconds": 0,        # 0: a dialog that stays until OK; >0: a brief toast
    },
    "power": {
        # Momentary button between this GPIO and GND: hold 5 s to shut down; a
        # press also starts a halted Pi, but only on GPIO 3 (header pins 5 and 6).
        "button_gpio": 3,          # 0: no button
    },
    "leds": {
        "off": ["ACT", "PWR"],     # board LEDs (/sys/class/leds) kept off while running
        "ethernet_off": False,     # also the Ethernet socket's LEDs (boot config, reboot)
    },
    "jellyfin": {
        "url": "http://localhost:8096",
        "api_key": "",             # Dashboard -> API Keys; empty disables refreshes
    },
    "video": {
        "minlength": 600,          # seconds; shorter titles are ignored
        "film_min": 3600,          # a film's main title is at least this long
        "episode_min": 1080,       # season discs: episode duration band
        "episode_max": 4200,
        "episode_tolerance": 0.2,  # episodes within 20% of the band median
        "season_min_titles": 3,
        "max_candidates": 12,      # more titles than this: ask (decoy playlists)
        "duplicate_tolerance": 0.02,  # >1 title this close to the longest: ask
        "scan_timeout": 1800,
        "rip_timeout": 14400,
        "tv_lookup": True,         # episode names from TVmaze (names only, never order)
        "tv_confirm": "when_unsure",  # always | when_unsure | never (library.settle_season)
        "label_noise": ["WS", "FS", "PAL", "NTSC", "DVD", "BLURAY", "BD", "R1", "R2",
                        "UK", "SE", "SPECIAL EDITION", "COLLECTORS EDITION",
                        "FAM FUN EDITION", "FAMILY FUN EDITION", "WIDESCREEN"],
        "junk_labels": ["DVD", "DVD_VIDEO", "DVDVIDEO", "DVDVOLUME", "VIDEO_TS",
                        "LOGICAL_VOLUME_ID", "DISC", "MOVIE", "FILM", "VOLUME",
                        "NO_LABEL", "UNTITLED"],
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path=CONFIG_PATH):
    cfg = copy.deepcopy(DEFAULTS)
    with contextlib.suppress(FileNotFoundError):
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if isinstance(cfg["drive"]["device"], str):
        cfg["drive"]["device"] = [cfg["drive"]["device"]]
    return cfg


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- drive

CDROM_DRIVE_STATUS = 0x5326
CDSL_CURRENT = 0x7FFFFFFF

# CDROM_DRIVE_STATUS return values (linux/cdrom.h)
DRIVE_STATUS = {0: "no_info", 1: "no_disc", 2: "tray_open", 3: "not_ready", 4: "ready"}
CDS_TRAY_OPEN, CDS_DISC_OK = 2, 4


def find_drive(cfg):
    """Return the first configured device path that exists, or None."""
    for path in cfg["drive"]["device"]:
        if os.path.exists(path):
            return os.path.realpath(path)
    return None


def drive_status(dev):
    """CDROM_DRIVE_STATUS for dev (1-4, see DRIVE_STATUS). Doesn't spin the disc.

    Opens and closes the device every call, so a recreated /dev/sr0 is picked up.
    Raises OSError if the device can't be opened or the ioctl fails.
    """
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
    finally:
        os.close(fd)


def eject(dev):
    """Eject and confirm the tray opened. Returns True on success.

    Uses util-linux eject: a bare CDROMEJECT ioctl returns 0 on this drive while
    the drive refuses with "medium removal prevented" (sense 5/53/02).
    """
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(["eject", dev], capture_output=True, timeout=30)
    for _ in range(10):
        with contextlib.suppress(OSError):
            if drive_status(dev) == CDS_TRAY_OPEN:
                return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- state file


def empty_state():
    return {
        "schema_version": 2,
        "updated_at": None,
        "drive": {"present": False, "status": None, "media": None, "label": None,
                  "last_poll": None},
        "job": None,
        "message": None,
        "pending": [],
        "history": [],
    }


@contextlib.contextmanager
def json_update(path, default):
    """Read-modify-write a JSON file under a lock; yields the data to modify.

    Written atomically (temp file + rename), so readers never see a partial file.
    """
    path = Path(path)
    with open(path.with_name(f".{path.name}.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = default()
        yield data
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)


@contextlib.contextmanager
def state_update(cfg):
    """Read-modify-write state.json; yields the state dict to modify."""
    with json_update(Path(cfg["paths"]["state_dir"]) / "state.json", empty_state) as state:
        yield state
        state["updated_at"] = now_iso()


def read_state(cfg):
    try:
        return json.loads((Path(cfg["paths"]["state_dir"]) / "state.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return empty_state()


# ---------------------------------------------------------------- rip lock and jobs


class Busy(Exception):
    """Another process holds the rip lock."""


@contextlib.contextmanager
def rip_lock(cfg, wait=0):
    """Hold the one-rip-at-a-time lock (flock on run_dir/lock); raise Busy if taken.

    The lock lives on tmpfs, so a reboot or crash can't leave it held.
    """
    with open(Path(cfg["paths"]["run_dir"]) / "lock", "a") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(0.2)
        yield


def _close_job(cfg, state, result, error=None, output=None):
    job = state["job"]
    state["history"].insert(0, {
        "id": job["id"],
        "type": job["type"],
        "title": job["title"],
        "result": result,
        "finished_at": now_iso(),
        "output": output,
        "error": error,
    })
    del state["history"][cfg["state"]["history_max"]:]
    state["job"] = None


def start_job(cfg, job_id):
    with state_update(cfg) as state:
        state["job"] = {
            "id": job_id, "type": None, "kind": None, "stage": "detecting",
            "title": None, "percent": None, "detail": None,
            "started_at": now_iso(), "error": None,
        }
        state["message"] = None


def update_job(cfg, **fields):
    with state_update(cfg) as state:
        if state["job"]:
            state["job"].update(fields)


def finish_job(cfg, result, error=None, output=None):
    """Move the current job into history. result: ok, error, cancelled or skipped."""
    with state_update(cfg) as state:
        if state["job"]:
            _close_job(cfg, state, result, error, output)


def recover_stale_job(cfg):
    """Close a job left behind by a crash or reboot. Caller must hold rip_lock.

    Returns the stale job, or None.
    """
    with state_update(cfg) as state:
        job = state["job"]
        if job:
            _close_job(cfg, state, "error", f"interrupted at stage {job['stage']!r} "
                       "(reboot, crash or kill)")
        return job


def add_pending(cfg, item):
    """Add a prompt for the user (spec 8.2 pending); replaces one with the same id."""
    with state_update(cfg) as state:
        state["pending"] = [p for p in state["pending"] if p["id"] != item["id"]]
        state["pending"].append({**item, "created_at": now_iso()})


def get_pending(cfg, pending_id):
    return next((p for p in read_state(cfg)["pending"] if p["id"] == pending_id), None)


def resolve_pending(cfg, pending_id, output=None):
    """Drop a pending item; record where its files went in the job's history entry."""
    with state_update(cfg) as state:
        state["pending"] = [p for p in state["pending"] if p["id"] != pending_id]
        for h in state["history"]:
            if h["id"] == pending_id:
                h["output"] = output


def check_free_space(cfg, need_bytes=0):
    """Return an error string if media would drop below min_free_gb, else None."""
    media = cfg["paths"]["media"]
    free_gb = shutil.disk_usage(media).free / 1e9
    need_gb = need_bytes / 1e9 + cfg["dispatch"]["min_free_gb"]
    if free_gb < need_gb:
        return f"only {free_gb:.0f} GB free on {media}, need {need_gb:.0f} GB"
    return None


def set_message(cfg, text):
    """A notice for the UI that isn't tied to a job (e.g. a rejected disc)."""
    with state_update(cfg) as state:
        state["message"] = {"text": text, "at": now_iso()} if text else None


# ---------------------------------------------------------------- systemd


def sd_notify(message):
    """Send a sd_notify(3) message; a no-op when not run by systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(message.encode())
