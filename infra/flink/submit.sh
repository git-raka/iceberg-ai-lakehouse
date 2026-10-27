#!/bin/bash
# Submit the streaming job once: skip if a job with the same name is already active.
# S3 credentials are not in the SQL: Iceberg's S3FileIO reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from the
# environment of the Flink processes, so they never appear in the SQL client's echo or the job graph.
set -euo pipefail
REST=http://flink-jobmanager:8081
JOB=ebus-telemetry-to-iceberg
until curl -fsS "$REST/overview" >/dev/null 2>&1; do sleep 2; done
# one job object per line, then look for this job name in an active state
if curl -fsS "$REST/jobs/overview" | sed 's/{"jid"/\n{"jid"/g' | grep "\"name\":\"$JOB\"" \
    | grep -qE '"state":"(RUNNING|CREATED|INITIALIZING|RESTARTING|RECONCILING)"'; then
  echo "streaming job $JOB already active"; exit 0
fi
sed -e "s|\${ICEBERG_WAREHOUSE}|${ICEBERG_WAREHOUSE}|g" /opt/sql/ebus_to_iceberg.sql.tpl > /tmp/job.sql
/opt/flink/bin/sql-client.sh -Drest.address=flink-jobmanager -Drest.port=8081 -f /tmp/job.sql
rm -f /tmp/job.sql
