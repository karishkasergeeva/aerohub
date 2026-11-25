#!/usr/bin/env bash
# Тестова PostgreSQL у Docker на порту 55432
set -euo pipefail
cd "$(dirname "$0")/.."
docker run -d --rm --name aerohub-testpg -e POSTGRES_USER=aerohub -e POSTGRES_PASSWORD=aerohub -e POSTGRES_DB=aerohub \
  -p 55432:5432 -v "$PWD/db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" postgres:16-alpine
