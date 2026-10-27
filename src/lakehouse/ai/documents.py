"""AI layer A: turn unstructured documents into governed, structured data.

1. Text: take the PDF text layer, or OCR the page images when there is none (scanned documents).
2. Fields: a local LLM extracts typed fields with a JSON schema; the result is validated with plain rules
   (dates parse, end after start, tenant matches the file) and anything doubtful goes to a review queue.
Every row carries lineage: parser/prompt version, model name and extraction time, so results can be
audited and re-extracted when the prompt or model changes.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timezone

import pyarrow as pa
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes

from ..settings import settings
from ..storage import append, get_bytes, read_arrow, table_exists
from . import llm

PARSER_VERSION = "text-or-ocr-1"
PROMPT_VERSION = "fields-v2"  # v2: the model quotes the fee text; code converts it (v1 asked the model for a number)
FEE_RANGE_IDR = (1_000_000, 100_000_000_000)  # outside this range a fee is implausible for a monthly estate fee
TS = pa.timestamp("us", tz="UTC")

TEXT_SCHEMA = pa.schema([
    ("doc_id", pa.string()), ("file_name", pa.string()), ("tenant_id", pa.string()), ("doc_kind", pa.string()),
    ("text", pa.string()), ("chars", pa.int32()), ("method", pa.string()), ("parser_version", pa.string()),
    ("extracted_at", TS),
])
FIELDS_SCHEMA = pa.schema([
    ("doc_id", pa.string()), ("file_name", pa.string()), ("tenant_id", pa.string()), ("doc_type", pa.string()),
    ("party_name", pa.string()), ("party_tenant_id", pa.string()), ("plot_id", pa.string()),
    ("lease_start", pa.date32()), ("lease_end", pa.date32()), ("handover_date", pa.date32()),
    ("monthly_fee_idr", pa.int64()), ("needs_review", pa.bool_()), ("review_reason", pa.string()),
    ("model", pa.string()), ("prompt_version", pa.string()), ("extracted_at", TS),
])

FIELD_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["lease_agreement", "handover_certificate", "other"]},
        "party_name": {"type": ["string", "null"]},
        "party_tenant_id": {"type": ["string", "null"]},
        "plot_id": {"type": ["string", "null"]},
        "lease_start": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "lease_end": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "handover_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "monthly_fee_text": {"type": ["string", "null"], "description": "the fee exactly as written"},
    },
    "required": ["doc_type", "party_name", "party_tenant_id", "plot_id", "lease_start", "lease_end",
                 "handover_date", "monthly_fee_text"],
}

SYSTEM_PROMPT = """You extract fields from documents of an industrial estate.
Return JSON only, matching the schema. Use null for anything the document does not state explicitly;
never guess or compute a value. Dates must be YYYY-MM-DD.
- lease_start and lease_end: only for a lease agreement, the start and end of the lease term.
- handover_date: only for a handover certificate, the date the plot was handed over.
- monthly_fee_text: copy the monthly fee exactly as written, including currency and separators.
- party_tenant_id looks like T001; plot_id looks like A-01."""

_AMOUNT = re.compile(r"(?:IDR|Rp\.?)\s*([0-9][0-9.,\s]*[0-9])", re.IGNORECASE)


def parse_idr(text: str | None) -> int | None:
    """'IDR 164,000,000' / 'Rp 164.000.000' / 'Rp164.000.000,00' -> 164000000. None when unclear."""
    if not text:
        return None
    m = _AMOUNT.search(text) or re.search(r"([0-9][0-9.,\s]*[0-9])", text)
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group(1))
    raw = re.sub(r"[.,]\d{2}$", "", raw) if re.search(r"[.,]\d{3}", raw) else raw  # drop cents after thousands
    digits = re.sub(r"[.,]", "", raw)
    return int(digits) if digits.isdigit() else None


def normalize_tenant_id(value: str | None) -> str | None:
    """Fix the OCR confusions that are certain for this ID format (T + 3 digits): O->0, I/l->1."""
    if not value:
        return value
    v = value.strip().upper()
    if len(v) == 4 and v[0] == "T":
        v = "T" + v[1:].translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    return v


def pdf_text(data: bytes) -> tuple[str, str]:
    """Return (text, method). Uses OCR when the PDF has no usable text layer."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if len(text.strip()) >= 40:
        return text, "text-layer"
    pages = convert_from_bytes(data, dpi=200)
    return "\n".join(pytesseract.image_to_string(img, lang="eng") for img in pages), "ocr"


