#!/bin/bash
set -e
SCRIPT_DIR="$(realpath "$(dirname "$0")")"

stacks=(
	immich
	nginxproxymanager
	system-monitor
)

for stack in "${stacks[@]}"; do
	dir="$SCRIPT_DIR/$stack"
	if [ ! -f "$dir/docker-compose.yml" ]; then
		echo "==> Skipping $stack (no docker-compose.yml)"
		continue
	fi
	echo "==> Updating $stack"
	( cd "$dir" && docker compose pull && docker compose up --remove-orphans -d )
done

echo "==> Pruning unused images"
docker image prune -f
