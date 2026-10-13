#!/bin/bash
# Build step 1: base packages, directory layout, bootloader update.
# Run as: sudo bash setup/01-base.sh   (safe to re-run)
set -euo pipefail

OWNER=jellyfin

echo "== apt packages"
apt-get update
apt-get install -y --no-install-recommends \
    git jq \
    abcde cd-discid cdparanoia flac \
    libmusicbrainz-discid-perl libwebservice-musicbrainz-perl \
    python3-flask \
    samba \
    docker.io docker-cli

echo "== docker group"
usermod -aG docker "$OWNER"

echo "== directory layout"
mkdir -p /srv/media/{_work,_inbox,Movies,TV,Music,_unknown_music}
mkdir -p /opt/discrip /var/lib/discrip/logs
chown -R "$OWNER:$OWNER" /srv/media /opt/discrip /var/lib/discrip

echo "== persistent journal (Pi OS defaults to volatile)"
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nSyncIntervalSec=15s\n' > /etc/systemd/journald.conf.d/99-persistent.conf
systemctl restart systemd-journald

echo "== bootloader EEPROM"
rpi-eeprom-update -a

echo
echo "Done. Reboot to apply the EEPROM update and docker group membership."
