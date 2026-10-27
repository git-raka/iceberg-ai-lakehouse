"""Edge ingestion into the Bronze layer (raw, append-only, with ingestion metadata).

- ERP tables: change data capture by `updated_at` watermark (only rows changed since the last run)
- Landing files (weighbridge CSV, service-desk JSON lines): each file is ingested once, tracked in a log table
- Documents: content-addressed copies in the documents volume; a changed file becomes a new version
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.json as pajson

from ..settings import settings
from ..storage import append, get_bytes, list_files, put_bytes, read_arrow, s3_key, table_exists

ERP_TABLES = ["plot", "tenant", "employee", "asset", "purchase_order"]
TS = pa.timestamp("us", tz="UTC")


def _arrow_type(data_type: str, precision: int | None, scale: int | None) -> pa.DataType:
    return {
        "text": pa.string(), "character varying": pa.string(), "integer": pa.int32(), "bigint": pa.int64(),
        "date": pa.date32(), "timestamp with time zone": TS, "boolean": pa.bool_(),
    }.get(data_type) or (pa.decimal128(precision, scale) if data_type == "numeric" else pa.string())


def _meta(n: int, batch: str, now: datetime) -> dict[str, pa.Array]:
    return {"_batch_id": pa.array([batch] * n), "_ingested_at": pa.array([now] * n, type=TS)}


def ingest_erp() -> dict[str, int]:
    """CDC: copy rows whose updated_at is newer than the newest already in Bronze."""
    batch, now, counts = uuid.uuid4().hex[:12], datetime.now(timezone.utc), {}
    with psycopg.connect(settings().pg_dsn("erp")) as conn:
        for table in ERP_TABLES:
            cols = conn.execute(
                "SELECT column_name, data_type, numeric_precision, numeric_scale FROM information_schema.columns "
                "WHERE table_name=%s ORDER BY ordinal_position", (table,)).fetchall()
            target = f"bronze.erp_{table}"
            watermark = None
            if table_exists(target):
                done = read_arrow(target, columns=("updated_at",))
                watermark = pc.max(done["updated_at"]).as_py() if done.num_rows else None
            sql = f"SELECT * FROM {table}" + (" WHERE updated_at > %s" if watermark else "")
            rows = conn.execute(sql, (watermark,) if watermark else ()).fetchall()
            schema = pa.schema([(c, _arrow_type(t, p, s)) for c, t, p, s in cols])
            data = pa.Table.from_pylist([dict(zip(schema.names, r)) for r in rows], schema=schema)
            for name, arr in _meta(data.num_rows, batch, now).items():
                data = data.append_column(name, arr)
            counts[table] = append(target, data)
    return counts


WEIGHBRIDGE_SCHEMA = pa.schema([("ticket_no", pa.string()), ("tenant_id", pa.string()), ("truck_plate", pa.string()),
                                ("gross_kg", pa.int64()), ("tare_kg", pa.int64()), ("weighed_at", pa.string())])
TICKET_SCHEMA = pa.schema([("ticket_id", pa.string()), ("tenant_id", pa.string()), ("category", pa.string()),
                           ("priority", pa.string()), ("opened_at", pa.string()), ("closed_at", pa.string()),
                           ("status", pa.string()), ("description", pa.string())])
FILE_LOG = "bronze.ingest_file_log"


def _done_files() -> set[tuple[str, int]]:
    if not table_exists(FILE_LOG):
        return set()
    log = read_arrow(FILE_LOG, columns=("path", "mtime_ns"))
    return set(zip(log["path"].to_pylist(), log["mtime_ns"].to_pylist()))


def ingest_landing_files() -> dict[str, int]:
    """Read new or changed CSV/JSON files from the landing zone into Bronze."""
    batch, now, done = uuid.uuid4().hex[:12], datetime.now(timezone.utc), _done_files()
    counts, log = {"weighbridge": 0, "ticket": 0}, []
    for source, target, schema, reader in [
        ("weighbridge", "bronze.weighbridge", WEIGHBRIDGE_SCHEMA,
         lambda b, sc: pacsv.read_csv(pa.BufferReader(b), convert_options=pacsv.ConvertOptions(column_types=sc))),
        ("ticketing", "bronze.ticket", TICKET_SCHEMA,
         lambda b, sc: pajson.read_json(pa.BufferReader(b), parse_options=pajson.ParseOptions(explicit_schema=sc))),
    ]:
        for f in list_files(s3_key("landing", source)):
            if (f.path, f.mtime_ns) in done:
                continue
            data = reader(get_bytes(f.path), schema).select(schema.names)
            data = data.append_column("_source_file", pa.array([f.path] * data.num_rows))
            for name, arr in _meta(data.num_rows, batch, now).items():
                data = data.append_column(name, arr)
            counts["weighbridge" if source == "weighbridge" else "ticket"] += append(target, data)
            log.append({"path": f.path, "mtime_ns": f.mtime_ns, "rows": data.num_rows, "ingested_at": now})
    if log:
        append(FILE_LOG, pa.Table.from_pylist(log, schema=pa.schema(
            [("path", pa.string()), ("mtime_ns", pa.int64()), ("rows", pa.int64()), ("ingested_at", TS)])))
    return counts


DOC_SCHEMA = pa.schema([
    ("doc_id", pa.string()), ("file_name", pa.string()), ("tenant_id", pa.string()), ("doc_kind", pa.string()),
    ("volume_path", pa.string()), ("size_bytes", pa.int64()), ("sha256", pa.string()), ("_ingested_at", TS),
])


def ingest_documents() -> int:
    """Copy new or changed PDFs into the documents volume (one immutable copy per version)."""
    known = set()
    if table_exists("bronze.document"):
        known = set(read_arrow("bronze.document", columns=("sha256",))["sha256"].to_pylist())
    now, rows = datetime.now(timezone.utc), []
    for f in list_files(s3_key("landing", "documents")):
        data = get_bytes(f.path)
        sha = hashlib.sha256(data).hexdigest()
        if sha in known:
            continue
        name = f.path.rsplit("/", 1)[-1]
        tenant_id, kind = name.split("_", 1)[0], name.split("_", 1)[1].rsplit(".", 1)[0]
        volume_key = s3_key("volumes", "documents", tenant_id, f"{sha[:16]}_{name}")
        put_bytes(volume_key, data)
        rows.append({"doc_id": sha[:16], "file_name": name, "tenant_id": tenant_id, "doc_kind": kind,
                     "volume_path": "s3://" + volume_key, "size_bytes": len(data), "sha256": sha, "_ingested_at": now})
        known.add(sha)
    return append("bronze.document", pa.Table.from_pylist(rows, schema=DOC_SCHEMA))
