#!/bin/bash
# Run setup.sh twice inside the LXD test container and assert the second run
# is a no-op. Exits non-zero on any failure.

set -e

CT_NAME="${CT_NAME:-testpi}"
REMOTE_REPO="/home/pi/my-server-conf"

lxc() { sudo lxc "$@"; }

if ! lxc info "$CT_NAME" >/dev/null 2>&1; then
	echo "Container '$CT_NAME' does not exist. Run test/up.sh first." >&2
	exit 1
fi

ct_run_pi()   { lxc exec "$CT_NAME" -- sudo -iu pi bash -c "export SETUP_SKIP_NPM_SEED=1; $1"; }
ct_run_root() { lxc exec "$CT_NAME" -- bash -c "$1"; }

echo "============================================================"
echo "  Run 1: initial setup.sh on a clean container"
echo "============================================================"
ct_run_pi "cd $REMOTE_REPO && ./setup.sh 2>&1 | tee /tmp/run1.log; exit \${PIPESTATUS[0]}" || {
	echo "FAIL: first setup.sh run exited non-zero (last 20 lines:)" >&2
	ct_run_root "tail -20 /tmp/run1.log" >&2
	exit 1
}
echo "  Run 1 final line:"
ct_run_root "tail -1 /tmp/run1.log"

echo
echo "Snapshotting /etc, /lib/systemd/system and repo git status before run 2..."
ct_run_root "
	rm -rf /tmp/etc-before /tmp/sys-before
	cp -a /etc /tmp/etc-before
	cp -a /lib/systemd/system /tmp/sys-before
	cd $REMOTE_REPO && git status --porcelain | sort > /tmp/git-before
"

echo
echo "============================================================"
echo "  Run 2: re-run setup.sh, expect no changes"
echo "============================================================"
ct_run_pi "cd $REMOTE_REPO && ./setup.sh 2>&1 | tee /tmp/run2.log; exit \${PIPESTATUS[0]}" || {
	echo "FAIL: second setup.sh run exited non-zero (last 20 lines:)" >&2
	ct_run_root "tail -20 /tmp/run2.log" >&2
	exit 1
}
echo "  Run 2 final line:"
ct_run_root "tail -1 /tmp/run2.log"

echo
echo "Diffing /etc..."
etc_diff=$(ct_run_root "diff -rq /tmp/etc-before /etc 2>/dev/null | grep -v -E 'log|cache|apt/lists|machine-id|resolv\\.conf' || true")
if [ -n "$etc_diff" ]; then
	echo "FAIL: /etc changed across runs:" >&2
	echo "$etc_diff" >&2
	exit 1
fi

echo "Diffing /lib/systemd/system..."
sys_diff=$(ct_run_root "diff -rq /tmp/sys-before /lib/systemd/system 2>/dev/null || true")
if [ -n "$sys_diff" ]; then
	echo "FAIL: systemd units changed across runs:" >&2
	echo "$sys_diff" >&2
	exit 1
fi

echo "Checking repo working tree (diff vs pre-run-2 snapshot)..."
# Snapshot was taken BEFORE run 2; the harness pre-seeded the container with
# the host's current (uncommitted) state, so an "always-empty status" check is
# not meaningful here. We only care that run 2 didn't introduce new changes.
ct_run_root "cd $REMOTE_REPO && git status --porcelain | sort > /tmp/git-after"
# /tmp/git-before was captured at snapshot time (see above)
tree_diff=$(ct_run_root "diff /tmp/git-before /tmp/git-after 2>/dev/null || true")
if [ -n "$tree_diff" ]; then
	echo "FAIL: setup.sh introduced new repo working-tree changes during run 2:" >&2
	echo "$tree_diff" >&2
	exit 1
fi

echo
echo "============================================================"
echo "  PASS — setup.sh is idempotent in the LXD container"
echo "============================================================"
