#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
cd "$SCRIPT_DIR"
source "$SCRIPT_DIR/.env"
source "$SCRIPT_DIR/_backup-mount.sh"

MOUNT=/tmp/immich-mountpoint
mkdir -p "$MOUNT"

# Layer our umount-mountpoint trap on top of the backup-disk trap from
# _backup-mount.sh so both unmounts happen on exit.
trap '
	borg umount "$MOUNT" 2>/dev/null || true
	rmdir "$MOUNT" 2>/dev/null || true
	release_backup_mount
' EXIT

borg mount "$BACKUP_PATH"/immich-borg "$MOUNT"
echo "Borg archive mounted at $MOUNT"
echo "Press Ctrl-D or run 'exit' to unmount and disconnect."

cd "$MOUNT"
exec "$SHELL"
