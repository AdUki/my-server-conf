#!/bin/bash
# Bring up (or reuse) an LXD container that mimics the Pi closely enough to
# exercise setup.sh. The repo is *pushed* into the container (not bind-mounted)
# so the container can freely modify its copy without touching host ownership.
# Re-run up.sh to re-push after editing on the host.
#
# Why LXD (not Multipass)? Multipass + QEMU is broken on many Ubuntu 24.04
# hosts (VM never reaches Running state — known class of upstream issues).
# LXD is faster, has Docker support via security.nesting, and is reliable.

set -e

CT_NAME="${CT_NAME:-testpi}"
IMAGE="${LXD_IMAGE:-ubuntu:22.04}"
TEST_DIR="$(realpath "$(dirname "$0")")"
REPO_DIR="$(realpath "$TEST_DIR/..")"
REMOTE_REPO="/home/pi/my-server-conf"

lxc() { sudo lxc "$@"; }

if ! sudo -n true 2>/dev/null && ! command -v lxc >/dev/null; then
	echo "Cannot reach LXD. Install with:  sudo snap install lxd && sudo lxd init --auto" >&2
	exit 1
fi

# Ensure the default profile has a root disk + a NIC.
if ! sudo lxc profile show default | grep -q 'type: disk'; then
	pool=$(sudo lxc storage list -fcsv 2>/dev/null | head -n1 | cut -d, -f1)
	[ -z "$pool" ] && { echo "No LXD storage pool exists — run 'sudo lxc storage create default dir'" >&2; exit 1; }
	sudo lxc profile device add default root disk path=/ pool="$pool"
fi
if ! sudo lxc profile show default | grep -q 'type: nic'; then
	sudo lxc network show lxdbr0 >/dev/null 2>&1 || sudo lxc network create lxdbr0
	sudo lxc profile device add default eth0 nic name=eth0 network=lxdbr0
fi

# If Tailscale is installed it routes 10.0.0.0/8 via tailscale0, which steals
# LXD container traffic. Insert a higher-priority rule routing lxdbr0's subnet
# back through the main table. Idempotent.
lxd_subnet=$(sudo lxc network get lxdbr0 ipv4.address 2>/dev/null | awk -F/ '{print $1}' | awk -F. '{print $1"."$2"."$3".0/24"}')
if [ -n "$lxd_subnet" ] && ! ip rule show priority 100 2>/dev/null | grep -q "$lxd_subnet"; then
	echo "Adding routing rule so $lxd_subnet bypasses Tailscale (priority 100)..."
	sudo ip rule add to "$lxd_subnet" lookup main priority 100
fi

# Has the container already been created?
if lxc info "$CT_NAME" >/dev/null 2>&1; then
	echo "Container '$CT_NAME' already exists; reusing."
	lxc start "$CT_NAME" 2>/dev/null || true
else
	echo "Launching '$CT_NAME' from $IMAGE..."
	lxc launch "$IMAGE" "$CT_NAME" \
		-c security.nesting=true \
		-c security.privileged=true
fi

# Pass the host's /dev/kmsg through to the container. cadvisor (part of the
# monitoring stack) requires it; LXD containers don't have it by default.
lxc config device add "$CT_NAME" kmsg unix-char source=/dev/kmsg path=/dev/kmsg 2>/dev/null || true

# Wait for cloud-init to finish so apt isn't locked when we run setup.sh.
echo "Waiting for cloud-init to finish inside the container..."
for i in $(seq 1 60); do
	if lxc exec "$CT_NAME" -- cloud-init status --wait >/dev/null 2>&1; then
		break
	fi
	sleep 2
done

# Override systemd-resolved with a static resolv.conf so DNS works reliably
# for apt and `docker pull` regardless of LXD's resolver state.
lxc exec "$CT_NAME" -- bash -c '
	rm -f /etc/resolv.conf
	cat > /etc/resolv.conf <<EOF
nameserver 1.1.1.1
nameserver 8.8.8.8
EOF
	chattr +i /etc/resolv.conf 2>/dev/null || true
'

# Create a 'pi' user (rename the default 'ubuntu' user if present so the
# image's pre-baked groups carry over). Add passwordless sudo.
lxc exec "$CT_NAME" -- bash -c '
	set -e
	if id pi >/dev/null 2>&1; then
		:
	elif id ubuntu >/dev/null 2>&1; then
		usermod -l pi -d /home/pi -m ubuntu
		groupmod -n pi ubuntu 2>/dev/null || true
	else
		useradd -m -s /bin/bash pi
	fi
	usermod -aG sudo pi
	echo "pi ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-pi-nopasswd
	chmod 440 /etc/sudoers.d/90-pi-nopasswd
'

# Push the repo into the container. Fast (~MB) so iteration is still snappy:
# edit on host → re-run up.sh → re-push → test/setup.sh or idempotency.sh.
echo "Pushing repo into container at $REMOTE_REPO..."
lxc exec "$CT_NAME" -- rm -rf "$REMOTE_REPO"
lxc exec "$CT_NAME" -- mkdir -p "$REMOTE_REPO"
# Tar streaming is much faster than `lxc file push -r` for many small files.
tar -C "$REPO_DIR" -cf - --exclude='.git/objects/pack' --exclude='test/.cache' . \
	| lxc exec "$CT_NAME" -- tar -C "$REMOTE_REPO" -xf -
lxc exec "$CT_NAME" -- chown -R pi:pi "$REMOTE_REPO"

# Inject a test-only config.local.conf so setup.sh has values for the required
# secrets. Real deployments place this file by hand on the target machine;
# the harness fabricates one with throwaway values.
lxc exec "$CT_NAME" -- bash -c "cat > $REMOTE_REPO/config.local.conf <<'EOF'
SAMBA_PI_PASSWORD=test-samba-pw
NPM_ADMIN_EMAIL=admin@example.com
NPM_ADMIN_PASSWORD=test-npm-pw
DUCKDNS_TOKEN=00000000-0000-0000-0000-000000000000
IMMICH_DB_PASSWORD=test-immich-db
IMMICH_ADMIN_EMAIL=admin@example.com
IMMICH_ADMIN_PASSWORD=test-immich-admin
IMMICH_API_KEY=test-api-key
EOF
chown pi:pi $REMOTE_REPO/config.local.conf
chmod 600 $REMOTE_REPO/config.local.conf"

echo
echo "Container ready. Next steps:"
echo "  bash test/setup.sh                             # run ./setup.sh, leave running, print URLs"
echo "  bash test/verify.sh                            # run ./setup.sh twice, check no-op idempotency"
echo "  bash test/down.sh                              # tear down the container"
echo "  sudo lxc shell $CT_NAME                        # root shell inside the container"
echo "  sudo lxc exec $CT_NAME -- sudo -iu pi bash     # pi shell"
