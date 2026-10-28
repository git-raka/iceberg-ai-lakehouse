"""Dashboards as code: create (or update) the Trino database, datasets, charts and the executive
dashboard in Superset through its REST API. Safe to run repeatedly.

`publish` does the same for a prompt-to-dashboard result: one virtual dataset per item (the compiled SQL), one
chart each, on a database connection that runs as the identity that asked."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from collections.abc import Callable

import requests

BASE = os.environ.get("SUPERSET_URL", "http://superset:8088")
PUBLIC_URL = "http://localhost:8088"
DASH_SLUG = "executive"


def db_name(user: str) -> str:
    return f"Lakehouse (Trino, {user})"


DB_NAME = db_name("executive")


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
        # filter on the server: AI-published dashboards add datasets and charts beyond one page of results
        rison = "'" + value.replace("!", "!!").replace("'", "!'") + "'"
        r = self.s.get(f"{BASE}/api/v1/{resource}/",
                       params={"q": f"(filters:!((col:{field},opr:eq,value:{rison})),page_size:100)"})
        r.raise_for_status()
        return next((x for x in r.json()["result"] if x.get(field) == value), None)

    def create(self, resource: str, payload: dict) -> int:
        r = self.s.post(f"{BASE}/api/v1/{resource}/", json=payload)
        if not r.ok:
            raise RuntimeError(f"{resource}: {r.status_code} {r.text[:300]}")
        return r.json()["id"]

    def upsert(self, resource: str, field: str, value: str, payload: dict) -> int:
        existing = self.find(resource, field, value)
        if existing:
            r = self.s.put(f"{BASE}/api/v1/{resource}/{existing['id']}", json=payload)
            r.raise_for_status()
            return existing["id"]
        return self.create(resource, payload)

    def database(self, user: str) -> int:
        """A Trino connection that runs every query as `user`, so Trino's row filters and masks apply to it."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", user):
            raise ValueError(f"not a Trino identity: {user!r}")
        name = db_name(user)
        return self.upsert("database", "database_name", name, {
            "database_name": name, "sqlalchemy_uri": f"trino://{user}@trino:8080/iceberg",
            "expose_in_sqllab": True, "allow_run_async": False})


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
        q.update(columns=p["all_columns"], orderby=[json.loads(c) for c in p.get("order_by_cols", [])])
    elif viz == "pie":
        q.update(columns=p["groupby"], metrics=[p["metric"]], orderby=[[p["metric"], False]])
    else:
        q.update(columns=p["groupby"], metrics=p["metrics"])
    return {"datasource": {"id": ds_id, "type": "table"}, "force": False, "queries": [q],
            "form_data": p, "result_format": "json", "result_type": "full"}


BUILDER_MD = (
    "### Build a dashboard from a prompt (local LLM, governed by the same Trino policies)\n"
    '<iframe src="http://localhost:8000/dashboard?embed=1" title="Prompt to dashboard" width="100%" height="720" '
    'style="border:0" loading="lazy"></iframe>'
)
ASSISTANT_MD = (
    "### AI assistant (local LLM, governed by the same Trino policies)\n"
    '<iframe src="http://localhost:8000/" title="Estate data assistant" width="100%" height="560" '
    'style="border:0" loading="lazy"></iframe>'
)


def grid(items: list[dict]) -> list[list[dict]]:
    """Fill rows of Superset's 12-column grid left to right; each item has a width "w"."""
    rows, row, width = [], [], 0
    for item in items:
        if row and width + item["w"] > 12:
            rows.append(row)
            row, width = [], 0
        row.append(item)
        width += item["w"]
    return rows + [row] if row else rows


