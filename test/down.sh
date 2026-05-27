#!/bin/bash
# Destroy the LXD test container. Idempotent.

set -e
CT_NAME="${CT_NAME:-testpi}"

if sudo lxc info "$CT_NAME" >/dev/null 2>&1; then
	sudo lxc delete -f "$CT_NAME"
	echo "Container '$CT_NAME' deleted."
else
	echo "Container '$CT_NAME' does not exist; nothing to tear down."
fi
