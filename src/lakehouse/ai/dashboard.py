"""Prompt-to-dashboard ("generative BI").

1. The local LLM turns a request ("an executive summary of what we have") into a dashboard *spec*: KPI tiles and
   charts, each naming a metric, a grouping, a time window and a chart type. The JSON schema only allows values
   from the metric catalog (dashboard_catalog.yaml), so the model chooses; it never writes SQL.
2. Code compiles every item to SQL from the catalog, checks it with the SQL guard and runs it in Trino *as the
   person asking*, so row filters and column masks shape the dashboard exactly as they shape everything else.
3. Code validates the result: items with no data or no access are dropped with a reason, a grouping the metric
   does not support is refused, and the chart type is fitted to the data (a time axis is a line, a few
   categories may be a pie, many rows become a table).
4. A short written summary is generated from the KPI values; every number in it must appear in those values.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml
from trino.exceptions import TrinoUserError

from . import llm, text2sql
from .agent import ungrounded
from .trino_client import query

MAX_KPIS, MAX_CHARTS = 6, 8


@lru_cache
def catalog() -> dict:
    return yaml.safe_load(Path(__file__).with_name("dashboard_catalog.yaml").read_text(encoding="utf-8"))


def spec_schema() -> dict:
    c = catalog()
    metric = {"type": "string", "enum": sorted(c["metrics"])}
    window = {"type": "string", "enum": list(c["windows"])}
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "kpis": {"type": "array", "maxItems": MAX_KPIS, "items": {
                "type": "object", "properties": {"metric": metric, "window": window}, "required": ["metric", "window"]}},
            "charts": {"type": "array", "maxItems": MAX_CHARTS, "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "metric": metric,
                               "group_by": {"type": "string", "enum": ["time", *sorted(c["dimensions"])]},
                               "window": window,
                               "chart": {"type": "string", "enum": ["line", "bar", "pie", "table"]}},
                "required": ["title", "metric", "group_by", "window", "chart"]}},
        },
        "required": ["title", "kpis", "charts"],
    }


def catalog_prompt() -> str:
    c = catalog()
    lines = ["Metrics (id: meaning; groupings; has a time axis?):"]
    for mid, m in c["metrics"].items():
        dims = ", ".join(m["dims"]) or "none"
        lines.append(f"- {mid}: {m['label']}; group by {dims}{'; time axis' if m.get('time') else '; no time axis'}")
    lines.append("Windows: " + ", ".join(c["windows"]) + " (only applies to metrics with a time axis).")
    lines.append("group_by 'time' draws a trend over the time axis; it needs a metric with a time axis.")
    return "\n".join(lines)


SYSTEM = """You design dashboards for an industrial estate's management. Choose KPI tiles and charts from the
metric catalog that best answer the request: up to 6 KPIs and up to 8 charts, no duplicates. Prefer a trend
(group_by time, chart line) for metrics with a time axis, a bar chart to compare categories. Write the title and
chart titles in the language of the request. Return JSON only.

