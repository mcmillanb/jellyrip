# discrip setup guide

How to build a discrip box from a blank Raspberry Pi: the OS, the data disk, MakeMKV, discrip itself, Jellyfin, Samba and the Jellyfin header pill. It also covers how much depends on this particular hardware, and what to change for a different disk or DVD drive.

`Jellyspec.md` is the design and `docs/hardware-notes.md` has the evidence behind the hardware rules. This guide is the how-to.

## What you need

| Part | Used here | Notes |
|---|---|---|
| Raspberry Pi | Pi 4 Model B, 4 GB | 4 GB is comfortable with Jellyfin. A Pi 5 should work but hasn't been tried. |
| Power supply | Official 3 A USB-C | Use the official supply; `vcgencmd get_throttled` must stay `0x0`. |
| SD card | 32 GB+ | Holds the OS only. Library and state live on the data disk. |
| Data disk | Kingston XS1000 1 TB SSD (USB) | Any USB SSD or HDD works. See [Data disk](#4-data-disk). |
| DVD drive | Pioneer DVRTD11RS slim drive in an INIC-1618L USB enclosure | Most USB DVD drives should work. See [Other DVD drives](#other-dvd-drives). |
| Spare USB charger | Any phone charger | Powers the DVD drive's auxiliary lead. See the power rules below. |

### Power rules (read first)

The Pi 4 has **one** current limiter shared by all four USB ports. A DVD drive spinning up can trip it, which resets **every** USB device at once, the data disk included. That caused four crashes during bring-up (`docs/hardware-notes.md`). So:

1. **Give the DVD drive its own power.** If it has an auxiliary USB power lead, plug that into a separate USB charger, not into the Pi. If it has no aux lead, put it on a **powered USB hub with backfeed protection**.
2. **A bus-powered spinning hard disk needs a powered hub too.** An SSD is usually fine on the Pi's own power.
3. **Never plug or unplug USB power leads while the Pi is running.** Hot-plugging the charger lead caused one of the crashes. Shut down (`sudo poweroff`), change the cables, then power on.
4. **Keep the OS on the SD card, not on the USB disk**, so a USB reset can't take the root filesystem down.

## Setup from a clean Pi

Steps 1-9 take about an hour of attention. Building MakeMKV (step 6) takes a while unattended.

### 1. Flash the SD card

Use Raspberry Pi Imager and choose **Raspberry Pi OS Lite (64-bit)**. It must be the **Debian 13 "trixie"** based release: the scripts need Python 3.11+ and the trixie package names (`docker-cli`).

In Imager's OS customisation settings, set:

- **Hostname:** `jellyfin`. Any name works; the web app is then at `http://<hostname>.local:8080`.
- **Username:** any name; `jellyfin` is used in the examples below. discrip runs as the user who runs the setup scripts: see [Which user](#which-user).
- **SSH:** enabled, ideally with your public key.
- **Wi-Fi:** only if you won't use Ethernet. Ethernet is better for streaming.
- **Locale and time zone:** your real time zone. The overnight encoding window uses local time.

Boot the Pi **with the data disk and DVD drive already connected and powered** (power rules above).

### 2. First login and updates

```bash
ssh jellyfin@jellyfin.local
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
sudo reboot
```

### 3. Get the code

```bash
git clone https://github.com/mcmillanb/jellyrip.git ~/jellyrip
cd ~/jellyrip
```

All setup scripts are run from here with `sudo bash setup/<script>`. Each one is safe to re-run.

### 4. Data disk

`setup/00-data-disk.sh` **erases** the disk you name. It then:

1. creates one ext4 partition labelled `discrip-data`;
2. mounts it at `/srv` by UUID (with `nofail`, so a missing disk doesn't stop boot);
3. bind-mounts `/srv/discrip` onto `/var/lib/discrip`, so discrip's state lives on the disk and not on the SD card.

Find the disk first:

```bash
lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINT
```

Pick the `usb` disk with the right size and model, usually `/dev/sda`. **Don't** pick `mmcblk0`, which is the SD card. The script refuses the disk the OS runs from, refuses a disk with mounted partitions, and asks you to type the device name before erasing.

```bash
sudo bash setup/00-data-disk.sh /dev/sda
```

**Using a different disk:**

- **Any size USB SSD or HDD works the same way.** Rough space needs:
  - a DVD film is 4-8 GB as ripped and about 1-1.5 GB after overnight H.264 encoding;
  - a TV episode is about 1 GB ripped and 250-400 MB after encoding;
  - a CD album is 250-450 MB as FLAC.

  Rips stop with an error below `[dispatch] min_free_gb` (25 GB).
- **Spinning HDDs** need a powered hub (power rules) and are slower, which only matters for encoding.
- **Keeping a disk's existing contents:** don't run `00-data-disk.sh`. Instead:
  1. make sure the filesystem is **ext4**. exFAT and NTFS can't hold Linux permissions, so Samba and Jellyfin ownership won't work;
  2. add the two `/etc/fstab` lines the script writes, with your disk's UUID from `sudo blkid`;
  3. run `sudo mkdir -p /srv/discrip && sudo mount -a`.
- **Moving to a bigger disk later:**
  1. stop the services: `sudo systemctl stop discrip-watcher discrip-web discrip-encode && sudo docker stop jellyfin`;
  2. copy `/srv` across with `rsync -aHAX`;
  3. point the fstab UUID at the new disk.

  Library paths don't change, so Jellyfin doesn't notice.

### 5. Base packages and layout

```bash
sudo bash setup/01-base.sh
sudo reboot
```

This installs:

- abcde, cdparanoia, FLAC and the MusicBrainz tools;
- Flask and ffmpeg;
- Samba;
- Docker, with its data on `/srv/docker`.

It also:

- creates the library layout under `/srv/media` (`Movies`, `TV`, `Music`, `_inbox`, `_unknown_music`, `_work`);
- makes the systemd journal persistent;
- checks that the DVD drive's USB autosuspend is off;
- updates the bootloader EEPROM.

The reboot applies the EEPROM update and the `docker` group membership.

If it prints `WARNING: autosuspend is enabled for the DVD drive`, the drive will go to sleep and the watcher will miss discs. Check `/sys/bus/usb/devices/<port>/power/control`; it should say `on`.

### 6. MakeMKV

```bash
sudo bash setup/02-makemkv.sh          # or: sudo bash setup/02-makemkv.sh 2.0.1
```

This builds headless `makemkvcon` from MakeMKV's official Linux source release and accepts the EULA. The default version is `2.0.0`. If makemkv.com has moved on and the download 404s, pass the current version number.

**Ripping DVDs with MakeMKV is free and needs no key.** Only Blu-ray needs the beta key, and discrip doesn't rip Blu-ray.

Check that the drive works before going further. Put a DVD in and run:

```bash
makemkvcon -r info disc:0 | grep -E '^(DRV|CINFO:2|TCOUNT)'
```

You should see the drive and the disc's title. On its first run MakeMKV uploads a firmware dump of the drive to its servers and checks for updates; that's normal.

### 7. discrip

```bash
sudo bash setup/03-discrip.sh
```

This:

1. copies `bin/`, `lib/` and `web/` to `/opt/discrip`;
2. installs the config at `/opt/discrip/discrip.toml`, **only if it doesn't exist yet**, so your edits survive re-installs;
3. generates the web app's token;
4. installs the four systemd units and the polkit rule, then starts the services:
   - `discrip-watcher`: watches the drive;
   - `discrip-rip`: one rip, started by the watcher;
   - `discrip-web`: the web app;
   - `discrip-encode`: overnight encoding.

At the end it prints:

```
Web app: http://jellyfin.local:8080/
Open this once on each phone/computer to save the token there:
  http://jellyfin.local:8080/#token=…
```

Open the `#token=` link once on each phone and computer. It saves the token in that browser, which the buttons need; viewing works without it. On an iPhone, Share → Add to Home Screen makes it an app.

**Set the drive device.** The config ships with this build's Pioneer drive first and `/dev/sr0` as the fallback. A different drive will use `/dev/sr0`, which works, but a stable path is better because `/dev/sr0` can become `sr1` after a USB reset. Find your drive's path and put it first in `/opt/discrip/discrip.toml`:

```bash
ls -l /dev/disk/by-id/ | grep sr0
sudo nano /opt/discrip/discrip.toml        # [drive] device = ["/dev/disk/by-id/usb-…", "/dev/sr0"]
sudo systemctl restart discrip-watcher
```

### 8. Jellyfin

```bash
sudo bash setup/09-jellyfin.sh            # optional image tag, default 12.2
```

This runs the official `jellyfin/jellyfin` image in Docker. It uses host networking so apps can discover it, and runs as your user. Config is on the data disk, and the three libraries are mounted **read-only**: only discrip and Samba write to them.

Open `http://jellyfin.local:8096` and run the setup wizard. Add the libraries:

| Library type | Folder |
|---|---|
| Movies | `/media/movies` |
| Shows | `/media/tv` |
| Music | `/media/music` |

Don't add `_inbox`, `_work` or `_unknown_music`.

**Give discrip an API key** so it can tell Jellyfin to rescan after each rip. Without one, new rips can take a long time to appear.

1. In Jellyfin, go to Dashboard → API Keys → `+`, and name the key `discrip`.
2. Put the key in the config. The file is mode 600; don't paste the key anywhere else.
   ```bash
   sudo nano /opt/discrip/discrip.toml        # [jellyfin] api_key = "…"
   sudo systemctl restart discrip-watcher discrip-web discrip-encode
   ```

The same key lets discrip:

- show pop-ups in open Jellyfin apps when a disc needs an answer or a rip fails;
- pause overnight encoding while anyone is watching.

**Optional:** to let people download files from a browser, enable "Allow media downloads" on each user (Dashboard → Users).

### 9. Samba (network share)

```bash
sudo bash setup/09-samba.sh
sudo smbpasswd -a jellyfin                 # choose the share password
```

The share is `smb://jellyfin.local/media`, covering the whole `/srv/media`. Use it to rename things, to tag `_unknown_music` albums by hand, or to copy files in.

On a Mac: Finder → Go → Connect to Server.

It accepts clients from any private address range (`10.x`, `172.16-31.x`, `192.168.x`, and IPv6 link-local and ULA), so it works on any home network without changes. It never accepts clients from the internet.

### 10. The Jellyfin header pill

The pill is a small status badge in Jellyfin's top bar. For example, "💿 Ripping The Full Monty 42%" or "💿 2 discs need you". Tapping it opens the web app.

The pill needs a third-party plugin, **JavaScript Injector** by n00bcodr, to load `pill.js` into Jellyfin's web client.

1. In Jellyfin, open Dashboard → Plugins → Repositories (in some versions: Catalog → the settings icon). Add a repository:
   - Name: `n00bcodr`
   - URL: `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/12/manifest.json`

   That URL is for Jellyfin 12. For another Jellyfin version, check the plugin's GitHub page for the matching manifest.
2. Open Catalog, find **JavaScript Injector** and install it. This build uses version 4.0.0.0.
3. Restart Jellyfin: `sudo docker restart jellyfin`.
4. Open Dashboard → Plugins → JavaScript Injector and add a script with this code. Enable it, and save.
   ```js
   (function(){var s=document.createElement('script');
     s.src=location.protocol+'//'+location.hostname+':8080/pill.js';document.head.appendChild(s);})();
   ```
5. Reload Jellyfin in the browser once.

After that, the pill updates itself whenever discrip is re-installed, with no reload needed.

How the pill behaves:

- It only shows when there's something to see: a rip, questions waiting, a failed rip in the last 6 h, a missing drive, or under-voltage.
- It works in the Jellyfin web client and in apps that wrap it (iOS and Android). Native TV apps don't run injected scripts, so they get the pop-up messages only.
- If you later put Jellyfin behind **HTTPS** (a reverse proxy), the browser will block the pill, because it loads from plain `http://…:8080`. Serve the web app through the same proxy and change the snippet's URL.
- The pill finds Jellyfin's header by its layout, which can change between Jellyfin versions. If it ends up floating at the top centre instead of sitting in the header, `journalctl -u discrip-web | grep pill=` shows what it found.

### 11. Check it all works

```bash
journalctl -fu discrip-watcher -u discrip-rip       # watch a rip
```

1. **A film DVD:** it should rip on its own, eject, and appear in `Movies/<Name>/` and in Jellyfin. Expect about 13-17 min, plus up to 15 min of scanning on copy-protected discs.
2. **A CD:** it should land in `Music/<Artist>/<Album>/` as tagged FLAC, in about 14 min for an album.
3. **A TV season disc:** episodes are filed to `TV/<Show>/Season NN/`. If discrip is unsure of the numbering, it waits for you to confirm in the web app.
4. **Encoding:** the web app's *Overnight encoding* card should show the queue. The first encode starts at 01:00. *CPU temperature* charts the night.

## Day to day

- **Updating:** `cd ~/jellyrip && git pull && sudo bash setup/03-discrip.sh`. Your config, token and state are kept. New config options get their defaults from `lib/discrip.py`. `config/discrip.toml` documents them, so copy across any you want to change.
- **Config:** the live file is `/opt/discrip/discrip.toml`. After editing it, run `sudo systemctl restart discrip-watcher discrip-web discrip-encode`. Rips pick up changes at their next start. Encoding settings are better changed in the web app.
- **Logs:**
  - `journalctl -u discrip-watcher -u discrip-rip -u discrip-web -u discrip-encode`;
  - each rip's full log is in `/var/lib/discrip/logs/` and in the web app's history.
- **Answering questions without the web app:** `/opt/discrip/bin/discrip-pending` (run `--help` for usage).
- **What to back up:**
  - `/opt/discrip/discrip.toml`: on the SD card;
  - `/var/lib/discrip`: known discs, library records, TV numbering;
  - `/srv/jellyfin/config`: Jellyfin's users and watch history;
  - the library itself, if you can.

## How adaptable is it

### Other DVD drives

Nothing in discrip is specific to the Pioneer drive except the device path in the config. It only uses things that every Linux optical drive supports:

| Part | How | Drive-specific? |
|---|---|---|
| Disc detection | Polls `CDROM_DRIVE_STATUS` every 2 s | No: standard Linux ioctl |
| Media type | udev's `cdrom_id` and `blkid` | No |
| Ripping | MakeMKV, addressed by device path (`dev:/dev/sr0`) | MakeMKV supports nearly all DVD drives |
| CSS reset | `dvd-reset-auth`, a standard SCSI `REPORT KEY` / INVALIDATE AGID | No: an MMC command every DVD drive implements. Harmless on drives that never get stuck. |
| Eject | util-linux `eject`, then checks the tray opened | No. The raw ioctl failed on this drive, so the more compatible route is used. |
| CD ripping | abcde with cdparanoia | No |

What to check with a different drive:

- **The device path** (step 7).
- **Power:** does it need its own power? Slim drives often do over USB 2. Watch `dmesg -w` while a disc spins up; `over-current` lines mean it needs its own power.
- **Region:** DVD drives are region-locked (RPC-2). Set the region to match your discs. MakeMKV can often read other regions anyway, but this hasn't been tested here.
- **Slot-loading drives** should work, since the watcher only needs the "no disc" → "ready" change, but haven't been tried.
- **One drive at a time:** discrip uses the first configured device that exists, and rips one disc at a time.
- **A Blu-ray drive** works for DVDs and CDs. Blu-ray discs are detected and refused with "Blu-ray not supported". Adding them would mean the MakeMKV beta key, about 25-45 GB per film, and encodes far too slow for a Pi.
- **Slow drives:** spin-up time varies by drive. `[drive] ready_timeout` (60 s) is how long a rip waits for "ready".

### Discs

| Disc | Handled |
|---|---|
| DVD-Video film, PAL or NTSC, CSS or not | Yes: ripped to MKV, named from the disc label. Junk labels (e.g. `DVD_VIDEO`) go to `_inbox` and ask for a name. |
| DVD with a TV season | Yes: episodes found by length. TVmaze supplies episode names (on by default, `[video] tv_lookup`). Numbering continues across discs. |
| Discs with decoy playlists (some Disney and Sony titles) | Asks you to pick the titles instead of guessing |
| Audio CD | Yes: FLAC, tagged from MusicBrainz. With no match, it goes to `_unknown_music`. |
| Enhanced / mixed-mode CD | Audio tracks only |
| Data CD or DVD, Blu-ray | Refused with a message and ejected |

The film and episode detection thresholds are in `[video]` in the config. If a type of disc keeps being misjudged, they can be tuned there.

### Other computers

- **Pi 5:** should work unchanged (64-bit, same OS). It has more USB power, but give the DVD drive its own power anyway. Not tested.
- **Pi 4 with 2 GB:** discrip itself is small, but Jellyfin and an ffmpeg encode together would be tight. Not recommended.
- **Pi 3 or older:** too slow, and no 64-bit Jellyfin worth having.
- **A non-Pi Debian 13 machine (x86 or arm64):**
  - `01-base.sh` skips the bootloader update when it isn't on a Pi 4 or 5;
  - the temperature and throttle readings use Pi-specific tools: temperature falls back to sysfs, and the throttle flags stay empty;
  - everything else, including the Docker image and the MakeMKV build, works on x86 too.

### Which user

discrip runs as one Linux user, which also owns the library and is the Samba login. The setup scripts choose it in this order (`setup/owner.sh`):

1. `DISCRIP_USER`, if set: `sudo DISCRIP_USER=alice bash setup/01-base.sh`;
2. the owner of `/srv/media`, once `01-base.sh` has created it, so every later script and re-run keeps the first choice;
3. the user who ran `sudo`.

`01-base.sh` adds that user to the `cdrom` group (for the drive) and the `docker` group. The systemd units, polkit rule, Jellyfin container and Samba share all take the user from the scripts.

## Known gaps

- **Thermal test:** the build has not been thermally tested in its final case (spec step 11). Use the temperature chart during a rip plus a stream, and during an overnight encode.
- **Untested paths:** not yet seen on real discs:
  - the junk-label `_inbox` path;
  - a CD with no MusicBrainz match;
  - the title picker for discs with decoy playlists.
