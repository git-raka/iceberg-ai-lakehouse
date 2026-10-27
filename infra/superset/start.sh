#!/bin/bash
# Prepare the metadata database and the admin user (idempotent), then run the web server.
set -euo pipefail
superset db upgrade >/dev/null
superset fab create-admin --username admin --firstname Admin --lastname User \
  --email admin@estate.example --password "${SUPERSET_ADMIN_PASSWORD}" >/dev/null 2>&1 || true
superset init >/dev/null
exec /usr/bin/run-server.sh