{catalog}"""


def design(prompt: str, user: str, feedback: str | None = None) -> dict:
    messages = [{"role": "system", "content": SYSTEM.format(catalog=catalog_prompt())},
                {"role": "user", "content": prompt}]
    if feedback:
        messages.append({"role": "user", "content": feedback})
    msg = llm.chat(messages, purpose="dashboard-design", user=user, schema=spec_schema())
    try:
        return json.loads(msg.content or "{}")
    except json.JSONDecodeError:
        return {}


# ------------------------------------------------------------------------------------------- compile and run

def compile_sql(metric_id: str, group_by: str | None, window: str) -> tuple[str, str | None]:
    """SQL for one item from the catalog; returns (sql, note). Raises ValueError for an impossible request."""
    c = catalog()
    m = c["metrics"][metric_id]
    note = None
    where = "TRUE"
    if m.get("time"):
        where = c["windows"][window].format(time=m["time"])
    elif window != "all_time":
        note = f"{metric_id} has no time axis; the window was ignored"
    if group_by is None:
        sql = f"SELECT {m['expr']} AS value FROM {m['table']} WHERE {where}"
    elif group_by == "time":
        if not m.get("time"):
            raise ValueError(f"{metric_id} has no time axis to draw a trend")
        sql = (f"SELECT {m['time']} AS x, {m['expr']} AS y FROM {m['table']} WHERE {where} "
               f"GROUP BY 1 ORDER BY 1")
    else:
        if group_by not in m["dims"]:
            raise ValueError(f"{metric_id} cannot be grouped by {group_by}")
        sql = (f"SELECT {group_by} AS x, {m['expr']} AS y FROM {m['table']} WHERE {where} "
               f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST")
    return sql, note


def run(sql: str, user: str) -> list[list]:
    return query(text2sql.guard(sql), user=user, max_rows=text2sql.MAX_ROWS).rows


def fit_chart(requested: str, group_by: str, rows: list[list]) -> str:
    """The chart type that suits the data, whatever was requested."""
    if group_by == "time":
        return "line" if len(rows) > 2 else "bar"
    if len(rows) > 25:
        return "table"
    if requested == "pie" and len(rows) <= 8 and all((r[1] or 0) >= 0 for r in rows):
        return "pie"
    return "table" if requested == "table" else "bar"


def build(spec: dict, user: str) -> dict:
    """Compile, run and validate every item of a spec. Nothing the model chose reaches Trino unchecked."""
    c = catalog()
    kpis, charts, issues, seen = [], [], [], set()
    for item in (spec.get("kpis") or [])[:MAX_KPIS]:
        mid, window = item.get("metric"), item.get("window", "all_time")
        if mid not in c["metrics"] or (mid, window) in seen:
            continue
        seen.add((mid, window))
        try:
            sql, note = compile_sql(mid, None, window)
            rows = run(sql, user)
        except (ValueError, text2sql.UnsafeSQL, TrinoUserError) as e:
            issues.append(f"KPI {mid}: {getattr(e, 'message', e)}")
            continue
        value = rows[0][0] if rows else None
        if value is None:
            issues.append(f"KPI {mid}: no data visible to {user}")
            continue
        kpis.append({"metric": mid, "label": c["metrics"][mid]["label"], "window": window, "value": value,
                     "format": c["metrics"][mid]["format"], "sql": sql, **({"note": note} if note else {})})
    for item in (spec.get("charts") or [])[:MAX_CHARTS]:
        mid, group_by, window = item.get("metric"), item.get("group_by"), item.get("window", "all_time")
        if mid not in c["metrics"] or (mid, group_by, window) in seen:
            continue
        seen.add((mid, group_by, window))
        try:
            sql, note = compile_sql(mid, group_by, window)
            rows = run(sql, user)
        except (ValueError, text2sql.UnsafeSQL, TrinoUserError) as e:
            issues.append(f"Chart '{item.get('title') or mid}': {getattr(e, 'message', e)}")
            continue
        rows = [r for r in rows if r[1] is not None]
        if not rows:
            issues.append(f"Chart '{item.get('title') or mid}': no data visible to {user}")
            continue
        charts.append({"title": item.get("title") or c["metrics"][mid]["label"], "metric": mid,
                       "label": c["metrics"][mid]["label"], "group_by": group_by,
                       "x_label": "Date" if group_by == "time" else c["dimensions"][group_by],
                       "window": window, "chart": fit_chart(item.get("chart", "bar"), group_by, rows),
                       "format": c["metrics"][mid]["format"], "rows": rows, "sql": sql,
                       **({"note": note} if note else {})})
    return {"title": spec.get("title") or "Dashboard", "kpis": kpis, "charts": charts, "issues": issues}


def summarize(prompt: str, dashboard: dict, user: str) -> str | None:
    """Two or three sentences over the KPI values; dropped if it states a number that is not in them."""
    if not dashboard["kpis"]:
        return None
    facts = "\n".join(f"- {k['label']} ({k['window'].replace('_', ' ')}): {k['value']}" for k in dashboard["kpis"])
    msg = llm.chat([{"role": "system", "content": "Write a 2-3 sentence executive summary using only these "
                     "figures, in the language of the request. Do not add numbers that are not listed."},
                    {"role": "user", "content": f"Request: {prompt}\n\nFigures:\n{facts}"}],
                   purpose="dashboard-summary", user=user)
    text = (msg.content or "").strip()
    return text if text and not ungrounded(text, facts + "\n" + prompt) else None


def generate(prompt: str, user: str) -> dict:
    """Prompt in, validated dashboard out. One repair round when nothing usable came back; the catalog's default
    dashboard when the request still yields nothing."""
    spec = design(prompt, user)
    dash = build(spec, user)
    repaired = False
    if not dash["kpis"] and not dash["charts"]:
        repaired = True
        spec = design(prompt, user, feedback="None of those items produced data: " + "; ".join(dash["issues"][:6])
                      + ". Choose other metrics from the catalog.")
        dash = build(spec, user)
    fallback = False
    if not dash["kpis"] and not dash["charts"]:
        fallback = True
        dash = build(catalog()["default_dashboard"], user)
    dash.update(prompt=prompt, user=user, spec=spec, repaired=repaired, fallback=fallback,
                summary=summarize(prompt, dash, user))
    return dash
