#!/bin/sh
# Idempotent: bootstrap the Lakekeeper server and create the Iceberg warehouse on SeaweedFS.
set -eu
API=http://lakekeeper:8181
until curl -fsS "$API/health" >/dev/null 2>&1; do sleep 1; done

curl -fsS -X POST "$API/management/v1/bootstrap" -H 'Content-Type: application/json' \
  -d '{"accept-terms-of-use": true}' >/dev/null 2>&1 || true

if curl -fsS "$API/management/v1/warehouse" | grep -q "\"name\":\"${ICEBERG_WAREHOUSE}\""; then
  echo "warehouse ${ICEBERG_WAREHOUSE} already exists"; exit 0
fi
curl -fsS -X POST "$API/management/v1/warehouse" -H 'Content-Type: application/json' -d @- <<EOF
{
  "warehouse-name": "${ICEBERG_WAREHOUSE}",
  "storage-profile": {
    "type": "s3", "bucket": "${S3_BUCKET}", "key-prefix": "warehouse",
    "region": "us-east-1", "endpoint": "http://seaweedfs:8333",
    "path-style-access": true, "flavor": "s3-compat", "sts-enabled": false,
    "remote-signing-enabled": false
  },
  "storage-credential": {
    "type": "s3", "credential-type": "access-key",
    "aws-access-key-id": "${S3_ACCESS_KEY}", "aws-secret-access-key": "${S3_SECRET_KEY}"
  }
}
EOF
echo
echo "warehouse ${ICEBERG_WAREHOUSE} created"
