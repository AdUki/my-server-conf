# my-server-conf

Provisioning for the Raspberry Pi home server (`./setup.sh`, idempotent, run as `pi`).
`CLAUDE.md` describes the layout; this file is the runbook for the two things that
matter when the OS disk is gone.

## Disk roles

| Label    | Device     | Holds                                                        |
|----------|------------|--------------------------------------------------------------|
| `rootfs` | NVMe       | OS, `~/my-server-conf`, Immich postgres + thumbs (fast cache) |
| `data`   | HDD, `/media/data` | media, Immich library (originals, encoded video, nightly DB dumps), Docker and containerd data |
| `backup` | HDD, `/media/backup`, mounted on demand | Borg archives of the Immich library + weekly `pg_dumpall` |

Only the NVMe is treated as disposable. Everything on it that is not a cache
is either in git, in `config.local.conf` / `immich/.env` (keep copies!), or is
rebuilt by `setup.sh`.

## Rebuilding after losing the NVMe

1. Flash Raspberry Pi OS Lite (64-bit), user `pi`, ssh enabled. Attach both HDDs.
2. `git clone https://github.com/AdUki/my-server-conf ~/my-server-conf`
3. Put back `config.local.conf` and `immich/.env` (same `DB_PASSWORD` as the dumps
   were made with; it is only needed for `pg_dumpall` files, Immich's own dumps
   work with any password).
4. Optional salvage from the old disk, if readable: `/var/lib/immich-storage/thumbs`
   (saves hours of thumbnail generation), `/var/lib/tailscale` (keeps the node
   identity), `/var/lib/samba/private` (Samba passwords), `/var/lib/docker/volumes`
   (Grafana/Prometheus history, Immich ML model cache), `~/.kodi`, crontabs.
5. `cd ~/my-server-conf && ./setup.sh`. It mounts the disks, moves `/var/log` and
   Docker to the HDD, and — finding an empty postgres dir next to
   `/media/data/immich/library/backups` — restores the newest Immich dump before
   starting the stack.
6. Reboot. Then: `sudo tailscale up` if `tailscale status` says logged out, and in
   Immich run *Administration > Jobs > Generate Thumbnails > Missing* if thumbs
   were not salvaged.

## Keeping the NVMe quiet

`setup_low_write_rootfs` moves every constant writer off the root filesystem:
logs (journal, `/var/log`, container logs) live in RAM and are gone after a
reboot, swap is 2 GB of zram, Docker and containerd write to the HDD.
Check what is left with

```
./rootfs-writes.sh 300
```

on an idle server. One-off writes (apt, config edits) are fine; anything that
appears every run belongs in RAM or on `/media/data`.
