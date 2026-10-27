# Spark Connect server with the Iceberg runtime. Clients (Dagster) talk to it over sc://spark:15002,
# so they need only the thin `pyspark-client` package, not a JVM.
FROM apache/spark:4.1.3-scala2.13-java17-python3-ubuntu

ARG ICEBERG_VERSION=1.12.0
ARG MAVEN=https://repo1.maven.org/maven2/org/apache/iceberg
USER root
RUN cd /opt/spark/jars \
 && wget -q ${MAVEN}/iceberg-spark-runtime-4.1_2.13/${ICEBERG_VERSION}/iceberg-spark-runtime-4.1_2.13-${ICEBERG_VERSION}.jar \
 && wget -q ${MAVEN}/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar
COPY spark-defaults.conf /opt/spark/conf/spark-defaults.conf
COPY entrypoint.sh /opt/entrypoint-connect.sh
RUN chmod +x /opt/entrypoint-connect.sh
USER spark
ENTRYPOINT ["/opt/entrypoint-connect.sh"]
