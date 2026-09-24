#!/bin/bash

cd "$(dirname "$0")/.." || exit 1
docker compose pull && docker compose down --remove-orphans && docker compose up -d --remove-orphans
