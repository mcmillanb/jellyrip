#!/bin/bash
# Build step 0: turn the USB SSD (XS1000) into the data disk mounted at /srv.
# The OS runs from the SD card; this ERASES EVERYTHING on the given disk.
# Run as: sudo bash setup/00-data-disk.sh /dev/sdX
set -euo pipefail

DEV=${1:?usage: sudo bash setup/00-data-disk.sh /dev/sdX}
LABEL=discrip-data

[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
ROOTDISK=/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
[ "$ROOTDISK" != "$DEV" ] || { echo "$DEV holds the running root filesystem"; exit 1; }
if lsblk -lnpo MOUNTPOINT "$DEV" | grep -q .; then
    echo "$DEV has mounted partitions; unmount them first"; exit 1
fi

lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL "$DEV"
read -rp "Type $DEV to erase it: " answer
[ "$answer" = "$DEV" ] || { echo "Aborted."; exit 1; }

echo "== partition and format"
wipefs -a "$DEV"
echo 'type=linux' | sfdisk --label gpt "$DEV"
udevadm settle
PART=$(lsblk -lnpo NAME "$DEV" | sed -n 2p)
mkfs.ext4 -F -L "$LABEL" -m 1 "$PART"
UUID=$(blkid -s UUID -o value "$PART")

echo "== fstab"
sed -i '\|[[:space:]]/srv[[:space:]]|d; \|[[:space:]]/var/lib/discrip[[:space:]]|d' /etc/fstab
cat >> /etc/fstab <<EOF
UUID=$UUID /srv ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2
/srv/discrip /var/lib/discrip none bind,nofail,x-systemd.requires-mounts-for=/srv 0 0
EOF
systemctl daemon-reload

echo "== mount"
mkdir -p /srv /var/lib/discrip
mount /srv
mkdir -p /srv/discrip
mount /var/lib/discrip
findmnt /srv
findmnt /var/lib/discrip

echo
echo "Done. Next: sudo bash setup/01-base.sh"
