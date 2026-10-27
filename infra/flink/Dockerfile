# Flink with the Iceberg (REST catalog, S3FileIO) and Kafka connectors.
FROM flink:2.1.3-scala_2.12-java17

ARG ICEBERG_VERSION=1.12.0
ARG KAFKA_CONNECTOR=5.0.0-2.1
ARG HADOOP_VERSION=3.4.2
ARG M=https://repo1.maven.org/maven2
RUN cd /opt/flink/lib \
 && wget -q $M/org/apache/iceberg/iceberg-flink-runtime-2.1/${ICEBERG_VERSION}/iceberg-flink-runtime-2.1-${ICEBERG_VERSION}.jar \
 && wget -q $M/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar \
 && wget -q $M/org/apache/flink/flink-sql-connector-kafka/${KAFKA_CONNECTOR}/flink-sql-connector-kafka-${KAFKA_CONNECTOR}.jar \
 && wget -q $M/org/apache/hadoop/hadoop-client-api/${HADOOP_VERSION}/hadoop-client-api-${HADOOP_VERSION}.jar \
 && wget -q $M/org/apache/hadoop/hadoop-client-runtime/${HADOOP_VERSION}/hadoop-client-runtime-${HADOOP_VERSION}.jar
COPY ebus_to_iceberg.sql /opt/sql/ebus_to_iceberg.sql.tpl
COPY submit.sh /opt/sql/submit.sh
