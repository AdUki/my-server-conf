# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Bash-based provisioning for a personal Raspberry Pi media/home server. There is no build step, no test suite, no language toolchain — every change is shell + config + docker-compose. The entry point is `setup.sh`; everything else is either invoked from it or runs independently as a Docker stack.

**Everything lives inside this repo.** There is no `~/apps/` directory on the Pi — Immich, NPM, and system-monitor all run out of their subdirectories here.

## Running the setup

```bash
./setup.sh             # Full idempotent provision. Must run as user `pi`, not root.
./setup.sh --dry-run   # Print every side-effecting command instead of running it.
```

Re-running `setup.sh` on a provisioned host is expected to produce **zero changes**: no systemd reloads, no docker container recreations, no file mtime updates outside of expected log/cache churn.

Key behaviors:
- Loads `config.conf` (non-secret defaults) followed by `config.local.conf` (gitignored secrets).
- If user groups (`lp`, `docker`) need to change, adds them and **exits with a reboot warning** — re-run after reboot to continue.
- `set_conf_file` returns 0 on actual change, 1 on no-op, so callers can skip service restarts.
- `set_conf_line_to_file` short-circuits when the exact line is already present.
- `jq_inplace` only writes when the filter changes the file's content.
- The Pi user is enforced via `id -un`; `--dry-run` bypasses the check.

## Configuration

- `config.conf` — committed, non-secret. Locales, apt extras, external-partition label, `SERVER_LAN_IP`, `DUCKDNS_DOMAIN`, `LETSENCRYPT_EMAIL`, `NPM_PROXY_HOSTS`.
- `config.local.conf` — **gitignored**. Holds `SAMBA_PI_PASSWORD`, `NPM_ADMIN_EMAIL`/`PASSWORD`, `DUCKDNS_TOKEN`, `IMMICH_DB_PASSWORD`, `IMMICH_ADMIN_EMAIL`/`PASSWORD`, `IMMICH_API_KEY`. Copy from `config.local.conf.example` and fill in before first run.

## Per-service helpers

The `immich/*.sh` helpers are self-locating (`SCRIPT_DIR="$(realpath "$(dirname "$0")")"`); run them from anywhere.

- `immich/start.sh` / `stop.sh` / `update.sh` — wrap `docker compose` for the Immich stack.
- `immich/setup-backup.sh` / `do-backup.sh` / `list-backups.sh` / `restore-backup.sh` — Borg-based backup of `UPLOAD_LOCATION` plus a `pg_dumpall` of `immich_postgres`. Excludes `thumbs/` and `encoded-video/`. Retention: 4 weekly + 3 monthly. `restore-backup.sh` mounts the archive at `/tmp/immich-mountpoint` and unmounts on exit via trap.
- `nginxproxymanager/seed.sh` — idempotent NPM bootstrap via its HTTP API: rotates the default admin to `NPM_ADMIN_*`, creates each `NPM_PROXY_HOSTS` entry, and requests a wildcard `*.${DUCKDNS_DOMAIN}.duckdns.org` cert via DNS-01. Invoked from `install_nginxproxymanager`.

## Architecture

Two layers stacked on a Raspberry Pi running as user `pi`:

1. **Host-installed services** (apt + systemd), configured by `setup.sh`:
   - CUPS (printing), Samba (`samba/smb.conf` → `/etc/samba/smb.conf`), Transmission daemon (patched to run as `pi`, not `debian-transmission`), Docker, Kodi (with `kodi/kodi.service` → `/etc/systemd/system/kodi.service`; sources.xml seeded from `kodi/sources.xml.template`; remote-control webserver on `:8081` and event server both enabled with no auth).
   - Tailscale is **out of scope** — it runs on the Pi but is set up via its browser auth flow; no install_* step in this repo.

`cleanup_legacy_stacks` is a one-time migration helper that stops/removes leftover Nextcloud + Rustdesk containers and moves `~/apps/immich-go` out of the (about-to-vanish) `~/apps/` directory. After the first successful run it's a no-op.

