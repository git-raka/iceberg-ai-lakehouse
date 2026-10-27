"""Unit tests for the parts of the AI layer that must be right without a model in the loop."""

from datetime import date

import pytest

from lakehouse.ai import documents, text2sql, vectors


# --------------------------------------------------------------------------- SQL guard

@pytest.mark.parametrize("sql", [
    "SELECT tenant_id, net_tons FROM gold.weighbridge_daily",
    "select * from gold.contract_expiry where days_to_expiry < 365 order by 1",
    "WITH t AS (SELECT tenant_id FROM gold.tenant_overview) SELECT * FROM t",
    "SELECT a.tenant_id FROM gold.tenant_overview a JOIN gold.ticket_sla b ON a.tenant_id = b.tenant_id",
])
def test_guard_accepts_read_only_queries_on_known_tables(sql):
    safe = text2sql.guard(sql)
    assert "LIMIT" in safe.upper()


@pytest.mark.parametrize("sql, reason", [
    ("DELETE FROM gold.tenant_overview", "SELECT"),
    ("DROP TABLE gold.tenant_overview", "SELECT"),
    ("INSERT INTO gold.headcount VALUES ('x', 1, 1)", "SELECT"),
    ("SELECT * FROM silver.employee", "semantic model"),
    ("SELECT * FROM bronze.erp_employee", "semantic model"),
    ("SELECT * FROM system.runtime.queries", "not"),
    ("SELECT 1; DROP TABLE gold.headcount", "one statement"),
    ("SELECT * FROM postgresql.public.secrets", "not"),
])
def test_guard_rejects_writes_and_unknown_tables(sql, reason):
    with pytest.raises(text2sql.UnsafeSQL, match=reason):
        text2sql.guard(sql)


def test_guard_keeps_an_existing_limit():
    assert text2sql.guard("SELECT tenant_id FROM gold.tenant_overview LIMIT 5").upper().endswith("LIMIT 5")


def test_semantic_model_tables_are_all_in_gold_or_silver():
    assert all(t.split(".")[0] in {"gold", "silver"} for t in text2sql.allowed_tables())
    assert "silver.employee" not in text2sql.allowed_tables()  # PII stays out of generated SQL


# --------------------------------------------------------------------------- extraction validation

def test_valid_lease_needs_no_review():
    fields, reasons = documents.validate({
        "doc_type": "lease_agreement", "party_name": "Bluepeak Ceramics", "party_tenant_id": "T002",
        "plot_id": "B-02", "lease_start": "2024-06-01", "lease_end": "2029-05-31", "handover_date": None,
        "monthly_fee_text": "IDR 120,000,000"}, "T002")
    assert reasons == []
    assert fields["lease_end"] == date(2029, 5, 31) and fields["monthly_fee_idr"] == 120_000_000


def test_lease_without_end_date_or_wrong_tenant_goes_to_review():
    _, reasons = documents.validate({
        "doc_type": "lease_agreement", "party_name": "X", "party_tenant_id": "T009", "plot_id": None,
        "lease_start": "2024-06-01", "lease_end": None, "handover_date": None, "monthly_fee_text": "IDR 5"}, "T002")
    assert any("lease_end missing" in r for r in reasons)
    assert any("differs" in r for r in reasons)


def test_invalid_dates_and_negative_fees_are_rejected_not_guessed():
    fields, reasons = documents.validate({
        "doc_type": "lease_agreement", "party_name": None, "party_tenant_id": None, "plot_id": None,
        "lease_start": "31/02/2024", "lease_end": "2023-01-01", "handover_date": None,
        "monthly_fee_text": "minus ten"}, "T001")
    assert fields["lease_start"] is None and fields["monthly_fee_idr"] is None
    assert any("not a valid date" in r for r in reasons)


# --------------------------------------------------------------------------- chunking

def test_chunks_cover_the_text_with_bounded_size():
    text = "\n".join(f"Clause {i}. " + "word " * 30 for i in range(40))
    chunks = vectors.chunk(text, size=500, overlap=80)
    assert len(chunks) > 5
    assert all(len(c) <= 500 for c in chunks)
    assert "Clause 0." in chunks[0] and "Clause 39." in chunks[-1]


def test_short_and_empty_texts():
    assert vectors.chunk("short text") == ["short text"]
    assert vectors.chunk("   \n  ") == []
