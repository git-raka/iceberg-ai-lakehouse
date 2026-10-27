-- Streaming ingestion: Kafka topic ebus.telemetry -> Iceberg table bronze.ebus_telemetry.
-- Iceberg commits happen on Flink checkpoints, so new data becomes queryable about every 30 seconds.
SET 'pipeline.name' = 'ebus-telemetry-to-iceberg';
SET 'execution.checkpointing.interval' = '30s';
SET 'table.dml-sync' = 'false';

CREATE CATALOG lakehouse WITH (
  'type' = 'iceberg',
  'catalog-type' = 'rest',
  'uri' = 'http://lakekeeper:8181/catalog',
  'warehouse' = '${ICEBERG_WAREHOUSE}',
  'io-impl' = 'org.apache.iceberg.aws.s3.S3FileIO',
  's3.endpoint' = 'http://seaweedfs:8333',
  's3.path-style-access' = 'true',
  'client.region' = 'us-east-1'
);

CREATE DATABASE IF NOT EXISTS lakehouse.bronze;

CREATE TABLE IF NOT EXISTS lakehouse.bronze.ebus_telemetry (
  bus_id STRING, route_id STRING, event_time TIMESTAMP(6), lat DOUBLE, lon DOUBLE,
  speed_kmh DOUBLE, battery_pct DOUBLE, passengers INT, kafka_partition INT, kafka_offset BIGINT,
  ingested_at TIMESTAMP_LTZ(6)
);

CREATE TEMPORARY TABLE ebus_source (
  bus_id STRING, route_id STRING, ts STRING, lat DOUBLE, lon DOUBLE, speed_kmh DOUBLE,
  battery_pct DOUBLE, passengers INT,
  kafka_partition INT METADATA FROM 'partition' VIRTUAL,
  kafka_offset BIGINT METADATA FROM 'offset' VIRTUAL
) WITH (
  'connector' = 'kafka',
  'topic' = 'ebus.telemetry',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'flink-ebus-to-iceberg',
  'scan.startup.mode' = 'group-offsets',
  'properties.auto.offset.reset' = 'earliest',
  'format' = 'json',
  'json.ignore-parse-errors' = 'true'
);

INSERT INTO lakehouse.bronze.ebus_telemetry
SELECT bus_id, route_id, CAST(TO_TIMESTAMP(ts) AS TIMESTAMP(6)), lat, lon, speed_kmh, battery_pct, passengers,
       kafka_partition, kafka_offset, CURRENT_TIMESTAMP
FROM ebus_source;
