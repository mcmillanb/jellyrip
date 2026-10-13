#!/bin/bash
# Build step 2: MakeMKV (headless makemkvcon) from the official Linux release.
# Run as: sudo bash setup/02-makemkv.sh [version]   (safe to re-run; rebuilds)
# Upstream instructions: https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224
set -euo pipefail

VER=${1:-2.0.0}
BUILD=/usr/local/src/makemkv-$VER

echo "== build deps (no Qt GUI; libavcodec is required since 2.0)"
apt-get update
apt-get install -y --no-install-recommends \
    build-essential pkg-config libc6-dev libssl-dev libexpat1-dev zlib1g-dev libavcodec-dev curl

mkdir -p "$BUILD"
cd "$BUILD"
for pkg in oss bin; do
    [ -f makemkv-$pkg-$VER.tar.gz ] || curl -fLO "https://www.makemkv.com/download/makemkv-$pkg-$VER.tar.gz"
    rm -rf makemkv-$pkg-$VER
    tar xzf makemkv-$pkg-$VER.tar.gz
done

echo "== makemkv-oss"
cd "$BUILD/makemkv-oss-$VER"
./configure --disable-gui
make -j"$(nproc)"
make install

echo "== makemkv-bin"
cd "$BUILD/makemkv-bin-$VER"
# The Makefile normally asks interactively to accept the EULA (src/eula_en_linux.txt).
mkdir -p tmp && echo accepted > tmp/eula_accepted
make
make install

ldconfig
echo
makemkvcon -r info 2>&1 | grep -m1 -E 'MSG:1005' || true
echo "Done."
