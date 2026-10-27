"""Optional Cube layer (`make cube-up`): demo tokens, and the same data through Cube's REST and GraphQL APIs and
the Data API, as an internal identity and as a fictional vendor."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import requests

from .settings import settings

CUBE = os.environ.get("CUBE_URL", "http://cube:4000/cubejs-api")
API = os.environ.get("API_URL", "http://api:8000")


def token(trino_user: str, hours: int = 24) -> str:
    """A Cube API token (HS256 JWT) whose `trino_user` claim is the Trino identity Cube will query as.
    Demo only: in production the identity provider issues the tokens and Cube verifies them with its JWKS."""
    def part(data: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=")
    signing_input = part({"alg": "HS256", "typ": "JWT"}) + b"." + part(
        {"trino_user": trino_user, "exp": int(time.time()) + hours * 3600})
    signature = hmac.new(os.environ["CUBE_API_SECRET"].encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def load(query: dict, auth: str | None, timeout_s: int = 60) -> dict:
    """Cube REST API. Cube answers "Continue wait" while a query is still running in Trino."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = requests.post(f"{CUBE}/v1/load", json={"query": query},
                          headers={"Authorization": auth} if auth else {}, timeout=60)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}
        if body.get("error") != "Continue wait" or time.monotonic() > deadline:
            return {"status": r.status_code, **body}
        time.sleep(1)


def graphql(query: str, auth: str, timeout_s: int = 60) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        r = requests.post(f"{CUBE}/graphql", json={"query": query}, headers={"Authorization": auth}, timeout=60)
        body = r.json()
        if "Continue wait" not in json.dumps(body.get("errors", "")) or time.monotonic() > deadline:
            return body
        time.sleep(1)


def _show(title: str, result: dict) -> None:
    print(f"\n--- {title}")
    for row in (result.get("data") or [])[:15]:
        print("   ", {k.split(".")[-1]: v for k, v in row.items()})
    if not result.get("data"):
        print(f"    HTTP {result.get('status')}: {str(result.get('error') or result)[:300]}")


def demo() -> None:
    tonnage = {"measures": ["tonnage.net_tons", "tonnage.trucks"], "dimensions": ["tonnage.tenant_id"],
               "timeDimensions": [{"dimension": "tonnage.day", "dateRange": "last 30 days"}],
               "order": {"tonnage.net_tons": "desc"}}
    vendor, executive = token("vendor_haulage"), token("executive")

    _show("Cube REST, as vendor_haulage: tonnage per tenant, last 30 days (Trino row filter: T002, T005)",
          load(tonnage, vendor))
    _show("Cube REST, as executive: the same query (all tenants)", load(tonnage, executive))

    gql = """{ cube(where: {tonnage: {day: {inDateRange: "last 7 days"}}}) {
                 tonnage(orderBy: {day: asc}) { tenant_id net_tons day { day } } } }"""
    result = graphql(gql, vendor)
    print("\n--- Cube GraphQL, as vendor_haulage: daily tons, last 7 days")
    for row in (result.get("data") or {}).get("cube", [])[:15]:
        t = row["tonnage"]
        print(f"    {t['day']['day'][:10]}  {t['tenant_id']}  {t['net_tons']}")
    if result.get("errors"):
        print("   ", json.dumps(result["errors"])[:300])

    _show("Cube REST, as vendor_haulage: internal ticket metrics (no grant in Trino)",
          load({"measures": ["tickets.open_tickets"], "dimensions": ["tickets.category"]}, vendor))
    _show("Cube REST, without a token", load(tonnage, None))
    forged = vendor.rsplit(".", 1)[0] + ".bm90LXRoZS1zaWduYXR1cmU"
    _show("Cube REST, token with a forged signature", load(tonnage, forged))
    _show("Cube REST, token for an identity Cube does not serve (admin)", load(tonnage, token("admin")))

    key = next((k for k, u in settings().api_keys.items() if u == "vendor_haulage"), None)
    print("\n--- Data API (fixed REST contract for external parties), as vendor_haulage: GET /v1/share/tonnage?days=7")
    if key:
        r = requests.get(f"{API}/v1/share/tonnage", params={"days": 7}, headers={"X-API-Key": key}, timeout=60)
        body = r.json()
        tenants = sorted({row[0] for row in body.get("rows", [])})
        print(f"    HTTP {r.status_code}, {len(body.get('rows', []))} rows, tenants: {tenants}")
    else:
        print("    no API key for vendor_haulage in API_KEYS_JSON")
