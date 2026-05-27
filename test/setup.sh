#!/bin/bash
# Run the project's ./setup.sh once inside the test container, leave the
# container up, and print URLs you can open in a browser to verify the
# services are actually working.

set -e

CT_NAME="${CT_NAME:-testpi}"
REMOTE_REPO="/home/pi/my-server-conf"

lxc() { sudo lxc "$@"; }

if ! lxc info "$CT_NAME" >/dev/null 2>&1; then
	echo "Container '$CT_NAME' doesn't exist. Run test/up.sh first." >&2
	exit 1
fi

echo "Running setup.sh in $CT_NAME..."
lxc exec "$CT_NAME" -- sudo -iu pi bash -c "export SETUP_SKIP_NPM_SEED=1; cd $REMOTE_REPO && ./setup.sh"

# Pick the eth0 address specifically — `lxc list` lumps every interface's IP
# into one CSV field (including Docker's internal 172.x.x.x bridges) and
# quotes the multi-line value, which would yield a junk IP if we just grabbed
# the first line.
ip=$(lxc exec "$CT_NAME" -- ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)

cat <<EOF

============================================================
  setup.sh finished. The container is still running.

  Services reachable from your browser on the host:

    NPM landing page : http://$ip/
    NPM admin UI     : http://$ip:81/
    Immich           : http://$ip:2283/
    Grafana          : http://$ip:3000/
    Prometheus       : http://$ip:9090/

  Drop into the container by hand:
    sudo lxc shell $CT_NAME
    sudo lxc exec $CT_NAME -- sudo -iu pi bash

  Tear down when done:
    bash test/down.sh
============================================================
EOF
