#!/bin/bash
# Run the Spark Connect server in the foreground. S3 credentials come from AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY in the environment (read by Iceberg's S3FileIO), never from the command line.
set -euo pipefail
export SPARK_NO_DAEMONIZE=true
exec /opt/spark/sbin/start-connect-server.sh \
  --conf spark.sql.catalog.lakehouse.warehouse="${ICEBERG_WAREHOUSE}" \
  --conf spark.connect.grpc.binding.port=15002
