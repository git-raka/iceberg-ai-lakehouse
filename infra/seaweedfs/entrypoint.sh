#!/bin/sh
# Render the S3 identity file from the environment, start SeaweedFS, create the bucket once.
set -eu
mkdir -p /etc/seaweedfs
cat > /etc/seaweedfs/s3.json <<EOF
{"identities":[{"name":"lakehouse","credentials":[{"accessKey":"${S3_ACCESS_KEY}","secretKey":"${S3_SECRET_KEY}"}],
  "actions":["Admin","Read","List","Tagging","Write"]}]}
EOF
weed server -dir=/data -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=1024 -volume.max=0 \
  -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json &
pid=$!
until echo "cluster.check" | weed shell -master=localhost:9333 >/dev/null 2>&1; do sleep 1; done
echo "s3.bucket.create -name ${S3_BUCKET}" | weed shell -master=localhost:9333 >/dev/null 2>&1 || true
echo "SeaweedFS ready, bucket ${S3_BUCKET}"
wait $pid
