#!/bin/bash
# Build step 9: Jellyfin in Docker (spec section 10).
# Run as: sudo bash setup/09-jellyfin.sh [image-tag]   (safe to re-run; recreates the
# container, keeps config). Web UI: http://jellyfin.local:8096
set -euo pipefail

OWNER=jellyfin
TAG=${1:-12.2}
IMAGE=jellyfin/jellyfin:$TAG

mountpoint -q /srv || { echo "/srv is not mounted: run setup/00-data-disk.sh first"; exit 1; }
uid=$(id -u "$OWNER"); gid=$(id -g "$OWNER")

mkdir -p /srv/jellyfin/config /srv/jellyfin/cache
chown -R "$OWNER:$OWNER" /srv/jellyfin

echo "== pull $IMAGE"
docker pull "$IMAGE"

echo "== (re)create container"
docker rm -f jellyfin 2>/dev/null || true
# Host networking so LAN clients can discover the server (UDP 7359).
# Libraries are read-only: only discrip and Samba write to /srv/media.
docker run -d --name jellyfin \
    --restart unless-stopped \
    --network host \
    --user "$uid:$gid" \
    -v /srv/jellyfin/config:/config \
    -v /srv/jellyfin/cache:/cache \
    -v /srv/media/Movies:/media/movies:ro \
    -v /srv/media/TV:/media/tv:ro \
    -v /srv/media/Music:/media/music:ro \
    "$IMAGE"

echo
echo "Done. Open http://$(hostname).local:8096 and run the setup wizard. Libraries:"
echo "  Movies -> /media/movies   Shows -> /media/tv   Music -> /media/music"
