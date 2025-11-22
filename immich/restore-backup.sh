#!/bin/bash

set -e

source .env

mkdir /tmp/immich-mountpoint
borg mount "$BACKUP_PATH"/immich-borg /tmp/immich-mountpoint
cd /tmp/immich-mountpoint
