# One Python image for the pipeline (Dagster), the Data/AI API, the MCP server and the simulators.
FROM python:3.12.16-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[pipeline,dev]"

ENV PYTHONUNBUFFERED=1 DAGSTER_HOME=/app/.dagster
RUN mkdir -p $DAGSTER_HOME && cp src/lakehouse/orchestration/dagster.yaml $DAGSTER_HOME/dagster.yaml \
 && useradd --create-home --uid 1000 app && chown -R app /app
USER app