def extract_text() -> int:
    """Text for every document version not processed by the current parser version."""
    if not table_exists("bronze.document"):
        return 0
    docs = read_arrow("bronze.document").to_pylist()
    done = set()
    if table_exists("silver.document_text"):
        t = read_arrow("silver.document_text", f"parser_version == '{PARSER_VERSION}'", ("doc_id",))
        done = set(t["doc_id"].to_pylist())
    rows, now = [], datetime.now(timezone.utc)
    for d in docs:
        if d["doc_id"] in done:
            continue
        text, method = pdf_text(get_bytes(d["volume_path"].removeprefix("s3://")))
        rows.append({"doc_id": d["doc_id"], "file_name": d["file_name"], "tenant_id": d["tenant_id"],
                     "doc_kind": d["doc_kind"], "text": text, "chars": len(text), "method": method,
                     "parser_version": PARSER_VERSION, "extracted_at": now})
    return append("silver.document_text", pa.Table.from_pylist(rows, schema=TEXT_SCHEMA))


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def validate(fields: dict, file_tenant: str) -> tuple[dict, list[str]]:
    """Normalise LLM output and list the reasons a human should look at it."""
    out = {k: fields.get(k) for k in FIELD_JSON_SCHEMA["properties"]}
    reasons = []
    for k in ("lease_start", "lease_end", "handover_date"):
        raw, out[k] = out[k], _date(out[k])
        if raw and out[k] is None:
            reasons.append(f"{k} is not a valid date")
    fee_text = out.pop("monthly_fee_text", None)
    out["monthly_fee_idr"] = parse_idr(fee_text)
    if fee_text and out["monthly_fee_idr"] is None:
        reasons.append(f"fee '{fee_text}' could not be read")
    if out["monthly_fee_idr"] is not None and not FEE_RANGE_IDR[0] <= out["monthly_fee_idr"] <= FEE_RANGE_IDR[1]:
        reasons.append(f"fee {out['monthly_fee_idr']} is outside the plausible range")
    if out["doc_type"] != "lease_agreement":  # lease terms only come from a lease
        out["lease_start"] = out["lease_end"] = out["monthly_fee_idr"] = None
    if out["doc_type"] != "handover_certificate":
        out["handover_date"] = None
    out["party_tenant_id"] = normalize_tenant_id(out["party_tenant_id"])
    if out["party_tenant_id"] and out["party_tenant_id"] != file_tenant:
        reasons.append(f"tenant in document ({out['party_tenant_id']}) differs from file ({file_tenant})")
    if out["doc_type"] == "lease_agreement":
        for k in ("lease_start", "lease_end", "monthly_fee_idr"):
            if out[k] is None:
                reasons.append(f"{k} missing")
        if out["lease_start"] and out["lease_end"] and out["lease_end"] <= out["lease_start"]:
            reasons.append("lease_end is not after lease_start")
    if out["doc_type"] == "handover_certificate" and out["handover_date"] is None:
        reasons.append("handover_date missing")
    return out, reasons


def extract_fields(limit: int | None = None) -> dict[str, int]:
    """LLM field extraction for every text not yet processed with the current prompt and model."""
    if not table_exists("silver.document_text"):
        return {"extracted": 0, "needs_review": 0}
    model = settings().llm_model
    texts = read_arrow("silver.document_text", f"parser_version == '{PARSER_VERSION}'").to_pylist()
    done = set()
    if table_exists("silver.document_fields"):
        f = read_arrow("silver.document_fields", f"prompt_version == '{PROMPT_VERSION}' and model == '{model}'",
                       ("doc_id",))
        done = set(f["doc_id"].to_pylist())
    rows, review = [], 0
    for t in [t for t in texts if t["doc_id"] not in done][:limit]:
        msg = llm.chat([{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"File: {t['file_name']}\n\n{t['text'][:6000]}"}],
                       purpose="document_fields", schema=FIELD_JSON_SCHEMA)
        try:
            raw = json.loads(msg.content)
        except json.JSONDecodeError:
            raw = {"doc_type": "other"}
        fields, reasons = validate(raw, t["tenant_id"])
        review += bool(reasons)
        rows.append({"doc_id": t["doc_id"], "file_name": t["file_name"], "tenant_id": t["tenant_id"], **fields,
                     "needs_review": bool(reasons), "review_reason": "; ".join(reasons) or None,
                     "model": model, "prompt_version": PROMPT_VERSION, "extracted_at": datetime.now(timezone.utc)})
    append("silver.document_fields", pa.Table.from_pylist(rows, schema=FIELDS_SCHEMA))
    return {"extracted": len(rows), "needs_review": review}


def ensure_tables() -> None:
    """Create empty AI tables up front so downstream SQL can always reference them."""
    from ..storage import catalog, ensure_namespace

    ensure_namespace("silver")
    for name, schema in (("silver.document_text", TEXT_SCHEMA), ("silver.document_fields", FIELDS_SCHEMA)):
        if not table_exists(name):
            catalog().create_table(name, schema=schema)