2. **Docker-composed services**, each in its own subdirectory of this repo:
   - `nginxproxymanager/` — reverse proxy / TLS termination, owns ports 80/443/81. `data/` and `letsencrypt/` are gitignored runtime state. The repo-root `index.html` is bind-mounted into the container as `/data/nginx/default_www/index.html` (read-only) and serves as the default landing page for un-routed hostnames.
   - `immich/` — photo management. `docker-compose.yml` is committed. `.env` is gitignored (generated from `.env.example` on first run; `DB_PASSWORD` must be preserved across deployments because the postgres data is encrypted with it).
   - `system-monitor/` — cloned from `github.com/AdUki/system-monitor` at install time (gitignored). Prometheus runs as uid `65534`, Grafana as `472`.

### Storage convention

Three optional disks, each configured by a `*_PARTITION_LABEL` in `config.conf`:

- **BOOT** — fast NVMe/SSD. Hosts Immich's `library/` and `postgres/`. If the BOOT label matches the root filesystem (Pi boots from NVMe), `IMMICH_STORAGE_ROOT` defaults to `/var/lib/immich-storage`. If BOOT is a separate partition, it's mounted at `/media/boot` and Immich lives at `/media/boot/immich`. Either way, override with `IMMICH_STORAGE_ROOT` in `config.conf`.
- **DATA** — bulk-media HDD. Mounted permanently at `/media/data` and becomes `STORAGE_ROOT`. If absent, `STORAGE_ROOT` falls back to `$HOME` (the test-VM / dev case).
- **BACKUP** — Borg-backup HDD. Added to `/etc/fstab` with `noauto,nofail` — **never mounted by `setup.sh`**. The immich backup helpers mount it on demand and unmount on exit so the disk can spin down between weekly backups.

Supported combinations:
1. BOOT + DATA + BACKUP (current target)
2. DATA + BACKUP (legacy Pi setup)
3. DATA only (no backups)
4. None (test VM — everything under `$HOME`)

`setup_storage_dirs()` creates the standard subdirs (`movies`, `pictures`, `rtorrent`, …) under `STORAGE_ROOT` and, if `STORAGE_ROOT` differs from `$HOME`, symlinks each into `$HOME` so Samba's `[homes]` share (with `wide links = yes`) exposes them.

The Immich compose file uses `${UPLOAD_LOCATION}` / `${DB_DATA_LOCATION}` / `${BACKUP_PATH}` from `immich/.env`, which `install_immich` rewrites to `$IMMICH_STORAGE_ROOT/library`, `$IMMICH_STORAGE_ROOT/postgres`, and `$BACKUP_MOUNTPOINT/immich/` respectively. The NPM compose file uses relative paths (`./data`, `./letsencrypt`), so it works regardless of where the repo lives.

The backup helpers share `immich/_backup-mount.sh` (sourced, not exec'd). It mounts `/media/backup` via the fstab entry, sets a trap to unmount on script exit (but only if it wasn't already mounted by the caller — we never tear down a mount we didn't create).

## Testing

A Multipass-based test harness lives in `test/`. See `test/README.md`. The short version:

```bash
sudo snap install multipass     # one-time
bash test/launch.sh             # boot Ubuntu 22.04 VM, mount repo
bash test/run.sh                # runs setup.sh twice; PASS iff second run is a no-op
bash test/reset.sh              # tear down VM
```

## Things to keep consistent when editing

- `setup.sh` runs top-to-bottom with `set -e`; new install steps go as their own `install_*` function and are appended to the call list at the bottom.
- Side-effecting commands (sudo, docker, systemctl, mv, rm) must be wrapped in `run` so `--dry-run` works.
- Paths inside `setup.sh` use `$STORAGE_ROOT` for storage and `$SCRIPT_DIR` for repo-relative locations. Never hardcode `/home/pi` or `$PWD`.
- The `.gitignore` excludes runtime state (`*.backup`, `*.tmp`, NPM data, the cloned `system-monitor/`, `config.local.conf`, `immich/.env`, `immich/library/`, `immich/postgres/`). Don't commit those back in.
- Secret values never go into committed files. `.example` templates must contain only blank `KEY=""` placeholders.
