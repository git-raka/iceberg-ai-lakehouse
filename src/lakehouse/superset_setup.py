"""Dashboards as code: create (or update) the Trino database, datasets, charts and the executive
dashboard in Superset through its REST API. Safe to run repeatedly."""

from __future__ import annotations

import json
import os

import requests

BASE = os.environ.get("SUPERSET_URL", "http://superset:8088")
DB_NAME = "Lakehouse (Trino, executive)"
DASH_SLUG = "executive"


class Superset:
    def __init__(self) -> None:
        self.s = requests.Session()
        r = self.s.post(f"{BASE}/api/v1/security/login", json={
            "username": "admin", "password": os.environ["SUPERSET_ADMIN_PASSWORD"], "provider": "db", "refresh": True})
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        csrf = self.s.get(f"{BASE}/api/v1/security/csrf_token/")
        csrf.raise_for_status()
        self.s.headers.update({"X-CSRFToken": csrf.json()["result"], "Referer": BASE})

    def find(self, resource: str, field: str, value: str) -> dict | None:
        r = self.s.get(f"{BASE}/api/v1/{resource}/", params={"q": "(page_size:100)"})
        r.raise_for_status()
        return next((x for x in r.json()["result"] if x.get(field) == value), None)

    def upsert(self, resource: str, field: str, value: str, payload: dict) -> int:
        existing = self.find(resource, field, value)
        if existing:
            r = self.s.put(f"{BASE}/api/v1/{resource}/{existing['id']}", json=payload)
            r.raise_for_status()
            return existing["id"]
        r = self.s.post(f"{BASE}/api/v1/{resource}/", json=payload)
        if not r.ok:
            raise RuntimeError(f"{resource} {value}: {r.status_code} {r.text[:300]}")
        return r.json()["id"]


def metric(column: str, agg: str, label: str) -> dict:
    return {"expressionType": "SIMPLE", "column": {"column_name": column}, "aggregate": agg, "label": label}


def charts(ds: dict[str, int]) -> list[dict]:
    count = {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "Count"}
    return [
        {"name": "Tenants", "ds": "tenant_overview", "viz": "big_number_total", "w": 3, "h": 40,
         "params": {"metric": count}},
        {"name": "Leases expiring within 12 months", "ds": "contract_expiry", "viz": "big_number_total", "w": 3, "h": 40,
         "params": {"metric": count, "adhoc_filters": [{"expressionType": "SQL", "clause": "WHERE",
                                                       "sqlExpression": "days_to_expiry BETWEEN 0 AND 365"}]}},
        {"name": "AI extractions to review", "ds": "contract_expiry", "viz": "big_number_total", "w": 3, "h": 40,
         "params": {"metric": count, "adhoc_filters": [{"expressionType": "SQL", "clause": "WHERE",
                                                       "sqlExpression": "needs_review"}]}},
        {"name": "Open service tickets", "ds": "ticket_sla", "viz": "big_number_total", "w": 3, "h": 40,
         "params": {"metric": metric("open_tickets", "SUM", "Open tickets")}},
        {"name": "Net tons per day", "ds": "weighbridge_daily", "viz": "echarts_timeseries_line", "w": 6, "h": 50,
         "params": {"x_axis": "day", "time_grain_sqla": "P1D", "metrics": [metric("net_tons", "SUM", "Net tons")],
                    "groupby": [], "row_limit": 10000}},
        {"name": "Net tons per tenant, last 30 days", "ds": "tenant_overview", "viz": "echarts_timeseries_bar", "w": 6,
         "h": 50, "params": {"x_axis": "tenant_id", "metrics": [metric("net_tons_30d", "SUM", "Net tons 30d")],
                             "groupby": [], "row_limit": 100}},
        {"name": "Lease expiry (fields extracted by the local LLM)", "ds": "contract_expiry", "viz": "table", "w": 6,
         "h": 60, "params": {"query_mode": "raw", "all_columns": ["tenant_id", "lease_start", "lease_end",
                                                                   "days_to_expiry", "monthly_fee_idr", "needs_review",
                                                                   "model"], "order_by_cols": ['["days_to_expiry", true]'],
                             "row_limit": 100}},
        {"name": "Service tickets by category", "ds": "ticket_sla", "viz": "table", "w": 6, "h": 60,
         "params": {"query_mode": "aggregate", "groupby": ["category"],
                    "metrics": [metric("tickets", "SUM", "Tickets"), metric("sla_breaches", "SUM", "SLA breaches"),
                                metric("avg_resolution_hours", "AVG", "Avg hours to close")], "row_limit": 50}},
        {"name": "E-bus fleet, latest position", "ds": "bus_latest_position", "viz": "table", "w": 12, "h": 50,
         "params": {"query_mode": "raw", "all_columns": ["bus_id", "route_id", "event_time", "lat", "lon", "speed_kmh",
                                                          "battery_pct", "passengers"], "row_limit": 50}},
    ]


