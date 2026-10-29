#!/bin/bash
# Show what writes to the root filesystem (the NVMe). Usage: rootfs-writes.sh [seconds]
#
# Prints the bytes written to the root block device over the interval and, via
# fatrace, which process wrote which file. Bind mounts and other filesystems
# (/media/data, /var/log on the HDD, tmpfs /tmp) are excluded, so everything
# listed really hits the NVMe. Run it for a few minutes on an idle server:
# anything that shows up every run is a candidate for RAM or the HDD.
set -e
SECS="${1:-60}"
PATH="$PATH:/usr/sbin:/sbin"   # fatrace lives in /usr/sbin

command -v fatrace >/dev/null || { echo "fatrace missing: sudo apt-get install fatrace" >&2; exit 1; }

ROOTDEV=$(findmnt -no SOURCE /)
PART=$(basename "$ROOTDEV")
sectors_written() { awk -v p="$PART" '$3==p {print $10}' /proc/diskstats; }

before=$(sectors_written)
echo "Watching writes to / ($ROOTDEV) for ${SECS}s ..."
# -c: only the filesystem of the current directory. W: write, O: open-for-write, C: create, D: delete.
out=$(cd / && sudo fatrace -c -f WCD -s "$SECS" 2>/dev/null || true)
after=$(sectors_written)

echo
echo "Bytes written to $ROOTDEV: $(( (after - before) * 512 ))"
echo
if [ -z "$out" ]; then
	echo "No file writes seen on /."
	exit 0
fi
echo "Writers (count  process  file):"
printf '%s\n' "$out" | awk '{print $1, $3}' | sort | uniq -c | sort -rn | head -40
