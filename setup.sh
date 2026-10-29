#!/bin/bash
set -e

###############################################################################
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
	echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}
warn() {
	echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}
error() {
	echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
	exit 1
}

###############################################################################
usage() {
	cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Idempotent provisioning for the Raspberry Pi media/home server. Re-running
on an already-configured host should produce zero changes.

Options:
  -h, --help     Show this help and exit.
      --dry-run  Print every side-effecting command instead of running it.

Environment:
  SETUP_SKIP_NPM_SEED=1   Skip the nginx-proxy-manager API bootstrap
                          (admin rotation, proxy hosts, wildcard cert).
EOF
}

# --dry-run mode: echo side-effecting commands instead of running them.
DRY_RUN=0
for arg in "$@"; do
	case "$arg" in
		-h|--help) usage; exit 0 ;;
		--dry-run) DRY_RUN=1 ;;
		*) error "Unknown argument: $arg (try --help)" ;;
	esac
done

run() {
	if [[ $DRY_RUN -eq 1 ]]; then
		echo "DRY-RUN: $*"
	else
		"$@"
	fi
}

# Wrap apt-get so noninteractive env vars survive sudo. Stops needrestart's
# whiptail "Which services should be restarted?" dialog from hanging the
# script when run via `sudo -i` or a non-TTY pipe. The dpkg options keep a
# locally modified conffile instead of prompting: without a terminal the
# prompt reads EOF, dpkg marks the package half-configured and apt fails.
apt_get() {
	run sudo env \
		DEBIAN_FRONTEND=noninteractive \
		NEEDRESTART_MODE=a \
		NEEDRESTART_SUSPEND=1 \
		apt-get \
		-o Dpkg::Options::=--force-confdef \
		-o Dpkg::Options::=--force-confold \
		"$@"
}

