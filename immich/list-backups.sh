#!/bin/bash

set -e

source .env

borg info "$BACKUP_PATH"/immich-borg

echo ''
echo '============= List of archives ============='
borg list -v "$BACKUP_PATH"/immich-borg
