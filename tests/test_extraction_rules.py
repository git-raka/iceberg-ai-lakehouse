"""Deterministic post-processing of LLM extractions: the model quotes, code converts and checks."""

import pytest

from lakehouse.ai.documents import normalize_tenant_id, parse_idr, validate


@pytest.mark.parametrize("text, expected", [
    ("IDR 164,000,000", 164_000_000),
    ("Rp 164.000.000", 164_000_000),
    ("Rp164.000.000,00", 164_000_000),
    ("IDR 185,000,000.00 per month", 185_000_000),
    ("a monthly service fee of IDR 98,000,000 for estate services", 98_000_000),
    ("", None),
    (None, None),
    ("to be agreed", None),
])
def test_parse_idr(text, expected):
    assert parse_idr(text) == expected


@pytest.mark.parametrize("raw, expected", [("TO06", "T006"), ("t0l1", "T011"), ("T003", "T003"), (None, None)])
def test_normalize_tenant_id_fixes_certain_ocr_confusions(raw, expected):
    assert normalize_tenant_id(raw) == expected


def test_handover_certificate_never_carries_lease_terms():
    fields, reasons = validate({
        "doc_type": "handover_certificate", "party_name": "Aurora Packaging", "party_tenant_id": "TO01",
        "plot_id": "A-01", "lease_start": "2023-01-14", "lease_end": None, "handover_date": "2023-01-14",
        "monthly_fee_text": None}, "T001")
    assert reasons == []
    assert fields["lease_start"] is None and fields["handover_date"].isoformat() == "2023-01-14"
    assert fields["party_tenant_id"] == "T001"


def test_implausible_fee_goes_to_review():
    _, reasons = validate({
        "doc_type": "lease_agreement", "party_name": "X", "party_tenant_id": "T001", "plot_id": "A-01",
        "lease_start": "2023-01-01", "lease_end": "2027-12-31", "handover_date": None,
        "monthly_fee_text": "IDR 164,000,000,000,000"}, "T001")
    assert any("plausible" in r for r in reasons)
