#!/bin/bash
# Build step 3+: install discrip code, config, systemd units and polkit rule.
# Run as: sudo bash setup/03-discrip.sh   (safe to re-run; config is kept if present)
set -euo pipefail

OWNER=jellyfin
REPO=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/discrip

mountpoint -q /srv || { echo "/srv is not mounted: run setup/00-data-disk.sh first"; exit 1; }

echo "== code -> $DEST"
mkdir -p "$DEST"
rsync -a --delete "$REPO/bin/" "$DEST/bin/"
rsync -a --delete --exclude __pycache__ "$REPO/lib/" "$DEST/lib/"
rsync -a --delete "$REPO/web/" "$DEST/web/"
if [ -e "$DEST/discrip.toml" ]; then
    echo "keeping existing $DEST/discrip.toml (new default: $REPO/config/discrip.toml)"
else
    cp "$REPO/config/discrip.toml" "$DEST/discrip.toml"
fi
cp "$REPO/config/abcde.conf" "$DEST/abcde.conf"  # not user config: always updated
chown -R "$OWNER:$OWNER" "$DEST" /var/lib/discrip
# The web app's shared token: generated once, kept across re-installs.
if ! grep -q '^\[web\]' "$DEST/discrip.toml"; then
    printf '\n[web]\nport = 8080\ntoken = ""\n' >> "$DEST/discrip.toml"
fi
if grep -q '^token = ""' "$DEST/discrip.toml"; then
    sed -i "s/^token = \"\"/token = \"$(openssl rand -hex 16)\"/" "$DEST/discrip.toml"
    echo "generated a web app token"
fi
chmod 600 "$DEST/discrip.toml"  # may hold the Jellyfin API key

echo "== /run/discrip"
echo "d /run/discrip 0755 $OWNER $OWNER -" > /etc/tmpfiles.d/discrip.conf
systemd-tmpfiles --create /etc/tmpfiles.d/discrip.conf

echo "== systemd units and polkit rule"
for unit in discrip-watcher.service discrip-rip.service discrip-web.service; do
    sed "s/@OWNER@/$OWNER/g" "$REPO/systemd/$unit" > "/etc/systemd/system/$unit"
done
sed "s/@OWNER@/$OWNER/g" "$REPO/systemd/50-discrip.rules" > /etc/polkit-1/rules.d/50-discrip.rules
systemctl daemon-reload
systemctl enable discrip-watcher.service discrip-web.service
systemctl restart discrip-watcher.service discrip-web.service

token=$(sed -n 's/^token = "\(.*\)"/\1/p' "$DEST/discrip.toml" | head -1)
echo
echo "Web app: http://$(hostname).local:8080/"
echo "Open this once on each phone/computer to save the token there:"
echo "  http://$(hostname).local:8080/#token=$token"

echo "Logs: journalctl -fu discrip-watcher -u discrip-rip -u discrip-web"
