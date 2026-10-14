#!/bin/bash
# Build step 1: base packages, directory layout, bootloader update.
# Run as: sudo bash setup/01-base.sh   (safe to re-run)
set -euo pipefail

. "$(dirname "$0")/owner.sh"

mountpoint -q /srv || { echo "/srv is not mounted: run setup/00-data-disk.sh first"; exit 1; }
mountpoint -q /var/lib/discrip || { echo "/var/lib/discrip is not mounted: run setup/00-data-disk.sh first"; exit 1; }

echo "== apt packages"
apt-get update
apt-get install -y --no-install-recommends \
    git jq \
    abcde cd-discid cdparanoia flac \
    libmusicbrainz-discid-perl libwebservice-musicbrainz-perl \
    python3-flask \
    ffmpeg mkvtoolnix \
    samba \
    docker.io docker-cli

echo "== groups: docker, and cdrom for the drive"
usermod -aG docker,cdrom "$OWNER"
echo "== docker data-root on /srv"
mkdir -p /etc/docker /etc/systemd/system/docker.service.d /srv/docker
printf '{\n  "data-root": "/srv/docker"\n}\n' > /etc/docker/daemon.json
printf '[Unit]\nRequiresMountsFor=/srv\n' > /etc/systemd/system/docker.service.d/10-srv.conf
systemctl daemon-reload
systemctl restart docker

echo "== directory layout"
mkdir -p /srv/media/{_work,_inbox,Movies,TV,Music,_unknown_music}
mkdir -p /opt/discrip /var/lib/discrip/logs /srv/jellyfin
chown -R "$OWNER:$OWNER" /srv/media /srv/jellyfin /opt/discrip /var/lib/discrip

echo "== persistent journal (Pi OS defaults to volatile)"
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nSyncIntervalSec=15s\n' > /etc/systemd/journald.conf.d/99-persistent.conf
systemctl restart systemd-journald

echo "== DVD drive USB autosuspend (must stay 'on')"
if [ -e /sys/block/sr0 ]; then
    usbdev=$(readlink -f /sys/block/sr0/device | grep -oE '[0-9]+-[0-9.]+:[0-9.]+' | tail -1 | cut -d: -f1)
    ctl=$(cat "/sys/bus/usb/devices/$usbdev/power/control")
    echo "sr0 is USB $usbdev, power/control=$ctl"
    [ "$ctl" = on ] || echo "WARNING: autosuspend is enabled for the DVD drive; the watcher needs it off"
else
    echo "WARNING: no /dev/sr0; check the DVD drive"
fi

if command -v rpi-eeprom-update >/dev/null && grep -qa "Raspberry Pi [45]" /proc/device-tree/model 2>/dev/null; then
    echo "== bootloader EEPROM"
    rpi-eeprom-update -a
else
    echo "== not a Pi 4 or 5: no bootloader EEPROM to update"
fi

echo
echo "Done. Reboot to apply the EEPROM update and docker group membership."
