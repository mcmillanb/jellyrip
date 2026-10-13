# Sourced by the setup scripts: sets OWNER, the Linux user discrip runs as and
# that owns the library (and the Samba login). In order of preference:
#   1. DISCRIP_USER, e.g.  sudo DISCRIP_USER=alice bash setup/01-base.sh
#   2. the owner of /srv/media, once 01-base.sh has created it (so re-runs and
#      later scripts keep the first choice)
#   3. the user who ran sudo
if [ -n "${DISCRIP_USER:-}" ]; then
    OWNER=$DISCRIP_USER
elif [ -d /srv/media ] && [ "$(stat -c %U /srv/media)" != root ]; then
    OWNER=$(stat -c %U /srv/media)
elif [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    OWNER=$SUDO_USER
else
    echo "Can't tell which user discrip should run as: run with sudo from that user's"
    echo "login, or set it:  sudo DISCRIP_USER=<user> bash $0"
    exit 1
fi
id "$OWNER" >/dev/null 2>&1 || { echo "user $OWNER does not exist"; exit 1; }
echo "discrip user: $OWNER"
