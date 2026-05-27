# Sourced by the backup helpers. Mounts the BACKUP partition on demand and
# arranges to unmount it on script exit (only if it wasn't already mounted by
# the caller — we never tear down a mount we didn't create).
#
# Requires: BACKUP_PATH from .env, pointing at a path under the mount point.
# We derive the mount point as the first two path components: /media/backup.

if [ -z "${BACKUP_PATH:-}" ]; then
	echo "BACKUP_PATH not set in .env; cannot manage backup mount" >&2
	exit 1
fi

# /media/backup/immich/  ->  /media/backup
BACKUP_MOUNTPOINT="$(printf '%s' "$BACKUP_PATH" | awk -F/ '{print "/" $2 "/" $3}')"

ensure_backup_mounted() {
	if mountpoint -q "$BACKUP_MOUNTPOINT"; then
		BACKUP_WAS_MOUNTED=1
		return 0
	fi

	BACKUP_WAS_MOUNTED=0
	if ! sudo mount "$BACKUP_MOUNTPOINT" 2>/dev/null; then
		echo "ERROR: cannot mount $BACKUP_MOUNTPOINT — is the BACKUP disk attached and configured in /etc/fstab?" >&2
		exit 1
	fi
}

release_backup_mount() {
	if [ "${BACKUP_WAS_MOUNTED:-1}" -eq 0 ]; then
		sudo umount "$BACKUP_MOUNTPOINT" 2>/dev/null || true
	fi
}

ensure_backup_mounted
trap release_backup_mount EXIT
