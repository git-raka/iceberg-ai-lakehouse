"""Data & AI API. The X-API-Key header selects the Trino identity; governance follows it everywhere."""

from __future__ import annotations

import html
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from trino.exceptions import TrinoUserError

from .. import superset_setup
from ..ai import agent, dashboard, text2sql, vectors
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


class DashboardPrompt(BaseModel):
    prompt: str = Field(min_length=3, max_length=1000)


class DashboardSpec(DashboardPrompt):
    spec: dict


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


@app.post("/v1/ai/dashboard")
def ai_dashboard(body: DashboardPrompt, user: str = Depends(identity)) -> dict:
    """Prompt to dashboard: the LLM picks catalog metrics, code compiles and runs them in Trino as the caller."""
    return dashboard.generate(body.prompt, user)


@app.post("/v1/ai/dashboard/publish")
def ai_dashboard_publish(body: DashboardSpec, user: str = Depends(identity)) -> dict:
    """Create the dashboard in Superset. The spec is compiled, checked and run again as the caller (nothing else
    from the browser is trusted), and Superset gets a database connection for the caller's identity, so its row
    filters and column masks still apply there."""
    if user == "admin" or user.startswith("vendor_"):
        # admin would get an unfiltered connection in Superset; external parties use the share API, not Superset
        raise HTTPException(403, f"{user} cannot publish dashboards to Superset")
    dash = dashboard.build(body.spec, user)
    if not dash["kpis"] and not dash["charts"]:
        raise HTTPException(400, "nothing in this spec produced data: " + "; ".join(dash["issues"][:6]))
    dash["prompt"] = body.prompt
    try:
        return superset_setup.publish(dash, user, summary=lambda: dashboard.summarize(body.prompt, dash, user))
    except (requests.RequestException, RuntimeError) as e:
        raise HTTPException(502, f"Superset: {e}") from e


# the demo pages may only be framed by the Superset dashboard on this machine
FRAME_ANCESTORS = {"Content-Security-Policy": "frame-ancestors 'self' http://localhost:8088 http://127.0.0.1:8088"}


def page(name: str) -> HTMLResponse:
    """A demo page. Its identity menu carries the non-admin API keys from .env, which is why every port is
    published on 127.0.0.1 only; do not expose these pages."""
    options = "\n".join(f'      <option value="{html.escape(key)}">{html.escape(user)}</option>'
                        for key, user in settings().api_keys.items()
                        if user != "admin" and not user.startswith("vendor_"))
    body = (Path(__file__).with_name(name)).read_text(encoding="utf-8").replace("<!-- IDENTITIES -->", options)
    return HTMLResponse(body, headers=FRAME_ANCESTORS)


@app.get("/", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    return page("chat.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    """Prompt box and ECharts renderer for /v1/ai/dashboard; `?embed=1` hides the heading for the Superset panel."""
    return page("dashboard.html")
