"""Dagster: the pipeline as assets with lineage, data-quality checks and schedules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dagster as dg

from ..ai import documents, vectors
from ..pipeline import bronze, transform
from ..ai.trino_client import query


@dg.asset(group_name="bronze", description="ERP tables, change data capture by updated_at watermark")
def erp_bronze() -> dg.MaterializeResult:
    counts = bronze.ingest_erp()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in counts.items()})


@dg.asset(group_name="bronze", description="Weighbridge CSV and service-desk JSON files from the landing zone")
def landing_bronze() -> dg.MaterializeResult:
    counts = bronze.ingest_landing_files()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in counts.items()})


@dg.asset(group_name="bronze", description="PDF documents copied into the documents volume, one version per content hash")
def documents_bronze() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"new_versions": dg.MetadataValue.int(bronze.ingest_documents())})


@dg.asset(group_name="silver", deps=[erp_bronze, landing_bronze],
          description="Cleaned, de-duplicated tables; ERP merged latest-version-wins (Spark MERGE INTO)")
def silver_tables() -> dg.MaterializeResult:
    counts = transform.silver()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in counts.items()})


@dg.asset(group_name="ai", deps=[documents_bronze], description="AI-A: text layer or OCR for every document version")
def document_text() -> dg.MaterializeResult:
    documents.ensure_tables()
    return dg.MaterializeResult(metadata={"documents": dg.MetadataValue.int(documents.extract_text())})


@dg.asset(group_name="ai", deps=[document_text],
          description="AI-A: fields extracted by the local LLM (JSON schema), validated, with a review queue")
def document_fields() -> dg.MaterializeResult:
    r = documents.extract_fields()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in r.items()})


@dg.asset(group_name="gold", deps=[silver_tables, document_fields], description="Business-ready aggregates")
def gold_tables() -> dg.MaterializeResult:
    counts = transform.gold()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in counts.items()})


@dg.asset(group_name="ai", deps=[document_text, gold_tables],
          description="AI-B: chunks in Iceberg (gold.document_chunk) + embeddings in pgvector")
def document_index() -> dg.MaterializeResult:
    r = vectors.index_documents()
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.int(v) for k, v in r.items()})


# ----------------------------------------------------------------------------------- data quality

def _one(sql: str):
    return query(sql, user="pipeline").rows[0]


@dg.asset_check(asset=silver_tables, description="Less than 5% of weighbridge tickets have net weight <= 0")
def weighbridge_valid_ratio() -> dg.AssetCheckResult:
    total, invalid = _one("SELECT count(*), count_if(NOT is_valid) FROM silver.weighbridge")
    ratio = invalid / total if total else 0
    return dg.AssetCheckResult(passed=ratio < 0.05, metadata={"invalid_ratio": round(ratio, 4), "rows": total})


@dg.asset_check(asset=silver_tables, description="E-bus telemetry is fresh (newest reading under 10 minutes old)")
def iot_freshness() -> dg.AssetCheckResult:
    try:
        (latest,) = _one("SELECT max(event_time) FROM silver.ebus_telemetry")
    except Exception as e:  # table not there yet
        return dg.AssetCheckResult(passed=False, metadata={"error": str(e)[:200]})
    if latest is None:
        return dg.AssetCheckResult(passed=False, metadata={"latest": "none"})
    age = datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(str(latest)).replace(tzinfo=None)
    return dg.AssetCheckResult(passed=age < timedelta(minutes=10), metadata={"age_minutes": round(age.total_seconds() / 60, 1)})


# on gold_tables, not document_fields: it reads gold.contract_expiry, which gold_tables builds
@dg.asset_check(asset=gold_tables, description="Leases whose AI extraction needs a human review (warning only)")
def ai_review_queue() -> dg.AssetCheckResult:
    (n,) = _one("SELECT count(*) FROM gold.contract_expiry WHERE needs_review")
    return dg.AssetCheckResult(passed=n == 0, severity=dg.AssetCheckSeverity.WARN, metadata={"needs_review": n})


@dg.asset_check(asset=gold_tables, description="Every active tenant has a lease with an end date")
def contract_completeness() -> dg.AssetCheckResult:
    (missing,) = _one("SELECT count(*) FROM gold.tenant_overview WHERE lease_end IS NULL")
    return dg.AssetCheckResult(passed=missing == 0, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"tenants_without_lease_end": missing})


@dg.op
def maintain_tables() -> dict:
    return transform.maintenance()


@dg.job(description="Compact data files and expire snapshots older than 7 days")
def maintenance_job():
    maintain_tables()


pipeline_job = dg.define_asset_job("lakehouse_pipeline", selection=dg.AssetSelection.all())

defs = dg.Definitions(
    assets=[erp_bronze, landing_bronze, documents_bronze, silver_tables, document_text, document_fields,
            gold_tables, document_index],
    asset_checks=[weighbridge_valid_ratio, iot_freshness, ai_review_queue, contract_completeness],
    jobs=[pipeline_job, maintenance_job],
    schedules=[dg.ScheduleDefinition(job=pipeline_job, cron_schedule="*/30 * * * *",
                                     default_status=dg.DefaultScheduleStatus.RUNNING),
               dg.ScheduleDefinition(job=maintenance_job, cron_schedule="0 3 * * 0",
                                     default_status=dg.DefaultScheduleStatus.RUNNING)],
)
