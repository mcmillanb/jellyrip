"""Shared helpers for the discrip scripts: config, drive access, state file, sd_notify.

Everything here is Python stdlib only.
"""
import contextlib
import copy
import datetime
import fcntl
import json
import os
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
        "pending": [],
        "history": [],
    }


@contextlib.contextmanager
def state_update(cfg):
    """Read-modify-write state.json under a lock; yields the dict to modify.

    Written atomically (temp file + rename), so readers never see a partial file.
    """
    state_dir = Path(cfg["paths"]["state_dir"])
    path = state_dir / "state.json"
    with open(state_dir / "state.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = empty_state()
        yield state
        state["updated_at"] = now_iso()
        tmp = path.with_name(f".state.json.{os.getpid()}")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)


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
