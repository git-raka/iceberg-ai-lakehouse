#!/bin/bash
# One Postgres instance, one database per concern.
set -euo pipefail
for db in erp lakekeeper dagster superset ai; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $db" >/dev/null
done
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d ai -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
echo "databases ready: erp lakekeeper dagster superset ai(pgvector)"
