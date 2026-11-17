"""Wi-Fi networks from the web app, through NetworkManager (nmcli).

Any number of networks can be saved; each is a NetworkManager connection that
connects automatically, so the Pi joins whichever saved network is in range (the
one used most recently if several are). Ethernet stays preferred while its cable is
in (NetworkManager gives wired routes the lower metric), so Wi-Fi is the fallback,
or the only link once the cable is out.

The discrip user may change NetworkManager's system connections through the polkit
rule in systemd/50-discrip.rules. Passwords are stored by NetworkManager (root-only
files in /etc/NetworkManager/system-connections) and are never returned here.
"""
import logging
import re
import subprocess

log = logging.getLogger("wifi")
IFACE = "wlan0"


class WifiError(Exception):
    pass


def _nmcli(*args, timeout=20, check=True):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise WifiError("NetworkManager (nmcli) isn't installed") from None
    except subprocess.TimeoutExpired:
        raise WifiError("NetworkManager didn't answer in time") from None
    if check and r.returncode:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise WifiError(msg[-1] if msg else f"nmcli failed ({r.returncode})")
    return r


def _split(line):
    """One line of nmcli -t output -> fields (":" separates, "\\:" is a colon)."""
    return [f.replace("\x00", ":") for f in line.replace("\\:", "\x00").split(":")]


def _ip(dev):
    r = _nmcli("-t", "-f", "IP4.ADDRESS", "device", "show", dev, check=False)
    m = re.search(r"IP4\.ADDRESS\[1\]:([\d.]+)", r.stdout)
    return m.group(1) if m else None


def _saved():
    """[(uuid, ssid, hidden)] for every saved Wi-Fi connection."""
    r = _nmcli("-t", "-f", "UUID,TYPE", "connection", "show", check=False)
    out = []
    for line in r.stdout.splitlines():
        uuid, typ = (_split(line) + [""])[:2]
        if typ != "802-11-wireless":
            continue
        d = _nmcli("-t", "-f", "802-11-wireless.ssid,802-11-wireless.hidden",
                   "connection", "show", uuid, check=False)
        # key:value lines: the value isn't escaped, so split at the first colon only
        f = dict(l.partition(":")[::2] for l in d.stdout.splitlines())
        if f.get("802-11-wireless.ssid"):
            out.append((uuid, f["802-11-wireless.ssid"], f.get("802-11-wireless.hidden") == "yes"))
    return out


def status():
    """Saved networks (no passwords), and how the Wi-Fi and Ethernet links are doing."""
    s = {"networks": [], "wifi": {"state": "unavailable"}, "ethernet": {"state": "unavailable"}}
    r = _nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device", "status", check=False)
    for line in r.stdout.splitlines():
        dev, typ, state = (_split(line) + [""] * 3)[:3]
        if typ not in ("wifi", "ethernet") or s[typ]["state"] == "connected":
            continue  # first device of each kind; a connected one wins
        s[typ] = {"device": dev, "state": state.split(" ")[0],
                  "ip": _ip(dev) if state.startswith("connected") else None}
    if s["wifi"]["state"] == "connected":
        r = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list",
                   "ifname", s["wifi"]["device"], "--rescan", "no", check=False)
        for line in r.stdout.splitlines():
            f = _split(line)
            if f[0] == "*" and len(f) >= 3:
                s["wifi"]["ssid"] = ":".join(f[1:-1])
                s["wifi"]["signal"] = int(f[-1]) if f[-1].isdigit() else None
    current = s["wifi"].get("ssid")
    s["networks"] = [{"ssid": ssid, "hidden": hidden, "connected": ssid == current}
                     for _, ssid, hidden in sorted(_saved(), key=lambda x: x[1].lower())]
    return s


def add(ssid, password, hidden=False):
    """Save a network (replacing a saved one with the same name), then try to join
    it if the Pi isn't already on Wi-Fi. Returns status() plus "joined" (True, False,
    or None if it didn't try) and "message" when joining failed. Raises WifiError on
    bad input or if NetworkManager refuses the change."""
    ssid = ssid.strip()
    if not ssid or len(ssid.encode()) > 32:
        raise WifiError("the network name must be 1 to 32 characters")
    if password and not (8 <= len(password) <= 63
                         or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise WifiError("a WPA password is 8 to 63 characters")
    for uuid, saved, _ in _saved():
        if saved == ssid:
            _nmcli("connection", "delete", uuid)
    args = ["connection", "add", "type", "wifi", "con-name", f"wifi {ssid}",
            "ifname", IFACE, "ssid", ssid,
            "802-11-wireless.hidden", "yes" if hidden else "no",
            "connection.autoconnect", "yes"]
    if password:
        # wpa-psk covers WPA2 and WPA2/WPA3 mixed-mode routers.
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    _nmcli(*args)
    _nmcli("radio", "wifi", "on", check=False)
    log.warning("Wi-Fi network %r saved from the web app", ssid)

    joined, message = None, None
    before = status()
    if before["wifi"]["state"] != "connected" or before["wifi"].get("ssid") == ssid:
        r = _nmcli("--wait", "30", "connection", "up", f"wifi {ssid}",
                   timeout=45, check=False)
        joined = r.returncode == 0
        if not joined:
            err = (r.stderr or r.stdout).strip()
            log.warning("couldn't join %r: %s", ssid, err)
            lines = [l for l in err.splitlines() if not l.startswith("Hint:")]
            if re.search(r"secrets were required|psk|password", err, re.I):
                message = "the password was refused"
            elif re.search(r"No network with SSID|not found|could not be found|not available|Timeout", err, re.I):
                message = "it isn't in range, or didn't answer"
            else:
                message = re.sub(r"^Error:\s*", "", lines[-1] if lines else "couldn't connect")
    s = status()
    s["joined"], s["message"] = joined, message
    return s


def remove(ssid):
    """Forget a saved network (disconnecting if it's the one in use)."""
    found = [uuid for uuid, saved, _ in _saved() if saved == ssid]
    if not found:
        raise WifiError(f"no saved network called {ssid!r}")
    for uuid in found:
        _nmcli("connection", "delete", uuid)
    log.warning("Wi-Fi network %r removed from the web app", ssid)
    return status()
