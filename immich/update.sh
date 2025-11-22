#!/bin/sh
./stop.sh
docker compose pull && docker compose up -d
