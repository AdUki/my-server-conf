#!/bin/bash

set -e

source .env

apt install -y borgbackup

mkdir "$UPLOAD_LOCATION/database-backup"
borg init --encryption=none "$BACKUP_PATH/immich-borg"
