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
from .agent import language, ungrounded
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
                # the title comes last: the model writes it after choosing what the chart shows
                "properties": {"metric": metric,
                               "group_by": {"type": "string", "enum": ["time", *sorted(c["dimensions"])]},
                               "window": window,
                               "chart": {"type": "string", "enum": ["line", "bar", "pie", "table"]},
                               "title": {"type": "string"}},
                "required": ["metric", "group_by", "window", "chart", "title"]}},
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
(group_by time, chart line) for metrics with a time axis, a bar chart to compare categories. A chart title says
what is drawn: call it a trend only when group_by is time, otherwise name the grouping ("per tenant", "by
category"). Write the title and chart titles in {language}. Return JSON only.

{catalog}"""


def design(prompt: str, user: str, feedback: str | None = None) -> dict:
    lang = language(prompt)
    # the language is named next to the request too: larger models follow it there for the dashboard title
    messages = [{"role": "system", "content": SYSTEM.format(language=lang, catalog=catalog_prompt())},
                {"role": "user", "content": f"{prompt}\n\n(Write the dashboard title and chart titles in {lang}.)"}]
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


MAX_TITLE = 120


def text(value, default: str) -> str:
    """A title from a spec: a string, at most MAX_TITLE characters, or the default."""
    return value.strip()[:MAX_TITLE] if isinstance(value, str) and value.strip() else default


def clean_item(item, chart: bool) -> tuple[str, str | None, str, str | None]:
    """(metric, group_by, window, note) for one spec item, or ValueError. A spec may come from a browser, not only
    from the schema-bound model, so every field is checked against the catalog before anything is compiled."""
    c = catalog()
    if not isinstance(item, dict):
        raise ValueError("not an object")
    mid, window, group_by = item.get("metric"), item.get("window", "all_time"), item.get("group_by")
    if not isinstance(mid, str) or mid not in c["metrics"]:
        raise ValueError(f"unknown metric {str(mid)[:40]!r}")
    if not isinstance(window, str) or window not in c["windows"]:
        raise ValueError(f"unknown window {str(window)[:40]!r}")
    note = None
    if window != "all_time" and not c["metrics"][mid].get("time"):
        window, note = "all_time", f"{mid} has no time axis; the window was ignored"
    if chart and (not isinstance(group_by, str) or group_by not in ("time", *c["dimensions"])):
        raise ValueError(f"unknown grouping {str(group_by)[:40]!r}")
    return mid, group_by if chart else None, window, note


def build(spec: dict, user: str) -> dict:
    """Compile, run and validate every item of a spec. Nothing the model chose reaches Trino unchecked."""
    c = catalog()
    spec = spec if isinstance(spec, dict) else {}
    kpis, charts, issues, seen = [], [], [], set()
    for item in (spec.get("kpis") if isinstance(spec.get("kpis"), list) else [])[:MAX_KPIS]:
        try:
            mid, _, window, note = clean_item(item, chart=False)
        except ValueError as e:
            issues.append(f"KPI: {e}")
            continue
        if (mid, window) in seen:
            continue
        seen.add((mid, window))
        try:
            sql, _ = compile_sql(mid, None, window)
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
    for item in (spec.get("charts") if isinstance(spec.get("charts"), list) else [])[:MAX_CHARTS]:
        try:
            mid, group_by, window, note = clean_item(item, chart=True)
        except ValueError as e:
            issues.append(f"Chart: {e}")
            continue
        if (mid, group_by, window) in seen:
            continue
        seen.add((mid, group_by, window))
        title = text(item.get("title"), c["metrics"][mid]["label"])
        try:
            sql, _ = compile_sql(mid, group_by, window)
            rows = run(sql, user)
        except (ValueError, text2sql.UnsafeSQL, TrinoUserError) as e:
            issues.append(f"Chart '{title}': {getattr(e, 'message', e)}")
            continue
        rows = [r for r in rows if r[1] is not None]
        if not rows:
            issues.append(f"Chart '{title}': no data visible to {user}")
            continue
        requested = item.get("chart") if item.get("chart") in ("line", "bar", "pie", "table") else "bar"
        charts.append({"title": title, "metric": mid, "label": c["metrics"][mid]["label"], "group_by": group_by,
                       "x_label": "Date" if group_by == "time" else c["dimensions"][group_by],
                       "window": window, "chart": fit_chart(requested, group_by, rows),
                       "format": c["metrics"][mid]["format"], "rows": rows, "sql": sql,
                       **({"note": note} if note else {})})
    return {"title": text(spec.get("title"), "Dashboard"), "kpis": kpis, "charts": charts, "issues": issues}


def figure(value, lang: str) -> str:
    """A KPI value written the way the summary should write it: '1,371,000,000' or '1.371.000.000', '47.1' or
    '47,1'. Given in the reply's own style, the model copies figures instead of reformatting (and garbling) them."""
    if not isinstance(value, (int, float)):
        return str(value)
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.translate(str.maketrans(",.", ".,")) if lang == "Indonesian" else text


def summarize(prompt: str, dashboard: dict, user: str) -> str | None:
    """Two or three sentences over the KPI values; dropped if it states a number that is not in them."""
    if not dashboard["kpis"]:
        return None
    lang = language(prompt)
    facts = "\n".join(f"- {k['label']} ({k['window'].replace('_', ' ')}): {figure(k['value'], lang)}"
                      for k in dashboard["kpis"])
    msg = llm.chat([{"role": "system", "content": "Write a 2-3 sentence executive summary using only these "
                     f"figures, in {lang}. Copy each figure exactly as written; do not round, convert or add "
                     "numbers."},
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
        spec = catalog()["default_dashboard"]  # the spec returned is what was built, so it can be published
        dash = build(spec, user)
    dash.update(prompt=prompt, user=user, spec=spec, repaired=repaired, fallback=fallback,
                summary=summarize(prompt, dash, user))
    return dash