def position(title: str, rows: list[list[dict]]) -> dict:
    """position_json for a dashboard. Items are charts ({"chart": id, "name"}) or Markdown ({"markdown": key,
    "code"}), each with a width "w" and a height "h"."""
    pos = {"DASHBOARD_VERSION_KEY": "v2",
           "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
           "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
           "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]}}
    for i, items in enumerate(rows):
        rid = f"ROW-{i}"
        pos["GRID_ID"]["children"].append(rid)
        pos[rid] = {"type": "ROW", "id": rid, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
                    "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for item in items:
            if "chart" in item:
                key, kind = f"CHART-{item['chart']}", "CHART"
                meta = {"chartId": item["chart"], "width": item["w"], "height": item["h"], "sliceName": item["name"]}
            else:
                key, kind = f"MARKDOWN-{item['markdown']}", "MARKDOWN"
                meta = {"width": item["w"], "height": item["h"], "code": item["code"]}
            pos[rid]["children"].append(key)
            pos[key] = {"type": kind, "id": key, "children": [], "parents": ["ROOT_ID", "GRID_ID", rid], "meta": meta}
    return pos


def provision() -> dict:
    api = Superset()
    db_id = api.database("executive")
    ds_ids = {}
    for table in ("tenant_overview", "contract_expiry", "weighbridge_daily", "ticket_sla", "bus_latest_position"):
        existing = api.find("dataset", "table_name", table)
        ds_ids[table] = existing["id"] if existing else api.upsert(
            "dataset", "table_name", table, {"database": db_id, "schema": "gold", "table_name": table})
    items = [{"markdown": "builder", "code": BUILDER_MD, "w": 12, "h": 100}]
    chart_ids = []
    for c in charts(ds_ids):
        params = {"viz_type": c["viz"], "datasource": f"{ds_ids[c['ds']]}__table", "time_range": "No filter",
                  **c["params"]}
        cid = api.upsert("chart", "slice_name", c["name"], {
            "slice_name": c["name"], "viz_type": c["viz"], "datasource_id": ds_ids[c["ds"]],
            "datasource_type": "table", "params": json.dumps(params),
            "query_context": json.dumps(query_context(ds_ids[c["ds"]], c["viz"], params))})
        chart_ids.append(cid)
        items.append({"chart": cid, "name": c["name"], "w": c["w"], "h": c["h"]})
    items.append({"markdown": "ai", "code": ASSISTANT_MD, "w": 12, "h": 80})

    dash_id = api.upsert("dashboard", "slug", DASH_SLUG, {
        "dashboard_title": "Estate executive overview", "slug": DASH_SLUG, "published": True,
        "position_json": json.dumps(position("Estate executive overview", grid(items))),
        "json_metadata": json.dumps({"refresh_frequency": 300})})
    for cid in chart_ids:
        api.s.put(f"{BASE}/api/v1/chart/{cid}", json={"dashboards": [dash_id]}).raise_for_status()
    return {"database": db_id, "datasets": len(ds_ids), "charts": len(chart_ids), "dashboard": dash_id,
            "url": f"{PUBLIC_URL}/superset/dashboard/{DASH_SLUG}/"}


# ------------------------------------------------------------------------------- prompt-to-dashboard results

D3_FORMAT = {"number": "SMART_NUMBER", "idr": "SMART_NUMBER", "hours": ",.1f", "percent": ",.1f"}


def ai_chart(item: dict) -> tuple[str, dict, int, int]:
    """(viz type, params, width, height) for one validated KPI or chart. Its dataset is the item's own SQL, whose
    columns are `value` for a KPI and `x`, `y` for a chart."""
    if "chart" not in item:  # KPI tile: the window as a small caption under the number
        return "big_number_total", {"metric": {"expressionType": "SQL", "sqlExpression": "MAX(value)",
                                               "label": item["label"]},
                                    "subheader": item["window"].replace("_", " "),
                                    "header_font_size": 0.3, "subheader_font_size": 0.125,
                                    "y_axis_format": D3_FORMAT[item["format"]]}, 2, 40
    y = {"expressionType": "SQL", "sqlExpression": "SUM(y)", "label": item["label"]}
    if item["chart"] == "pie":
        return "pie", {"groupby": ["x"], "metric": y, "row_limit": 1000}, 6, 50
    if item["chart"] == "table":
        return "table", {"query_mode": "raw", "all_columns": ["x", "y"], "order_by_cols": ['["y", false]'],
                         "row_limit": 1000}, 6, 50
    viz = "echarts_timeseries_line" if item["chart"] == "line" else "echarts_timeseries_bar"
    # no axis title: the chart title names the axis, and Superset draws the title over the tick labels
    params = {"x_axis": "x", "metrics": [y], "groupby": [], "row_limit": 1000, "y_axis_format":
              D3_FORMAT[item["format"]], "show_legend": False}
    if item["group_by"] != "time":  # categories: largest first, as on the prompt page
        params["x_axis_sort"], params["x_axis_sort_asc"] = item["label"], False
    return viz, params, 6, 50


def plain(text: str) -> str:
    """Text shown literally in a Markdown component: HTML and Markdown syntax are escaped, so a prompt or a model
    summary cannot add links, images or iframes to a dashboard."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|~>])", r"\\\1", html.escape(" ".join(text.split()), quote=False))


def slug_for(dash: dict, user: str) -> str:
    """The same prompt and the same validated items, published by the same identity, give the same slug."""
    items = [[i["sql"], i.get("chart"), i.get("title"), i["window"]] for i in dash["kpis"] + dash["charts"]]
    digest = hashlib.sha256(json.dumps([user, dash["prompt"], dash["title"], items]).encode()).hexdigest()[:10]
    return f"ai-{user.replace('_', '-')}-{digest}"


def publish(dash: dict, user: str, summary: Callable[[], str | None] = lambda: None) -> dict:
    """Create a validated prompt-to-dashboard result as a real Superset dashboard. Publishing the same result again
    returns the existing dashboard; `summary` is only called when a new one is created. If anything fails on the
    way, the objects created so far are removed again."""
    api = Superset()
    slug = slug_for(dash, user)
    url = f"{PUBLIC_URL}/superset/dashboard/{slug}/"
    existing = api.find("dashboard", "slug", slug)
    if existing:
        return {"dashboard": existing["id"], "url": url, "existing": True}
    db_id = api.database(user)

    note = (f"Built from the prompt “{plain(dash['prompt'])}” for **{user}**. Every chart queries Trino as {user}, "
            "so the same row filters and column masks apply.")
    text = summary()
    if text:
        note = f"{plain(text)}\n\n{note}"
    items = [{"markdown": "summary", "code": note, "w": 12, "h": 20}]
    created: list[tuple[str, int]] = []
    try:
        for n, item in enumerate(dash["kpis"] + dash["charts"]):
            ds_id = api.create("dataset", {"database": db_id, "schema": "gold",
                                           "table_name": f"{slug.replace('-', '_')}_{n}", "sql": item["sql"]})
            created.append(("dataset", ds_id))
            api.s.put(f"{BASE}/api/v1/dataset/{ds_id}/refresh").raise_for_status()  # read the columns of the SQL
            viz, params, w, h = ai_chart(item)
            params = {"viz_type": viz, "datasource": f"{ds_id}__table", "time_range": "No filter", **params}
            name = item.get("title") or item["label"]
            cid = api.create("chart", {"slice_name": name, "viz_type": viz, "datasource_id": ds_id,
                                       "datasource_type": "table", "params": json.dumps(params),
                                       "query_context": json.dumps(query_context(ds_id, viz, params))})
            created.append(("chart", cid))
            items.append({"chart": cid, "name": name, "w": w, "h": h})

        dash_id = api.create("dashboard", {"dashboard_title": dash["title"], "slug": slug, "published": True,
                                           "position_json": json.dumps(position(dash["title"], grid(items)))})
        created.append(("dashboard", dash_id))
        for kind, cid in created:
            if kind == "chart":
                api.s.put(f"{BASE}/api/v1/chart/{cid}", json={"dashboards": [dash_id]}).raise_for_status()
    except Exception:
        for kind, oid in reversed(created):  # charts before their datasets
            api.s.delete(f"{BASE}/api/v1/{kind}/{oid}")
        raise
    return {"dashboard": dash_id, "url": url, "charts": sum(k == "chart" for k, _ in created), "existing": False}
