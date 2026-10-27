"""AI layer B: semantic search over documents.

Chunks live in Iceberg (`gold.document_chunk`, the source of truth, governed by Trino) and their
embeddings in pgvector (`ai.doc_chunk`, a rebuildable index). A search first asks Trino, as the
caller, which documents the caller may see, then ranks only those chunks by vector similarity.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pyarrow as pa
from pgvector import Vector
from pgvector.psycopg import register_vector

from ..settings import settings
from ..storage import append, read_arrow, table_exists
from . import llm
from .trino_client import query

CHUNK_SIZE, OVERLAP, DIM = 800, 120, 768
CHUNK_SCHEMA = pa.schema([
    ("chunk_id", pa.string()), ("doc_id", pa.string()), ("tenant_id", pa.string()), ("file_name", pa.string()),
    ("chunk_no", pa.int32()), ("text", pa.string()), ("created_at", pa.timestamp("us", tz="UTC")),
])
DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS doc_chunk (
  chunk_id text PRIMARY KEY, doc_id text NOT NULL, tenant_id text, file_name text, chunk_no int,
  text text, model text, embedding vector({DIM}) NOT NULL);
CREATE INDEX IF NOT EXISTS doc_chunk_hnsw ON doc_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS doc_chunk_doc ON doc_chunk (doc_id);
"""


def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """Split on paragraph/line boundaries into pieces of about `size` characters with some overlap."""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            end = cut if cut > 0 else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in out if c]


def _connect():
    conn = psycopg.connect(settings().pg_dsn("ai"), autocommit=True)
    conn.execute(DDL)
    register_vector(conn)
    return conn


def index_documents() -> dict[str, int]:
    """Chunk + embed every document text that is not indexed yet."""
    if not table_exists("silver.document_text"):
        return {"documents": 0, "chunks": 0}
    texts = read_arrow("silver.document_text").to_pylist()
    known = set()
    if table_exists("gold.document_chunk"):
        known = set(read_arrow("gold.document_chunk", columns=("doc_id",))["doc_id"].to_pylist())
    todo = [t for t in texts if t["doc_id"] not in known]
    now, rows = datetime.now(timezone.utc), []
    with _connect() as conn:
        restored = _restore_missing(conn) if known else 0
        for t in todo:
            pieces = chunk(t["text"])
            if not pieces:
                continue
            vectors = llm.embed([f"search_document: {p}" for p in pieces])
            for i, (piece, vec) in enumerate(zip(pieces, vectors)):
                cid = f"{t['doc_id']}-{i:03d}"
                rows.append({"chunk_id": cid, "doc_id": t["doc_id"], "tenant_id": t["tenant_id"],
                             "file_name": t["file_name"], "chunk_no": i, "text": piece, "created_at": now})
                conn.execute(
                    "INSERT INTO doc_chunk VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO UPDATE "
                    "SET text = EXCLUDED.text, embedding = EXCLUDED.embedding, model = EXCLUDED.model",
                    (cid, t["doc_id"], t["tenant_id"], t["file_name"], i, piece, settings().embed_model, Vector(vec)))
    append("gold.document_chunk", pa.Table.from_pylist(rows, schema=CHUNK_SCHEMA))
    return {"documents": len({r["doc_id"] for r in rows}), "chunks": len(rows), "restored_vectors": restored}


def _restore_missing(conn) -> int:
    """Iceberg is the source of truth: re-embed chunks that are in gold.document_chunk but not in pgvector
    (a new Postgres, a dropped table, a different embedding model)."""
    have = {r[0] for r in conn.execute("SELECT chunk_id FROM doc_chunk WHERE model = %s", (settings().embed_model,))}
    missing = [c for c in read_arrow("gold.document_chunk").to_pylist() if c["chunk_id"] not in have]
    for i in range(0, len(missing), 32):
        batch = missing[i:i + 32]
        for c, vec in zip(batch, llm.embed([f"search_document: {c['text']}" for c in batch])):
            conn.execute(
                "INSERT INTO doc_chunk VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO UPDATE "
                "SET text = EXCLUDED.text, embedding = EXCLUDED.embedding, model = EXCLUDED.model",
                (c["chunk_id"], c["doc_id"], c["tenant_id"], c["file_name"], c["chunk_no"], c["text"],
                 settings().embed_model, Vector(vec)))
    return len(missing)


def allowed_doc_ids(user: str) -> list[str]:
    """Ask Trino, as the user, which documents they may see (row filters decide); latest version per file."""
    sql = """SELECT doc_id FROM (
               SELECT doc_id, rank() OVER (PARTITION BY tenant_id, file_name ORDER BY created_at DESC) AS rnk
               FROM (SELECT DISTINCT doc_id, tenant_id, file_name, created_at FROM gold.document_chunk))
             WHERE rnk = 1"""
    return [r[0] for r in query(sql, user=user).rows]


def search(question: str, user: str, k: int = 5) -> list[dict]:
    """Top-k chunks by cosine similarity, restricted to documents the user may see."""
    allowed = allowed_doc_ids(user)
    if not allowed:
        return []
    vec = Vector(llm.embed([f"search_query: {question}"], purpose="search", user=user)[0])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, tenant_id, file_name, chunk_no, text, 1 - (embedding <=> %s) AS score "
            "FROM doc_chunk WHERE doc_id = ANY(%s) ORDER BY embedding <=> %s LIMIT %s",
            (vec, allowed, vec, k)).fetchall()
    return [{"doc_id": r[0], "tenant_id": r[1], "file_name": r[2], "chunk_no": r[3], "text": r[4],
             "score": round(float(r[5]), 4)} for r in rows]
