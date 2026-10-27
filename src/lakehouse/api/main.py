"""Data & AI API. The X-API-Key header selects the Trino identity; governance follows it everywhere."""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from trino.exceptions import TrinoUserError

from ..ai import agent, text2sql, vectors
from ..ai.trino_client import query
from ..settings import settings

app = FastAPI(title="Iceberg AI Lakehouse API", version="0.1.0",
              description="Governed data and AI endpoints for a fictional industrial estate.")


def identity(x_api_key: str = Header(..., description="One of the demo API keys")) -> str:
    user = settings().api_keys.get(x_api_key)
    if not user:
        raise HTTPException(401, "unknown API key")
    return user


def run(sql: str, user: str, params_note: str = "", max_rows: int = 1000) -> dict:
    try:
        r = query(sql, user=user, max_rows=max_rows)
    except TrinoUserError as e:
        raise HTTPException(403 if "Access Denied" in str(e) else 400, e.message) from e
    return {"user": user, "columns": r.columns, "rows": r.rows, "note": params_note or None}


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class SQL(BaseModel):
    sql: str = Field(min_length=6, max_length=5000)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/tenants")
def tenants(user: str = Depends(identity)) -> dict:
    return run("SELECT * FROM gold.tenant_overview ORDER BY tenant_id", user)


@app.get("/v1/contracts/expiring")
def expiring(days: int = 365, user: str = Depends(identity)) -> dict:
    days = max(0, min(days, 3650))
    return run(f"SELECT tenant_id, file_name, lease_end, days_to_expiry, needs_review FROM gold.contract_expiry "
               f"WHERE days_to_expiry <= {days} ORDER BY days_to_expiry", user)


@app.get("/v1/bus/latest-position")
def bus_positions(user: str = Depends(identity)) -> dict:
    return run("SELECT * FROM gold.bus_latest_position ORDER BY bus_id", user)


@app.get("/v1/share/tonnage")
def shared_tonnage(days: int = 30, user: str = Depends(identity)) -> dict:
    """Shared data product for external parties: trucks and net tons per tenant per day. The shape is a fixed
    contract; which tenants a caller sees is decided by Trino (a vendor only gets the tenants it serves)."""
    days = max(1, min(days, 366))
    return run(f"SELECT tenant_id, day, trucks, net_tons FROM share.tonnage_daily "
               f"WHERE day >= current_date - INTERVAL '{days}' DAY ORDER BY day, tenant_id", user, max_rows=10000)


@app.post("/v1/sql")
def sql(body: SQL, user: str = Depends(identity)) -> dict:
    """Read-only SQL over the semantic-model tables (checked, limited to 200 rows)."""
    try:
        safe = text2sql.guard(body.sql)
    except text2sql.UnsafeSQL as e:
        raise HTTPException(400, str(e)) from e
    return {**run(safe, user, max_rows=text2sql.MAX_ROWS), "sql": safe}


@app.post("/v1/ai/text2sql")
def ai_text2sql(body: Question, user: str = Depends(identity)) -> dict:
    return {"user": user, **text2sql.ask(body.question, user)}


@app.post("/v1/ai/search")
def ai_search(body: Question, k: int = 5, user: str = Depends(identity)) -> dict:
    return {"user": user, "hits": vectors.search(body.question, user=user, k=max(1, min(k, 20)))}


@app.post("/v1/ai/ask")
def ai_ask(body: Question, user: str = Depends(identity)) -> dict:
    """The agent: picks tools (SQL, document search) and answers in plain language."""
    return agent.ask(body.question, user)


@app.get("/", response_class=HTMLResponse)
def chat_page() -> str:
    """The demo chat. Its identity menu carries the non-admin API keys from .env, which is why every port is
    published on 127.0.0.1 only; do not expose this page."""
    options = "\n".join(f'      <option value="{html.escape(key)}">{html.escape(user)}</option>'
                        for key, user in settings().api_keys.items()
                        if user != "admin" and not user.startswith("vendor_"))
    return (Path(__file__).with_name("chat.html")).read_text(encoding="utf-8").replace("<!-- IDENTITIES -->", options)
