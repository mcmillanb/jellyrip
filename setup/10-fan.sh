#!/bin/bash
# Temperature-controlled PWM fan on GPIO 18 (see fan/README.md).
# Run as: sudo bash setup/10-fan.sh   (safe to re-run; /etc/fanctl.conf is kept)
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
CONFIG=/boot/firmware/config.txt
BEGIN="# BEGIN fanctl (written by setup/10-fan.sh)"
END="# END fanctl"

echo "== files"
install -m 755 "$REPO/fan/fanctl" /usr/local/bin/fanctl
if [ -e /etc/fanctl.conf ]; then
    echo "keeping existing /etc/fanctl.conf (new default: $REPO/fan/fanctl.conf)"
else
    install -m 644 "$REPO/fan/fanctl.conf" /etc/fanctl.conf
fi
install -m 644 "$REPO/fan/fanctl.service" /etc/systemd/system/fanctl.service
install -D -m 755 "$REPO/fan/fanctl-halt" /usr/lib/systemd/system-shutdown/fanctl
install -D -m 644 "$REPO/fan/README.md" /usr/local/share/doc/fanctl/README.md

echo "== boot config ($CONFIG)"
# Refuse rather than override if something else already claims GPIO 18.
if sed "\\|^$BEGIN|,\\|^$END|d" "$CONFIG" | grep -v "^\s*#" | grep -Eq "i2s|hifiberry|pwm|audremap|gpio-fan|=18\b"; then
    echo "something else in $CONFIG may use GPIO 18 or PWM0; check it, nothing changed:"
    grep -nE "i2s|hifiberry|pwm|audremap|gpio-fan|=18\b" "$CONFIG"
    exit 1
fi
block="$BEGIN
# Hardware PWM0 on GPIO 18 (ALT5) for the fan, driven by fanctl.service through
# /sys/class/pwm. The fan's PWM line goes through an NPN transistor, so the
# signal is inverted: an idle or low pin means FULL speed (the fail-safe).
# (The power button, gpio-shutdown on GPIO 3, is in the discrip block.)
[all]
dtoverlay=pwm,pin=18,func=2
$END"
if grep -qF "$BEGIN" "$CONFIG"; then
    current=$(sed -n "\\|^$BEGIN|,\\|^$END|p" "$CONFIG")
else
    current=""
fi
if [ "$current" = "$block" ]; then
    echo "unchanged"
else
    [ -e "$CONFIG.fanctl-orig" ] || cp -p "$CONFIG" "$CONFIG.fanctl-orig"
    tmp="$CONFIG.fanctl-tmp"
    sed "\\|^$BEGIN|,\\|^$END|d" "$CONFIG" > "$tmp"
    printf '\n%s\n' "$block" >> "$tmp"
    mv "$tmp" "$CONFIG"
    echo "updated (original kept as config.txt.fanctl-orig)"
fi

# Load the overlay now too, so the fan is under control without a reboot.
if ! dtoverlay -l | grep -q " pwm "; then
    dtoverlay pwm pin=18 func=2 && echo "pwm overlay loaded for this boot"
fi

echo "== service"
systemctl daemon-reload
systemctl enable fanctl.service
systemctl restart fanctl.service
sleep 3
/usr/local/bin/fanctl status
