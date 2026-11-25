#!/usr/bin/env bash
# Запуск модульних тестів усіх сервісів. Потрібна PostgreSQL з базами flight_db, resource_db, fids_db, acdm_db, task_db
# (за замовчуванням localhost:55432; підняти: scripts/test-db.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
for svc in flight_service resource_service fids_service acdm_service task_service; do
  echo "== $svc"
  (cd "services/$svc" && python -m pytest -q --cov=app --cov-report=term tests)
done