# Install packages only if any are missing. Filters out fully-installed sets
# to keep --dry-run noise down. Pass package names as positional args.
apt_get_install() {
	local missing=() pkg
	for pkg in "$@"; do
		if ! dpkg-query -W -f='${Status}\n' "$pkg" 2>/dev/null | grep -q '^install ok installed$'; then
			missing+=("$pkg")
		fi
	done
	[ ${#missing[@]} -eq 0 ] && return 0
	apt_get install -y "${missing[@]}"
}

###############################################################################
# Escape a string so it can be used as a sed address (between // delimiters
# where we substitute | for /). Handles the BRE metacharacters and our
# alternate delimiter.
escape_sed_addr() {
	printf '%s' "$1" | sed 's/[][\\.*^$|]/\\&/g'
}

# Replace or append a single line in a config file. Idempotent:
#   - if the exact line is already present, do nothing (no sudo, no mtime touch);
#   - else if a line matching $id is present, replace it;
#   - else append the new line.
set_conf_line_to_file() {
	local file="$1"
	local id="$2"
	shift 2
	local line="$*"

	if [ -f "$file" ] && sudo grep -Fxq "$line" "$file"; then
		return 0
	fi

	local id_re
	id_re=$(escape_sed_addr "$id")

	if [ -f "$file" ] && sudo grep -q "$id" "$file"; then
		run sudo sed -i "\|$id_re|c\\
$line" "$file"
	else
		echo "$line" | run sudo tee -a "$file" > /dev/null
	fi
}

# Copy a source file to a target. Returns 0 if the target was changed, 1 if it
# was already up to date (caller can use the return code to decide whether to
# restart a service). Backup is created once per source basename.
set_conf_file() {
	local target_file="$1"
	local source_file="$2"

	if [ -f "$target_file" ] && sudo cmp -s "$source_file" "$target_file"; then
		return 1
	fi

	local backup_name="$(basename "$source_file").backup"
	if [ ! -f "$backup_name" ] && [ -f "$target_file" ]; then
		run sudo cp "$target_file" "$backup_name"
		log "Created backup: $backup_name"
	fi

	run sudo cp "$source_file" "$target_file"
	log "Copied $source_file to $target_file"
	return 0
}

# Apply a jq filter in place. Idempotent — only writes if the filter changes the
# content. Returns 0 if the file changed, 1 if it was already up to date.
jq_inplace() {
	local filter="$1"
	local target_file="$2"
	if [[ $DRY_RUN -eq 1 ]]; then
		echo "DRY-RUN: jq_inplace '$filter' $target_file"
		return 0
	fi
	local dir; dir=$(dirname "$target_file")
	local tmp_file; tmp_file=$(sudo mktemp "$dir/.$(basename "$target_file").XXXXXX")

	sudo jq "$filter" "$target_file" | sudo tee "$tmp_file" > /dev/null

	if sudo cmp -s "$tmp_file" "$target_file"; then
		sudo rm -f "$tmp_file"
		return 1
	fi

	run sudo mv "$tmp_file" "$target_file"
	return 0
}

# Idempotent state helpers: short-circuit silently when the desired state is
# already in place. Keeps --dry-run output focused on actual changes.
ensure_dir() {
	[ -d "$1" ] && return 0
	run sudo mkdir -p "$1"
}
ensure_owner() {
	local path="$1" owner="$2"
	[ -e "$path" ] || return 0
	local current
	current=$(stat -c '%u:%g' "$path" 2>/dev/null || true)
	# Translate "pi:pi" / "472:472" / "65534:65534" — accept either form.
	if [[ "$owner" == *:* ]] && [[ "$owner" != [0-9]*:[0-9]* ]]; then
		local want
		want=$(id -u "${owner%:*}" 2>/dev/null):$(getent group "${owner#*:}" | cut -d: -f3)
		[ "$current" = "$want" ] && return 0
	else
		[ "$current" = "$owner" ] && return 0
	fi
	run sudo chown -fh "$owner" "$path" 2>/dev/null || true
}
ensure_owner_recursive() {
	local path="$1" owner="$2"
	[ -e "$path" ] || return 0
	# Cheap heuristic: if the top dir is already correctly owned, assume the
	# tree is too. Full recursive stat would defeat the optimization.
	local current
	current=$(stat -c '%u:%g' "$path" 2>/dev/null || true)
	local want
	if [[ "$owner" == *:* ]] && [[ "$owner" != [0-9]*:[0-9]* ]]; then
		want=$(id -u "${owner%:*}" 2>/dev/null):$(getent group "${owner#*:}" | cut -d: -f3)
	else
		want="$owner"
	fi
	[ "$current" = "$want" ] && return 0
	run sudo chown -Rfh "$owner" "$path" 2>/dev/null || true
}
ensure_symlink() {
	local target="$1" link="$2"
	[ "$(readlink "$link" 2>/dev/null)" = "$target" ] && return 0
	run ln -sfn "$target" "$link"
}

###############################################################################
load_configuration() {
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	CONFIG_FILE="$SCRIPT_DIR/config.conf"
	LOCAL_CONFIG_FILE="$SCRIPT_DIR/config.local.conf"

	if [ -f "$CONFIG_FILE" ]; then
		log "Loading configuration from $CONFIG_FILE"
		# shellcheck disable=SC1090
		source "$CONFIG_FILE"
	else
		warn "Configuration file not found at $CONFIG_FILE"
	fi

	if [ -f "$LOCAL_CONFIG_FILE" ]; then
		log "Loading local configuration from $LOCAL_CONFIG_FILE"
		# shellcheck disable=SC1090
		source "$LOCAL_CONFIG_FILE"
	else
		warn "Local configuration not found at $LOCAL_CONFIG_FILE"
		warn "Copy config.local.conf.example to config.local.conf and fill in secrets."
	fi
}

###############################################################################
enforce_user_pi() {
	if [[ $EUID -eq 0 ]]; then
		error "This script should not be run as root. Run as the 'pi' user."
	fi
	if [ "$(id -un)" != "pi" ]; then
		if [[ $DRY_RUN -eq 1 ]]; then
			warn "Not running as 'pi' user (currently: $(id -un)); allowed under --dry-run."
			return 0
		fi
		error "This script must be run as user 'pi' (currently: $(id -un))."
	fi
}

###############################################################################
setup_essential_packages() {
	log "Updating system packages..."
	apt_get update
	apt_get upgrade -y

	log "Installing essential packages..."
	# git: system-monitor clone. borgbackup: immich/do-backup.sh. fatrace: rootfs-writes.sh.
	apt_get_install neovim curl wget rsync jq git borgbackup fatrace $INSTALL_PACKAGES
}

###############################################################################
# Return the filesystem type of a partition with the given label, or empty.
_partition_fstype() {
	lsblk -rno FSTYPE,LABEL | awk -v l="$1" '$2==l {print $1; exit}'
}

# Return the device path of a partition with the given label, or empty.
# Needs root: blkid reads /dev/disk/by-label/ which is restricted.
_partition_device() {
	sudo blkid -L "$1" 2>/dev/null || true
}

# True (0) if a partition with the given label exists, false (1) otherwise.
_partition_exists() {
	[ -n "$(_partition_device "$1")" ]
}

# Add an fstab entry, idempotent. Args: <label> <mountpoint> <fs> <options> <dump> <pass>
_ensure_fstab() {
	local label="$1" mount="$2" fs="$3" opts="$4" dump="$5" pass="$6"
	set_conf_line_to_file /etc/fstab "LABEL=$label " \
		"LABEL=$label $mount $fs $opts $dump $pass"
}

# Mount a labeled partition at $mount if not already mounted there. Adds an
# fstab entry first so the mount survives reboot.
_mount_labeled() {
	local label="$1" mount="$2"
	local fs; fs=$(_partition_fstype "$label")
	if [ -z "$fs" ]; then
		warn "Partition LABEL=$label not found; skipping mount of $mount"
		return 1
	fi
	_ensure_fstab "$label" "$mount" "$fs" "defaults,noatime" "0" "1"
	if ! mountpoint -q "$mount"; then
		ensure_dir "$mount"
		run sudo mount "$mount"
	fi
	return 0
}

###############################################################################
# Detect each configured disk, mount what should be mounted, and add fstab
# entries for everything (including BACKUP, which stays unmounted).
#
# Sets these globals for use by later install_* functions:
#   STORAGE_ROOT        — bulk-data root (DATA disk, else $HOME)
#   IMMICH_STORAGE_ROOT — fast storage for Immich's postgres/ and thumbs/
#   IMMICH_LIBRARY_ROOT — bulk storage for Immich's library/ (originals, video)
#   BACKUP_MOUNTPOINT   — where backup helpers will mount BACKUP on demand
setup_disks() {
	local root_label
	root_label=$(findmnt -no LABEL / 2>/dev/null || true)

	# --- DATA: permanent mount at /media/data ---
	STORAGE_ROOT="$HOME"
	if [ -n "$DATA_PARTITION_LABEL" ]; then
		if _mount_labeled "$DATA_PARTITION_LABEL" /media/data; then
			STORAGE_ROOT=/media/data
		else
			warn "DATA disk not present; STORAGE_ROOT falls back to $STORAGE_ROOT"
		fi
	fi
	log "STORAGE_ROOT=$STORAGE_ROOT"

	# --- BOOT: NVMe/SSD for Immich storage. May be the OS root itself. ---
	if [ -n "$IMMICH_STORAGE_ROOT" ]; then
		log "IMMICH_STORAGE_ROOT=$IMMICH_STORAGE_ROOT (from config)"
	elif [ -n "$BOOT_PARTITION_LABEL" ] && [ "$root_label" = "$BOOT_PARTITION_LABEL" ]; then
		IMMICH_STORAGE_ROOT="/var/lib/immich-storage"
		log "BOOT label matches root filesystem; IMMICH_STORAGE_ROOT=$IMMICH_STORAGE_ROOT"
	elif [ -n "$BOOT_PARTITION_LABEL" ] && _partition_exists "$BOOT_PARTITION_LABEL"; then
		_mount_labeled "$BOOT_PARTITION_LABEL" /media/boot
		IMMICH_STORAGE_ROOT="/media/boot/immich"
		log "Mounted BOOT at /media/boot; IMMICH_STORAGE_ROOT=$IMMICH_STORAGE_ROOT"
	else
		IMMICH_STORAGE_ROOT="$STORAGE_ROOT/immich"
		log "No BOOT disk; IMMICH_STORAGE_ROOT=$IMMICH_STORAGE_ROOT (on STORAGE_ROOT)"
	fi

	# Originals and encoded video don't fit on the fast disk, so the library
	# always lives on STORAGE_ROOT. Only postgres and thumbs go on BOOT.
	IMMICH_LIBRARY_ROOT="$STORAGE_ROOT/immich"
	log "IMMICH_LIBRARY_ROOT=$IMMICH_LIBRARY_ROOT"

	# --- BACKUP: fstab only with noauto,nofail; never mounted by setup.sh. ---
	BACKUP_MOUNTPOINT=""
	if [ -n "$BACKUP_PARTITION_LABEL" ]; then
		if _partition_exists "$BACKUP_PARTITION_LABEL"; then
			local fs; fs=$(_partition_fstype "$BACKUP_PARTITION_LABEL")
			_ensure_fstab "$BACKUP_PARTITION_LABEL" /media/backup "$fs" \
				"defaults,noatime,noauto,nofail" "0" "0"
			ensure_dir /media/backup
			BACKUP_MOUNTPOINT=/media/backup
			log "BACKUP disk configured at /media/backup (mounted on demand)"
			# noauto is not enough: udisks mounts it at boot on behalf of the
			# Kodi session. Tell udisks to ignore the partition.
			local rule_tmp="$SCRIPT_DIR/udisks-ignore-backup.rules.tmp"
			sed "s|@BACKUP_LABEL@|$BACKUP_PARTITION_LABEL|" \
				"$SCRIPT_DIR/system/99-udisks-ignore-backup.rules" > "$rule_tmp"
			if set_conf_file /etc/udev/rules.d/99-udisks-ignore-backup.rules "$rule_tmp"; then
				run sudo udevadm control --reload
				run sudo udevadm trigger --subsystem-match=block
			fi
			rm -f "$rule_tmp"
		else
			warn "BACKUP partition LABEL=$BACKUP_PARTITION_LABEL not found; skipping"
		fi
	fi
}

###############################################################################
# Create the standard subdirectories under STORAGE_ROOT and symlink each into
# $HOME. Runs unconditionally so the no-external-drive case still produces the
# directories Transmission/Samba expect.
setup_storage_dirs() {
	local dirs=(cartoons documents downloads icons movies music pictures retropie rtorrent tvshows videos)
	local d
	for d in "${dirs[@]}"; do
		ensure_dir "$STORAGE_ROOT/$d"
		ensure_owner "$STORAGE_ROOT/$d" pi:pi
	done

	# Skip making $HOME/foo a symlink to $STORAGE_ROOT/foo when they're the
	# same directory (the no-external-drive case).
	if [ "$STORAGE_ROOT" = "$HOME" ]; then
		return 0
	fi

	for d in "${dirs[@]}"; do
		ensure_symlink "$STORAGE_ROOT/$d" "$HOME/$d"
	done
}

###############################################################################
configure_locales() {
	log "Configuring locales..."
	apt_get_install locales

	# update-locale validates against /etc/locale.gen. Make sure both required
	# locales are uncommented there before we run it.
	local l
	for l in "$LOCALE_PRIMARY" "$LOCALE_SECONDARY"; do
		if grep -q "^# *${l} " /etc/locale.gen 2>/dev/null; then
			run sudo sed -i "s/^# *${l} /${l} /" /etc/locale.gen
		elif ! grep -q "^${l} " /etc/locale.gen 2>/dev/null; then
			echo "${l} UTF-8" | run sudo tee -a /etc/locale.gen > /dev/null
		fi
	done

	run sudo locale-gen

	run sudo update-locale \
		LANG="$LOCALE_PRIMARY" \
		LANGUAGE="" \
		LC_NUMERIC="$LOCALE_SECONDARY" \
		LC_TIME="$LOCALE_SECONDARY" \
		LC_MONETARY="$LOCALE_SECONDARY" \
		LC_PAPER="$LOCALE_SECONDARY" \
		LC_NAME="$LOCALE_SECONDARY" \
		LC_ADDRESS="$LOCALE_SECONDARY" \
		LC_TELEPHONE="$LOCALE_SECONDARY" \
		LC_MEASUREMENT="$LOCALE_SECONDARY" \
		LC_IDENTIFICATION="$LOCALE_SECONDARY" \
		LC_ALL=""

	log "Locales configured."
}

###############################################################################
add_user_to_group() {
	local user="$1"
	local group="$2"

	if ! getent group "$group" >/dev/null; then
		warn "Group '$group' does not exist on this system; skipping"
		return 0
	fi

	if id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
		return 0
	fi

	log "Adding user '$user' to '$group' group..."
	run sudo usermod -a -G "$group" "$user"
	return 1
}

setup_groups() {
	local changed=false

	add_user_to_group pi lp || changed=true
	add_user_to_group pi docker || changed=true

	# We don't exit/reboot here: the rest of the script uses `sudo` for any
	# command that needs the new group (e.g. `sudo docker compose`), so the
	# current session's stale group set isn't actually a blocker. The user
	# will see the new groups after their next login.
	if [ "$changed" = true ]; then
		warn "Group membership for 'pi' was updated. Log out/in to use 'docker' or 'lpadmin' without sudo."
	fi
}

###############################################################################
install_cups() {
	log "Installing CUPS..."
	apt_get_install cups

	local current
	current=$(sudo cupsctl 2>/dev/null || true)
	if ! { echo "$current" | grep -q '^_remote_admin=1' \
		&& echo "$current" | grep -q '^_remote_any=1' \
		&& echo "$current" | grep -q '^_share_printers=1'; }; then
		run sudo cupsctl --remote-admin --remote-any --share-printers
		run sudo systemctl restart cups.service
	fi
}

###############################################################################
install_samba() {
	log "Installing Samba..."
	apt_get_install samba samba-common-bin

	local changed=0

	if set_conf_file /etc/samba/smb.conf "$SCRIPT_DIR/samba/smb.conf"; then
		changed=1
	fi

	if sudo pdbedit -L 2>/dev/null | grep -q '^pi:'; then
		:
	else
		# Not fatal: a rebuild with an empty config.local.conf must still get
		# through Immich and the rest. Shares just stay inaccessible until
		# `sudo smbpasswd -a pi` (or the variable is filled and setup re-run).
		if [ -z "${SAMBA_PI_PASSWORD:-}" ]; then
			warn "SAMBA_PI_PASSWORD is not set in config.local.conf; Samba user 'pi' not created. Run: sudo smbpasswd -a pi"
		else
			log "Creating Samba user 'pi' (non-interactive)..."
			printf '%s\n%s\n' "$SAMBA_PI_PASSWORD" "$SAMBA_PI_PASSWORD" | run sudo smbpasswd -s -a pi >/dev/null
			changed=1
		fi
	fi

	if [ $changed -eq 1 ]; then
		run sudo systemctl restart smbd nmbd 2>/dev/null || run sudo systemctl restart samba
	fi
}

###############################################################################
install_transmission() {
	log "Installing Transmission..."
	apt_get_install transmission-daemon

	local conf_file=/etc/transmission-daemon/settings.json
	local unit_file=/lib/systemd/system/transmission-daemon.service
	local changed=0

	# Patch the systemd unit to run as 'pi' (only if not already patched).
	if sudo grep -q 'debian-transmission' "$unit_file" 2>/dev/null; then
		run sudo sed -i 's/debian-transmission/pi/g' "$unit_file"
		run sudo systemctl daemon-reload
		changed=1
	fi

	# Apply settings. jq_inplace returns 0 when content actually changes.
	# Stop the daemon if any change is pending so it doesn't overwrite our edits.
	local jq_filters=(
		'."rpc-whitelist-enabled" = false'
		'."rpc-host-whitelist-enabled" = false'
		'."rpc-authentication-required" = true'
		'."ratio-limit" = 2'
		'."ratio-limit-enabled" = true'
		".\"download-dir\" = \"$STORAGE_ROOT/downloads\""
	)

	local stopped=0
	local f
	for f in "${jq_filters[@]}"; do
		# Read-only probe: would this filter change the file?
		local current desired
		current=$(sudo cat "$conf_file")
		desired=$(printf '%s' "$current" | jq "$f")
		if [ "$current" != "$desired" ]; then
			if [ $stopped -eq 0 ]; then
				run sudo systemctl stop transmission-daemon
				stopped=1
			fi
			if jq_inplace "$f" "$conf_file"; then
				changed=1
			fi
		fi
	done

	if [ $changed -eq 1 ]; then
		run sudo systemctl start transmission-daemon
	fi
}

###############################################################################
# Keep the root filesystem (the NVMe) quiet. The previous NVMe wore out under
# months of constant small writes (journal, docker logs, prometheus, swap).
# Everything that writes continuously goes to RAM or to the DATA HDD; the
# NVMe is left with occasional writes (apt, config edits) plus the deliberate
# Immich postgres/thumbs cache. See rootfs-writes.sh to check what is left.
# Set LOW_WRITE_ROOTFS="" in config.conf to skip all of this.
setup_low_write_rootfs() {
	[ -n "${LOW_WRITE_ROOTFS:-}" ] || return 0
	log "Reducing writes to the root filesystem..."

	# 1. /tmp in RAM. Only the fstab entry is written; systemd mounts it on
	#    its next daemon-reload (apt triggers one), hiding whatever was in
	#    /tmp until then, so nothing in this script may keep state there.
	set_conf_line_to_file /etc/fstab "tmpfs /tmp " \
		"tmpfs /tmp tmpfs defaults,noatime,nosuid,nodev,size=${TMPFS_TMP_SIZE:-512M} 0 0"
	if ! mountpoint -q /tmp; then
		REBOOT_REQUIRED=1
	fi

	# 2. Swap: no swapfile on the NVMe, compressed zram instead.
	if dpkg-query -W -f='${Status}' dphys-swapfile 2>/dev/null | grep -q 'install ok installed'; then
		log "Replacing dphys-swapfile with zram"
		run sudo systemctl disable --now dphys-swapfile 2>/dev/null || true
		apt_get purge -y dphys-swapfile
		[ -f /var/swap ] && run sudo rm -f /var/swap
	fi
	apt_get_install zram-tools
	local zram_tmp="$SCRIPT_DIR/zramswap.tmp"
	sed "s|@ZRAM_SIZE_MB@|${ZRAM_SIZE_MB:-2048}|" "$SCRIPT_DIR/system/zramswap" > "$zram_tmp"
	if set_conf_file /etc/default/zramswap "$zram_tmp"; then
		run sudo systemctl restart zramswap.service
	fi
	rm -f "$zram_tmp"
	if set_conf_file /etc/sysctl.d/99-zram.conf "$SCRIPT_DIR/system/99-sysctl-zram.conf"; then
		run sudo sysctl -q -p /etc/sysctl.d/99-zram.conf
	fi

	# 3. Logs in RAM only: volatile journal, /var/log on tmpfs. A previous
	#    revision bind-mounted /var/log from the DATA disk; that fstab line
	#    is removed if present.
	ensure_dir /etc/systemd/journald.conf.d
	local journald_changed=0
	if set_conf_file /etc/systemd/journald.conf.d/low-write.conf "$SCRIPT_DIR/system/journald-low-write.conf"; then
		journald_changed=1
	fi
	set_conf_file /etc/tmpfiles.d/var-log.conf "$SCRIPT_DIR/system/var-log-tmpfiles.conf" || true
	if sudo grep -q " /var/log none bind" /etc/fstab; then
		run sudo sed -i "\| /var/log none bind|d" /etc/fstab
		run sudo systemctl daemon-reload
	fi
	set_conf_line_to_file /etc/fstab "tmpfs /var/log " \
		"tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,mode=0755,size=${TMPFS_LOG_SIZE:-256M} 0 0"
	if [ "$(findmnt -no FSTYPE /var/log 2>/dev/null)" != "tmpfs" ]; then
		# Switching live would hide files daemons hold open; done at boot.
		REBOOT_REQUIRED=1
	fi
	if [ $journald_changed -eq 1 ]; then
		run sudo systemctl restart systemd-journald
	fi
}

###############################################################################
install_tailscale() {
	log "Installing Tailscale..."
	local keyring=/usr/share/keyrings/tailscale-archive-keyring.gpg
	local list=/etc/apt/sources.list.d/tailscale.list
	local codename
	codename=$(. /etc/os-release && echo "$VERSION_CODENAME")

	if [ ! -f "$keyring" ]; then
		run sudo curl -fsSL "https://pkgs.tailscale.com/stable/raspbian/$codename.noarmor.gpg" -o "$keyring"
	fi
	local list_tmp="$SCRIPT_DIR/tailscale.list.tmp"
	echo "deb [signed-by=$keyring] https://pkgs.tailscale.com/stable/raspbian $codename main" > "$list_tmp"
	if set_conf_file "$list" "$list_tmp"; then
		apt_get update
	fi
	rm -f "$list_tmp"
	apt_get_install tailscale

	# No log upload to Tailscale and no local logtail buffer: the buffer
	# files under /var/lib/tailscale grew to 40 MB and were rewritten all
	# day on the NVMe.
	local flags='FLAGS="--no-logs-no-support"'
	if ! grep -Fxq "$flags" /etc/default/tailscaled 2>/dev/null; then
		set_conf_line_to_file /etc/default/tailscaled "FLAGS=" "$flags"
		run sudo systemctl restart tailscaled
	fi
	run sudo systemctl enable --now tailscaled

	# Login is a browser flow and is never forced here. A restored
	# /var/lib/tailscale/tailscaled.state keeps the node identity.
	if ! tailscale status >/dev/null 2>&1; then
		warn "Tailscale is installed but not logged in. Run: sudo tailscale up"
	fi
}

###############################################################################
# Docker's data-root (images, container logs, named volumes such as
# prometheus/grafana data) goes to the DATA disk under LOW_WRITE_ROOTFS.
# Immich's postgres and thumbs stay on the NVMe through their bind mounts.
_docker_data_root() {
	if [ -n "${LOW_WRITE_ROOTFS:-}" ] && [ "$STORAGE_ROOT" != "$HOME" ]; then
		echo "$STORAGE_ROOT/docker"
	fi
}

# One-time move of /var/lib/docker to the new data-root. Only runs while
# docker is stopped (or not installed yet) and the new root does not exist,
# so a salvaged volumes/ directory is carried over instead of being shadowed
# by a freshly initialised root. The old directory is renamed, not deleted.
_docker_migrate_root() {
	local new_root="$1"
	[ -n "$new_root" ] || return 0
	[ -d /var/lib/docker ] || return 0
	[ -e "$new_root" ] && return 0
	log "Moving /var/lib/docker to $new_root (one-time)"
	ensure_dir "$(dirname "$new_root")"
	run sudo rsync -aHAX /var/lib/docker/ "$new_root/"
	run sudo mv /var/lib/docker "/var/lib/docker.migrated-$(date +%Y%m%d)"
}

_containerd_migrate_root() {
	local new_root="$1"
	[ -n "$new_root" ] || return 0
	[ -d /var/lib/containerd ] || return 0
	[ -e "$new_root" ] && return 0
	log "Moving /var/lib/containerd to $new_root (one-time)"
	ensure_dir "$(dirname "$new_root")"
	run sudo rsync -aHAX /var/lib/containerd/ "$new_root/"
	run sudo mv /var/lib/containerd "/var/lib/containerd.migrated-$(date +%Y%m%d)"
}

install_docker() {
	local data_root containerd_root=""
	data_root=$(_docker_data_root)
	[ -n "$data_root" ] && containerd_root="$STORAGE_ROOT/containerd"

	# daemon.json first, so the very first start already uses it.
	ensure_dir /etc/docker
	local daemon_tmp="$SCRIPT_DIR/daemon.json.tmp"
	# Container logs go to journald (RAM, see setup_low_write_rootfs);
	# `docker logs` still works with that driver.
	jq -n --arg root "$data_root" \
		'{"log-driver": "journald"}
		 + (if $root != "" then {"data-root": $root} else {} end)' > "$daemon_tmp"
	local daemon_changed=0
	if set_conf_file /etc/docker/daemon.json "$daemon_tmp"; then
		daemon_changed=1
	fi
	rm -f "$daemon_tmp"

	if [ -n "$data_root" ]; then
		ensure_dir /etc/systemd/system/docker.service.d
		ensure_dir /etc/systemd/system/containerd.service.d
		ensure_dir /etc/containerd
		local tmp="$SCRIPT_DIR/wait-for-data.conf.tmp"
		sed "s|@STORAGE_ROOT@|$STORAGE_ROOT|" "$SCRIPT_DIR/system/docker-wait-for-data.conf" > "$tmp"
		if set_conf_file /etc/systemd/system/docker.service.d/wait-for-data.conf "$tmp"; then
			run sudo systemctl daemon-reload
		fi
		sed "s|@STORAGE_ROOT@|$STORAGE_ROOT|" "$SCRIPT_DIR/system/containerd-wait-for-data.conf" > "$tmp"
		if set_conf_file /etc/systemd/system/containerd.service.d/wait-for-data.conf "$tmp"; then
			run sudo systemctl daemon-reload
		fi
		sed "s|@ROOT@|$containerd_root|" "$SCRIPT_DIR/system/containerd-config.toml" > "$tmp"
		if set_conf_file /etc/containerd/config.toml "$tmp"; then
			daemon_changed=1
		fi
		rm -f "$tmp"
	fi

	if ! command -v docker > /dev/null; then
		_docker_migrate_root "$data_root"
		_containerd_migrate_root "$containerd_root"
		log "Installing Docker..."
		curl -fsSL https://get.docker.com | run sh
		return 0
	fi

	if [ $daemon_changed -eq 1 ]; then
		log "Docker/containerd config changed; restarting both"
		run sudo systemctl stop docker.socket docker.service containerd.service
		_docker_migrate_root "$data_root"
		_containerd_migrate_root "$containerd_root"
		run sudo systemctl start containerd.service docker.service
	else
		log "Docker already installed."
	fi
}

###############################################################################
# One-time migration helper: tear down legacy stacks that have been removed
# from the repo (Nextcloud, Rustdesk) and move ~/apps/immich-go out of ~/apps/
# so that directory can vanish. Idempotent — does nothing once the legacy
# state is gone.
cleanup_legacy_stacks() {
	if ! command -v docker >/dev/null; then
		return 0
	fi

	# Nextcloud — user does not use it. The compose file has been removed from
	# the repo but the containers may still be running from the old layout.
	local nc
	for nc in nextcloud-app-1 nextcloud-redis-1 nextcloud-db-1; do
		if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$nc"; then
			log "Removing legacy Nextcloud container: $nc"
			run sudo docker rm -f "$nc"
		fi
	done
	if docker network ls --format '{{.Name}}' 2>/dev/null | grep -qx nextcloud_default; then
		run sudo docker network rm nextcloud_default || true
	fi
	# Note: /media/data/nextcloud/ (the volumes on disk) is intentionally
	# preserved — user can delete it manually after they're sure nothing's
	# needed.

	# Rustdesk — replaced by Tailscale.
	local rd
	for rd in hbbs hbbr; do
		if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$rd"; then
			log "Removing legacy Rustdesk container: $rd"
			run sudo docker rm -f "$rd"
		fi
	done
	if [ -d "$HOME/apps/rustdesk" ]; then
		log "Removing $HOME/apps/rustdesk"
		run sudo rm -rf "$HOME/apps/rustdesk"
	fi

	# immich-go binary — keep it, but out of ~/apps/.
	if [ -f "$HOME/apps/immich-go" ] && [ ! -f "$HOME/.local/bin/immich-go" ]; then
		log "Moving immich-go binary to ~/.local/bin/"
		run mkdir -p "$HOME/.local/bin"
		run mv "$HOME/apps/immich-go" "$HOME/.local/bin/immich-go"
	fi

	# Once both stacks and the binary are gone, ~/apps/ can be removed too
	# (install_immich's migration step removes ~/apps/immich-app/ on its own).
	if [ -d "$HOME/apps" ] && [ -z "$(ls -A "$HOME/apps" 2>/dev/null)" ]; then
		run rmdir "$HOME/apps"
	fi
}

###############################################################################
install_immich() {
	log "Installing Immich (db/thumbs at $IMMICH_STORAGE_ROOT, library at $IMMICH_LIBRARY_ROOT)..."

	# Ensure storage directories exist
	ensure_dir "$IMMICH_LIBRARY_ROOT/library"
	ensure_dir "$IMMICH_STORAGE_ROOT/postgres"
	ensure_dir "$IMMICH_STORAGE_ROOT/thumbs"
	ensure_owner "$IMMICH_STORAGE_ROOT" pi:pi
	ensure_owner "$IMMICH_LIBRARY_ROOT/library" pi:pi

	local immich_dir="$SCRIPT_DIR/immich"
	local env_file="$immich_dir/.env"
	local example_file="$immich_dir/.env.example"

	# Migration: if the legacy ~/apps/immich-app install exists and our .env
	# isn't set up yet, copy its .env (preserves DB_PASSWORD => postgres data
	# stays decryptable), stop the old project, then bring up from the repo.
	local legacy_dir="$HOME/apps/immich-app"
	if [ ! -f "$env_file" ] && [ -f "$legacy_dir/.env" ]; then
		log "Migrating Immich from $legacy_dir to $immich_dir"
		run cp "$legacy_dir/.env" "$env_file"
		if [ -f "$legacy_dir/docker-compose.yml" ]; then
			run sudo docker compose -f "$legacy_dir/docker-compose.yml" --project-name immich down || true
		fi
	fi

	# Fresh install: seed .env from the example template.
	if [ ! -f "$env_file" ]; then
		log "Generating $env_file from .env.example"
		run cp "$example_file" "$env_file"
		run sed -i "s|^UPLOAD_LOCATION=.*|UPLOAD_LOCATION=$IMMICH_LIBRARY_ROOT/library|" "$env_file"
		run sed -i "s|^THUMB_LOCATION=.*|THUMB_LOCATION=$IMMICH_STORAGE_ROOT/thumbs|" "$env_file"
		run sed -i "s|^DB_DATA_LOCATION=.*|DB_DATA_LOCATION=$IMMICH_STORAGE_ROOT/postgres|" "$env_file"
		if [ -n "${BACKUP_MOUNTPOINT:-}" ]; then
			run sed -i "s|^BACKUP_PATH=.*|BACKUP_PATH=$BACKUP_MOUNTPOINT/immich/|" "$env_file"
		fi
		if [ -n "${IMMICH_DB_PASSWORD:-}" ]; then
			run sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$IMMICH_DB_PASSWORD|" "$env_file"
		else
			warn "IMMICH_DB_PASSWORD is not set in config.local.conf — Immich will start with the placeholder password."
		fi
	fi

	# .env files from before the separate thumbs mount: keep thumbs where they
	# already are (inside UPLOAD_LOCATION) rather than start on an empty dir.
	# Moving them to fast storage is a manual rsync + edit of THUMB_LOCATION.
	if [ -f "$env_file" ] && ! grep -q '^THUMB_LOCATION=' "$env_file"; then
		local upload_location
		upload_location=$(sed -n 's/^UPLOAD_LOCATION=//p' "$env_file")
		echo "THUMB_LOCATION=$upload_location/thumbs" | run tee -a "$env_file" > /dev/null
	fi

	# Disaster recovery: an empty postgres dir next to a library that has
	# backups means the OS disk was lost. Load the newest nightly dump before
	# the server can initialise an empty database. Thumbs that are missing are
	# regenerated later by Immich's own job.
	local db_dir upload_dir
	db_dir=$(sed -n 's/^DB_DATA_LOCATION=//p' "$env_file")
	upload_dir=$(sed -n 's/^UPLOAD_LOCATION=//p' "$env_file")
	# The postgres dir is 0700 and owned by the container's uid, so the
	# existence check needs root.
	if ! sudo test -f "$db_dir/PG_VERSION" && ls "$upload_dir"/backups/immich-db-backup-*.sql.gz >/dev/null 2>&1; then
		log "Immich database is empty but $upload_dir/backups has dumps; restoring the newest"
		run "$immich_dir/restore-db.sh"
	fi

	# Bring up (idempotent — compose detects no change).
	( cd "$immich_dir" && run sudo docker compose up -d )

	# Weekly backup. Runs as root: the borg repo on BACKUP is root-owned.
	if [ -n "${BACKUP_MOUNTPOINT:-}" ]; then
		local cron_file="$immich_dir/immich-backup.cron.tmp"
		printf '%s\n' \
			"# Weekly Immich backup (borg + pg_dumpall) to the BACKUP disk. Managed by my-server-conf/setup.sh." \
			"0 3 * * 1 root $immich_dir/do-backup.sh" > "$cron_file"
		set_conf_file /etc/cron.d/immich-backup "$cron_file" || true
		rm -f "$cron_file"
	fi
	# The pre-repo install ran the backup from root's crontab out of ~/apps/immich-app.
	if sudo crontab -l 2>/dev/null | grep -q 'apps/immich-app/do-backup.sh'; then
		log "Removing legacy ~/apps/immich-app backup entry from root's crontab"
		sudo crontab -l | grep -v 'apps/immich-app/do-backup.sh' | run sudo crontab -
	fi

	# Wait briefly for health, then tear down the legacy install dir if migrating.
	if [ -d "$legacy_dir" ] && [ -f "$env_file" ] && [ "$env_file" != "$legacy_dir/.env" ]; then
		log "Verifying repo-anchored Immich is healthy before removing $legacy_dir"
		local i
		for i in $(seq 1 30); do
			if sudo docker compose -f "$immich_dir/docker-compose.yml" ps --status running --quiet | grep -q .; then
				break
			fi
			sleep 2
		done
		if sudo docker compose -f "$immich_dir/docker-compose.yml" ps --status running --quiet | grep -q .; then
			log "Removing legacy $legacy_dir"
			run sudo rm -rf "$legacy_dir"
		else
			warn "Repo-anchored Immich did not report running; leaving $legacy_dir in place. Investigate then re-run."
		fi
	fi
}

###############################################################################
###############################################################################
# Set/replace a single <setting id="X">VAL</setting> entry in a Kodi-style XML
# file. Idempotent: returns 0 if it changed something, 1 if already correct.
_kodi_set_setting() {
	local file="$1" id="$2" val="$3"
	local current
	current=$(xmlstarlet sel -t -v "//setting[@id='$id']" "$file" 2>/dev/null || true)
	# Kodi re-writes guisettings on exit, and re-adds a `default="true"` attribute
	# whenever the stored value matches its built-in default. We don't care about
	# that attribute — only the value matters. Return "no change" when value
	# already matches.
	if [ "$current" = "$val" ]; then
		return 1
	fi
	if xmlstarlet sel -t -c "//setting[@id='$id']" "$file" >/dev/null 2>&1; then
		run xmlstarlet ed -L \
			-d "//setting[@id='$id']/@default" \
			-u "//setting[@id='$id']" -v "$val" \
			"$file"
	else
		run xmlstarlet ed -L \
			-s '/settings' -t elem -n setting -v "$val" \
			-i '/settings/setting[last()]' -t attr -n id -v "$id" \
			"$file"
	fi
	return 0
}

install_kodi() {
	log "Installing Kodi..."
	# Only install the Debian `kodi` metapackage if Kodi isn't already
	# present. Raspberry Pi OS users typically run `kodi21` from the RPi
	# repos (newer than Debian's `kodi` 20.x); we must not downgrade them.
	if ! command -v kodi-standalone >/dev/null; then
		apt_get_install kodi
	fi
	apt_get_install xmlstarlet

	# 1. Systemd unit. set_conf_file returns 0 on actual change.
	if set_conf_file /etc/systemd/system/kodi.service "$SCRIPT_DIR/kodi/kodi.service"; then
		run sudo systemctl daemon-reload
		run sudo systemctl enable kodi.service
	fi
	# Start on first install; subsequent runs are no-ops because systemctl is
	# idempotent for already-running units.
	if ! systemctl is-active --quiet kodi.service 2>/dev/null; then
		run sudo systemctl start kodi.service || \
			warn "kodi.service failed to start (likely no display attached — fine for a test container)."
	fi

	# 1b. Kodi's temp dir (kodi.log, caches) on tmpfs. Without a display the
	#     audio sink retries twice a second and the log alone wrote 1800 times
	#     in three minutes. kodi.service recreates the tmpfs dir on each boot.
	if [ -n "${LOW_WRITE_ROOTFS:-}" ]; then
		local kodi_temp="$HOME/.kodi/temp"
		if [ -d "$kodi_temp" ] && [ ! -L "$kodi_temp" ]; then
			log "Moving $kodi_temp to tmpfs"
			run sudo systemctl stop kodi.service
			run rm -rf "$kodi_temp"
		fi
		run mkdir -p /tmp/kodi-temp
		ensure_symlink /tmp/kodi-temp "$kodi_temp"
		if ! systemctl is-active --quiet kodi.service 2>/dev/null; then
			run sudo systemctl start kodi.service || true
		fi
	fi

	# 2. Sources — write once. Don't clobber the user's library on later runs.
	local userdata="$HOME/.kodi/userdata"
	ensure_dir "$userdata"
	if [ ! -f "$userdata/sources.xml" ]; then
		log "Writing initial $userdata/sources.xml"
		run bash -c "sed 's|@STORAGE_ROOT@|$STORAGE_ROOT|g' '$SCRIPT_DIR/kodi/sources.xml.template' > '$userdata/sources.xml'"
	fi

	# 3. guisettings.xml — patch idempotently. Kodi rewrites this file on
	# clean exit, so we must stop Kodi before editing or our changes are lost.
	local gui="$userdata/guisettings.xml"
	if [ ! -f "$gui" ]; then
		log "Kodi guisettings.xml not present yet. Start Kodi once (the kodi.service will do it once a display is attached) and re-run setup.sh to apply remote-control settings."
		return 0
	fi

	# Pre-check: only stop/start kodi when an actual setting differs.
	local want_settings=(
		"services.webserver=true"
		"services.webserverport=8081"
		"services.webserverauthentication=false"
		"services.esenabled=true"
		"services.esallinterfaces=true"
	)
	local need_change=0 kv id val current
	for kv in "${want_settings[@]}"; do
		id="${kv%%=*}"; val="${kv#*=}"
		current=$(xmlstarlet sel -t -v "//setting[@id='$id']" "$gui" 2>/dev/null || true)
		if [ "$current" != "$val" ]; then
			need_change=1
			break
		fi
	done

	if [ $need_change -eq 0 ]; then
		return 0
	fi

	# One-time backup before any in-place edits.
	[ -f "$gui.pre-migrate.bak" ] || run cp "$gui" "$gui.pre-migrate.bak"

	local kodi_was_running=0
	if systemctl is-active --quiet kodi.service 2>/dev/null; then
		kodi_was_running=1
		run sudo systemctl stop kodi.service
	fi

	for kv in "${want_settings[@]}"; do
		_kodi_set_setting "$gui" "${kv%%=*}" "${kv#*=}" || true
	done

	if [ $kodi_was_running -eq 1 ]; then
		run sudo systemctl start kodi.service
	fi
	log "Kodi guisettings updated; restarted kodi.service to pick them up."
}

install_nginxproxymanager() {
	log "Installing Nginx Proxy Manager..."
	# The compose file bind-mounts $SCRIPT_DIR/index.html to /var/www/html/
	# inside the container as the default landing page; no extra step here.
	( cd "$SCRIPT_DIR/nginxproxymanager" && run sudo docker compose up -d )

	# Custom per-proxy-host nginx snippet. NPM includes data/nginx/custom/
	# server_proxy.conf in every generated proxy host; we use it to raise upload
	# limits/timeouts so Immich uploads work through the proxy from external
	# networks (see the file's own comments). Reload nginx only when it changed.
	local npm_custom_dir="$SCRIPT_DIR/nginxproxymanager/data/nginx/custom"
	run sudo mkdir -p "$npm_custom_dir"
	if set_conf_file "$npm_custom_dir/server_proxy.conf" \
		"$SCRIPT_DIR/nginxproxymanager/server_proxy.conf"; then
		log "Reloading NPM nginx to apply custom proxy config..."
		( cd "$SCRIPT_DIR/nginxproxymanager" && run sudo docker compose exec app nginx -s reload )
	fi

	if [ -x "$SCRIPT_DIR/nginxproxymanager/seed.sh" ] && [ -z "${SETUP_SKIP_NPM_SEED:-}" ]; then
		log "Seeding NPM (admin + proxy hosts + wildcard cert)..."
		SCRIPT_DIR="$SCRIPT_DIR" run bash "$SCRIPT_DIR/nginxproxymanager/seed.sh"
	fi
}

###############################################################################
install_system_monitor() {
	log "Installing System monitor..."

	local sm_dir="$SCRIPT_DIR/system-monitor"
	# A clone salvaged from a dying disk can have a broken object store;
	# set it aside (its only local state is grafana/.env, re-seeded below)
	# and clone again rather than fail on pull.
	if [ -d "$sm_dir/.git" ] && ! git -C "$sm_dir" rev-parse --verify HEAD >/dev/null 2>&1; then
		warn "system-monitor clone is corrupt; moving it aside and cloning again"
		run mv "$sm_dir" "$sm_dir.corrupt-$(date +%Y%m%d-%H%M%S)"
	fi
	if [ -d "$sm_dir/.git" ]; then
		( cd "$sm_dir" && run git pull --ff-only )
	else
		run git clone https://github.com/AdUki/system-monitor.git "$sm_dir"
	fi

	# Both prometheus and grafana data live in named Docker volumes (managed
	# by the container's own UID); the bind-mounted dirs (./prometheus,
	# ./grafana/provisioning) only carry config, which Docker reads with
	# default permissions. No mkdir or chown of the working tree needed.
	#
	# The smartctl-exporter image is built locally from ./smartctl-exporter/
	# (upstream's image is amd64-only). First `up -d` triggers the build;
	# after a Dockerfile change run `docker compose build smartctl-exporter`
	# manually to refresh.

	# Seed Grafana admin credentials from config.local.conf into the env
	# file the container reads on first start.
	local env_file="$sm_dir/grafana/.env"
	if [ -n "${GRAFANA_ADMIN_USER:-}" ]; then
		set_conf_line_to_file "$env_file" "GF_SECURITY_ADMIN_USER=" \
			"GF_SECURITY_ADMIN_USER=$GRAFANA_ADMIN_USER"
	fi
	if [ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]; then
		set_conf_line_to_file "$env_file" "GF_SECURITY_ADMIN_PASSWORD=" \
			"GF_SECURITY_ADMIN_PASSWORD=$GRAFANA_ADMIN_PASSWORD"
	fi

	( cd "$sm_dir" && run sudo docker compose up -d )

	# Env vars only seed the initial admin on a fresh data volume; once the
	# user exists, Grafana's bcrypt-hashed password lives in the SQLite DB.
	# Use the CLI to reset it idempotently (same value = no observable change).
	if [ -n "${GRAFANA_ADMIN_PASSWORD:-}" ] && \
	   sudo docker ps --filter "name=^monitoring-grafana$" --format '{{.Names}}' 2>/dev/null | grep -qx monitoring-grafana; then
		run sudo docker exec monitoring-grafana \
			grafana cli --homepath /usr/share/grafana \
			admin reset-admin-password "$GRAFANA_ADMIN_PASSWORD" >/dev/null
	fi
}

###############################################################################
enforce_user_pi

log "Starting Raspberry Pi server setup..."

REBOOT_REQUIRED=0

load_configuration
setup_essential_packages
configure_locales
setup_disks
setup_storage_dirs
setup_low_write_rootfs
setup_groups
install_cups
install_samba
install_transmission
install_tailscale
install_docker
cleanup_legacy_stacks
install_immich
install_nginxproxymanager
install_system_monitor
install_kodi

if [ "$REBOOT_REQUIRED" = 1 ]; then
	warn "Some changes (tmpfs /tmp, /var/log on the HDD) only take full effect after a reboot."
fi
log "All done."