def query_context(ds_id: int, viz: str, p: dict) -> dict:
    """The saved query a chart runs for API access, thumbnails, alerts and reports."""
    where = " AND ".join(f"({f['sqlExpression']})" for f in p.get("adhoc_filters", []))
    q: dict = {"filters": [], "extras": {"where": where} if where else {}, "row_limit": p.get("row_limit", 1000)}
    if viz == "big_number_total":
        q["metrics"] = [p["metric"]]
    elif viz.startswith("echarts_timeseries"):
        axis = {"expressionType": "SQL", "sqlExpression": p["x_axis"], "label": p["x_axis"], "columnType": "BASE_AXIS"}
        if p.get("time_grain_sqla"):
            axis["timeGrain"] = p["time_grain_sqla"]
        q.update(columns=[axis], metrics=p["metrics"], orderby=[[p["metrics"][0], False]])
    elif p.get("query_mode") == "raw":
        q.update(columns=p["all_columns"], orderby=[[c.split('"')[1], True] for c in p.get("order_by_cols", [])])
    else:
        q.update(columns=p["groupby"], metrics=p["metrics"])
    return {"datasource": {"id": ds_id, "type": "table"}, "force": False, "queries": [q],
            "form_data": p, "result_format": "json", "result_type": "full"}


ASSISTANT_MD = (
    "### AI assistant (local LLM, governed by the same Trino policies)\n"
    '<iframe src="http://localhost:8000/" title="Estate data assistant" width="100%" height="560" '
    'style="border:0" loading="lazy"></iframe>'
)


def provision() -> dict:
    api = Superset()
    db_id = api.upsert("database", "database_name", DB_NAME, {
        "database_name": DB_NAME, "sqlalchemy_uri": "trino://executive@trino:8080/iceberg",
        "expose_in_sqllab": True, "allow_run_async": False})
    ds_ids = {}
    for table in ("tenant_overview", "contract_expiry", "weighbridge_daily", "ticket_sla", "bus_latest_position"):
        existing = api.find("dataset", "table_name", table)
        ds_ids[table] = existing["id"] if existing else api.upsert(
            "dataset", "table_name", table, {"database": db_id, "schema": "gold", "table_name": table})
    layout, rows, row, width = [], [], [], 0
    chart_ids = []
    for c in charts(ds_ids):
        params = {"viz_type": c["viz"], "datasource": f"{ds_ids[c['ds']]}__table", "time_range": "No filter",
                  **c["params"]}
        cid = api.upsert("chart", "slice_name", c["name"], {
            "slice_name": c["name"], "viz_type": c["viz"], "datasource_id": ds_ids[c["ds"]],
            "datasource_type": "table", "params": json.dumps(params),
            "query_context": json.dumps(query_context(ds_ids[c["ds"]], c["viz"], params))})
        chart_ids.append(cid)
        if width + c["w"] > 12:
            rows.append(row)
            row, width = [], 0
        row.append((cid, c))
        width += c["w"]
    rows.append(row)

    position = {"DASHBOARD_VERSION_KEY": "v2",
                "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
                "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": "Estate executive overview"}},
                "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]}}
    for i, items in enumerate(rows + [None]):
        rid = f"ROW-{i}"
        position["GRID_ID"]["children"].append(rid)
        position[rid] = {"type": "ROW", "id": rid, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
                         "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        if items is None:  # last row: the AI assistant panel
            position[rid]["children"].append("MARKDOWN-ai")
            position["MARKDOWN-ai"] = {"type": "MARKDOWN", "id": "MARKDOWN-ai", "children": [],
                                       "parents": ["ROOT_ID", "GRID_ID", rid],
                                       "meta": {"width": 12, "height": 80, "code": ASSISTANT_MD}}
            continue
        for cid, c in items:
            key = f"CHART-{cid}"
            position[rid]["children"].append(key)
            position[key] = {"type": "CHART", "id": key, "children": [], "parents": ["ROOT_ID", "GRID_ID", rid],
                             "meta": {"chartId": cid, "width": c["w"], "height": c["h"], "sliceName": c["name"]}}
    dash_id = api.upsert("dashboard", "slug", DASH_SLUG, {
        "dashboard_title": "Estate executive overview", "slug": DASH_SLUG, "published": True,
        "position_json": json.dumps(position), "json_metadata": json.dumps({"refresh_frequency": 300})})
    for cid in chart_ids:
        api.s.put(f"{BASE}/api/v1/chart/{cid}", json={"dashboards": [dash_id]}).raise_for_status()
    return {"database": db_id, "datasets": len(ds_ids), "charts": len(chart_ids), "dashboard": dash_id,
            "url": f"http://localhost:8088/superset/dashboard/{DASH_SLUG}/"}
