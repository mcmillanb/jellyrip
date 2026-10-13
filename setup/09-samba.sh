#!/bin/bash
# Build step 9: Samba share of /srv/media (spec section 10).
# Run as: sudo bash setup/09-samba.sh   (safe to re-run; replaces smb.conf, the
# original is kept as smb.conf.dist). Then set the share password once:
#   sudo smbpasswd -a jellyfin
set -euo pipefail

OWNER=jellyfin
CONF=/etc/samba/smb.conf

mountpoint -q /srv || { echo "/srv is not mounted: run setup/00-data-disk.sh first"; exit 1; }
[ -e "$CONF.dist" ] || cp "$CONF" "$CONF.dist"

cat > "$CONF" <<EOF
# Managed by jellyrip setup/09-samba.sh; Debian's original is smb.conf.dist.
[global]
   workgroup = WORKGROUP
   server string = %h
   server role = standalone server
   log file = /var/log/samba/log.%m
   max log size = 1000
   logging = file
   panic action = /usr/share/samba/panic-action %d

   # LAN only, authenticated only, no SMB1.
   hosts allow = 127. 192.168.
   map to guest = never
   server min protocol = SMB2_10
   # Setting a Samba password must not change the Linux login password.
   unix password sync = no

   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

   # macOS Finder: keep its metadata in xattrs instead of ._ files.
   vfs objects = catia fruit streams_xattr
   fruit:metadata = stream
   fruit:model = MacSamba
   fruit:posix_rename = yes
   fruit:veto_appledouble = no
   fruit:nfs_aces = no
   fruit:wipe_intentionally_left_blank_rfork = yes
   fruit:delete_empty_adfiles = yes

[media]
   comment = discrip library (Movies, TV, Music, _inbox, _unknown_music)
   path = /srv/media
   valid users = $OWNER
   read only = no
   # Everything written over the share belongs to $OWNER, like the rips.
   force user = $OWNER
   force group = $OWNER
   create mask = 0664
   directory mask = 0775
EOF

testparm -s "$CONF" >/dev/null
systemctl restart smbd nmbd

if pdbedit -L 2>/dev/null | grep -q "^$OWNER:"; then
    echo "Samba user $OWNER exists."
else
    echo "Now set the share password:  sudo smbpasswd -a $OWNER"
fi
echo "Connect from a Mac: Finder > Go > Connect to Server > smb://$(hostname).local/media"
