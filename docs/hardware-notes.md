# Hardware notes

Findings from bring-up on 2026-10-13. The spec (`Jellyspec.md`) holds the decisions; this file holds the evidence behind them.

## Hardware

- Raspberry Pi 4 Model B Rev 1.4, 4 GB, official 3 A PSU. EEPROM `BOOT_ORDER=0xf41` (SD, then USB).
- DVD: Pioneer DVRTD11RS (slim, DVD-RW), in an enclosure with an Initio INIC-1618L USB-SATA bridge, `13fd:0840`, USB 2 only (480M). The drive is set to region 2 (4 region changes left). It has a separate micro-USB lead for extra power.
- SSD: Kingston XS1000, 1 TB, `0951:1780`. It only links at USB 2 (480M) with the `usb-storage` driver, with three different cables, both blue ports, and on a Mac too, so the fault is the drive (or every cable tried). It reads at about 40 MB/s, which is plenty.

## USB power: the crashes

Four crashes on 2026-10-13 had the same mechanism:

1. The DVD drive spun up (`blkid -p`, a disc insert, a MakeMKV scan), or a USB power lead was plugged in while the Pi was running.
2. The kernel logged `over-current change` on **every** Pi USB port at once (`usb2-port1..4`, `1-1-port1..4`). The Pi 4 has one current limiter shared by all ports.
3. The DVD bridge disconnected and the XS1000 was reset or disconnected. When the XS1000 held the root filesystem, ext4 aborted its journal and went read-only, and Claude Code died with SIGBUS.

`vcgencmd get_throttled` stayed `0x0` throughout, because this is the USB port limiter, not PSU under-voltage. Don't rely on it to detect this.

**Fix:** put the drive's aux micro-USB lead on a separate USB charger. Tested clean: 3 cold spin-ups, a full MakeMKV scan and rip of Mirrormask (17 min, peak 44.3 C), and a disc swap. There were no over-current events, resets or disconnects.

**Rule:** only plug or unplug USB power leads while the Pi is off. Hot-plugging the charger lead caused crash #4, probably from two 5 V supplies meeting on the drive's shared VBUS.

If it recurs, use a powered USB hub with backfeed protection for the DVD drive. The OS is on the SD card so that a USB event can't take the system down.

## DVD drive quirks

- **Stuck CSS AGIDs:** after an interrupted session, `REPORT KEY` returns sense 5/2C/00, and MakeMKV says "Failed to open disc" or "KEY NOT PRESENT". `bin/dvd-reset-auth` invalidates AGIDs 0-3 and fixes it. Run it before every `makemkvcon` call.
- **Harmless read errors:** sense 5/6F/03 near the end of CSS discs (scrambled sectors read without authentication) happens on every boot and every `blkid -p`. MakeMKV may report one "Read Error" in its decrypt pass and still save the title fine.
- **Slow scans:** discs with corrupt-IFO protection (Mirrormask) need a VOB scan, which takes 9-14 min before ripping starts. Normal discs take about 1 min.
- **Rip speed:** 5-9 MB/s (about 4-7x DVD speed). Full Monty title: 3.9 GB in 12m46s. Mirrormask: 4.5 GB in 8m11s.
- **Missing udev properties:** after a disc insert, `ID_CDROM_MEDIA` and `ID_FS_LABEL` were missing twice until `udevadm trigger --action=change /dev/sr0`. Kernel uevents do arrive (eject request, media change at tray close, another media change about 8 s later when ready), but the props aren't reliable. Hence the polling watcher (spec section 6).
- **Wedged bridge:** the drive's USB bridge can wedge (it shows up on USB but its SCSI probe never completes, so there's no `/dev/sr0`). A Pi reboot doesn't clear it while the drive has its own power: power-cycle the drive.
- **Eject:** a bare `CDROMEJECT` ioctl returns 0 but the drive refuses with sense 5/53/02 (medium removal prevented) and the tray stays shut. util-linux `eject` works; `lib/discrip.py` uses it and confirms the tray opened.
- **Spin-up time varies:** after closing the tray the drive reported "not ready" for 9-22 s before "ready", and `CDROM_DRIVE_STATUS` can drop back briefly just after, so dispatch waits for ready again.
- The kernel media poll is 2000 ms (`events_dfl_poll_msecs`), and the drive's USB `power/control` is `on` (no autosuspend).

## Software notes

- Debian 13 trixie: the docker CLI is the separate `docker-cli` package.
- MakeMKV 2.0 needs `libavcodec-dev` to build. On first run it uploads a drive firmware dump and checks for updates.
- Pi OS ships a volatile journal (`40-rpi-volatile-storage.conf`); `01-base.sh` makes it persistent.

## Moving the OS to the SD card (2026-10-13)

1. Shut down, insert the SD card (same user and hostname `jellyfin`), and power on. The Pi boots from SD first. On the Mac: `ssh-keygen -R jellyfin` and `ssh-keygen -R jellyfin.local`.
2. Check that `/` is on `mmcblk0p2` and that the old SSD partitions are not mounted.
3. Before erasing the SSD, copy anything wanted from its old root. Do this read-only: `sudo mount -o ro /dev/sda2 /mnt` (check the device with `lsblk` first). Copy the Claude Code memory: `cp -a /mnt/home/jellyfin/.claude/projects/-home-jellyfin-jellyrip ~/.claude/projects/`. Then `sudo umount /mnt`.
4. `git clone https://github.com/mcmillanb/jellyrip.git ~/jellyrip`
5. `sudo bash setup/00-data-disk.sh /dev/sda` (erases the SSD and mounts it at `/srv`), then `sudo bash setup/01-base.sh`, `sudo bash setup/02-makemkv.sh`, and reboot.
