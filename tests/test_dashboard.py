"""Unit tests for prompt-to-dashboard: compile, validate and fit charts without a model or Trino in the loop."""

from types import SimpleNamespace

import pytest
from trino.exceptions import TrinoUserError

from lakehouse import superset_setup
from lakehouse.ai import dashboard, text2sql


# --------------------------------------------------------------------------- catalog and compile

def test_schema_only_offers_catalog_values():
    schema = dashboard.spec_schema()
    chart = schema["properties"]["charts"]["items"]["properties"]
    assert chart["metric"]["enum"] == sorted(dashboard.catalog()["metrics"])
    assert set(chart["group_by"]["enum"]) == {"time", *dashboard.catalog()["dimensions"]}
    assert set(chart["window"]["enum"]) == set(dashboard.catalog()["windows"])


def test_every_catalog_metric_compiles_to_sql_the_guard_accepts():
    c = dashboard.catalog()
    for mid, m in c["metrics"].items():
        for group_by in [None, *m["dims"], *(["time"] if m.get("time") else [])]:
            sql, _ = dashboard.compile_sql(mid, group_by, "last_30_days")
            assert "LIMIT" in text2sql.guard(sql).upper(), (mid, group_by)


def test_window_applies_to_the_time_column():
    sql, note = dashboard.compile_sql("net_tons", "time", "last_7_days")
    assert "day >= current_date - INTERVAL '7' DAY" in sql and "GROUP BY 1 ORDER BY 1" in sql and note is None


def test_window_on_a_metric_without_time_axis_is_ignored_with_a_note():
    sql, note = dashboard.compile_sql("tenants", None, "last_30_days")
    assert "WHERE TRUE" in sql and "no time axis" in note


@pytest.mark.parametrize("metric, group_by", [("tenants", "time"), ("net_tons", "department"),
                                              ("leases_to_review", "tenant_id")])
def test_impossible_groupings_are_refused(metric, group_by):
    with pytest.raises(ValueError):
        dashboard.compile_sql(metric, group_by, "all_time")


# --------------------------------------------------------------------------- fit the chart to the data

@pytest.mark.parametrize("requested, group_by, n, expected", [
    ("bar", "time", 30, "line"),       # a trend is a line whatever was asked
    ("line", "time", 2, "bar"),        # two points are no trend
    ("pie", "category", 5, "pie"),
    ("pie", "tenant_id", 12, "bar"),   # too many slices
    ("line", "zone", 4, "bar"),        # categories are not a line
    ("bar", "tenant_id", 40, "table"),
    ("table", "sector", 3, "table"),
])
def test_fit_chart(requested, group_by, n, expected):
    rows = [[f"k{i}", i + 1] for i in range(n)]
    assert dashboard.fit_chart(requested, group_by, rows) == expected


def test_pie_with_negative_values_becomes_bar():
    assert dashboard.fit_chart("pie", "category", [["a", 3], ["b", -1]]) == "bar"


# --------------------------------------------------------------------------- validate a spec

def fake_trino(results: dict):
    """Stand-in for dashboard.run: rows by metric id (an exception is raised instead of returned)."""
    def run(sql, user):
        for mid, rows in results.items():
            if f"FROM {dashboard.catalog()['metrics'][mid]['table']} " in sql and \
                    dashboard.catalog()["metrics"][mid]["expr"] in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return []
    return run


def test_build_keeps_items_with_data_and_explains_the_rest(monkeypatch):
    denied = TrinoUserError({"message": "Access Denied: Cannot select from table gold.headcount",
                             "errorName": "PERMISSION_DENIED"}, "q1")
    monkeypatch.setattr(dashboard, "run", fake_trino({
        "tenants": [[24]], "headcount": denied, "open_tickets": [[None]],
        "net_tons": [["2026-10-01", 120.5], ["2026-10-02", None], ["2026-10-03", 98.0], ["2026-10-04", 101.0]]}))
    spec = {"title": "Ringkasan", "kpis": [{"metric": "tenants", "window": "all_time"},
                                           {"metric": "tenants", "window": "all_time"},       # duplicate
                                           {"metric": "headcount", "window": "all_time"},     # denied
                                           {"metric": "open_tickets", "window": "all_time"},  # no data
                                           {"metric": "made_up", "window": "all_time"}],      # not in catalog
            "charts": [{"title": "Tonase", "metric": "net_tons", "group_by": "time", "window": "last_30_days",
                        "chart": "pie"},
                       {"title": "Bad", "metric": "tenants", "group_by": "time", "window": "all_time", "chart": "line"}]}
    d = dashboard.build(spec, "tenant_t003")
    assert d["title"] == "Ringkasan"
    assert [k["metric"] for k in d["kpis"]] == ["tenants"] and d["kpis"][0]["value"] == 24
    assert len(d["charts"]) == 1 and d["charts"][0]["chart"] == "line"
    assert [r[1] for r in d["charts"][0]["rows"]] == [120.5, 98.0, 101.0]  # null points dropped
    issues = " | ".join(d["issues"])
    assert "Access Denied" in issues and "no data visible to tenant_t003" in issues and "no time axis" in issues
    assert "unknown metric 'made_up'" in issues


@pytest.mark.parametrize("item", [
    {"metric": "net_tons", "window": "last_30_days", "chart": "bar", "title": "no grouping"},
    {"metric": "net_tons", "group_by": "time", "window": "bogus", "chart": "line", "title": "bad window"},
    {"metric": "net_tons", "group_by": "time", "window": ["a"], "chart": "line", "title": "window list"},
    {"metric": ["net_tons"], "group_by": "time", "window": "all_time", "chart": "line", "title": "metric list"},
    {"metric": "net_tons", "group_by": "day; DROP TABLE x", "window": "all_time", "chart": "bar", "title": "sql"},
    "not an object",
])
def test_a_malformed_chart_from_a_browser_is_reported_not_crashed(monkeypatch, item):
    monkeypatch.setattr(dashboard, "run", lambda sql, user: pytest.fail(f"must not run: {sql}"))
    d = dashboard.build({"title": "t", "kpis": [], "charts": [item]}, "executive")
    assert not d["charts"] and len(d["issues"]) == 1


@pytest.mark.parametrize("item", [{"metric": "net_tons", "window": "bogus"}, {"metric": "net_tons", "window": 7},
                                  {"metric": None}, ["net_tons"]])
def test_a_malformed_kpi_from_a_browser_is_reported_not_crashed(monkeypatch, item):
    monkeypatch.setattr(dashboard, "run", lambda sql, user: pytest.fail(f"must not run: {sql}"))
    d = dashboard.build({"title": "t", "kpis": [item], "charts": "not a list"}, "executive")
    assert not d["kpis"] and len(d["issues"]) == 1


def test_spec_that_is_not_an_object_gives_an_empty_dashboard():
    assert dashboard.build(["x"], "executive") == {"title": "Dashboard", "kpis": [], "charts": [], "issues": []}


def test_window_of_a_metric_without_time_axis_does_not_duplicate_a_kpi(monkeypatch):
    monkeypatch.setattr(dashboard, "run", lambda sql, user: [[12]])
    d = dashboard.build({"kpis": [{"metric": "tenants", "window": "all_time"},
                                  {"metric": "tenants", "window": "last_30_days"}], "charts": []}, "executive")
    assert len(d["kpis"]) == 1 and d["kpis"][0]["window"] == "all_time"


def test_titles_are_shortened_and_chart_types_checked(monkeypatch):
    monkeypatch.setattr(dashboard, "run", lambda sql, user: [["a", 1], ["b", 2], ["c", 3]])
    d = dashboard.build({"title": "x" * 500, "kpis": [], "charts": [
        {"metric": "tickets", "group_by": "category", "window": "all_time", "chart": "<script>", "title": "y" * 500}]},
        "executive")
    assert len(d["title"]) == dashboard.MAX_TITLE and len(d["charts"][0]["title"]) == dashboard.MAX_TITLE
    assert d["charts"][0]["chart"] == "bar"


def test_build_caps_the_number_of_items(monkeypatch):
    monkeypatch.setattr(dashboard, "run", lambda sql, user: [[1]] if " AS value " in sql else [["a", 1]])
    metrics = list(dashboard.catalog()["metrics"])
    spec = {"title": "t", "kpis": [{"metric": m, "window": "all_time"} for m in metrics],
            "charts": [{"title": m, "metric": m, "group_by": dashboard.catalog()["metrics"][m]["dims"][0],
                        "window": "all_time", "chart": "bar"}
                       for m in metrics if dashboard.catalog()["metrics"][m]["dims"]]}
    d = dashboard.build(spec, "executive")
    assert len(d["kpis"]) == dashboard.MAX_KPIS and len(d["charts"]) == dashboard.MAX_CHARTS


def test_generate_falls_back_to_the_default_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard, "design", lambda prompt, user, feedback=None: {"title": "x", "kpis": [], "charts": []})
    monkeypatch.setattr(dashboard, "run", lambda sql, user: [[5]] if " AS value " in sql else [["a", 5]])
    monkeypatch.setattr(dashboard, "summarize", lambda prompt, dash, user: None)
    d = dashboard.generate("something unrelated", "executive")
    assert d["repaired"] and d["fallback"] and d["title"] == dashboard.catalog()["default_dashboard"]["title"]
    assert d["spec"] == dashboard.catalog()["default_dashboard"]  # what was built, so publishing it works


# --------------------------------------------------------------------------- reply language

@pytest.mark.parametrize("prompt, lang", [("buatkan dashboard eksekutif summary dari data yang kita punya", "Indonesian"),
                                          ("An executive summary of what we have", "English")])
def test_design_and_summary_name_the_prompt_language(monkeypatch, prompt, lang):
    seen = []

    def chat(messages, **kw):
        seen.append(messages[0]["content"])
        return SimpleNamespace(content='{"title": "t", "kpis": [], "charts": []}')
    monkeypatch.setattr(dashboard.llm, "chat", chat)
    dashboard.design(prompt, "executive")
    dashboard.summarize(prompt, {"kpis": [{"label": "Tenants", "window": "all_time", "value": 24}]}, "executive")
    assert all(f"in {lang}" in s for s in seen) and len(seen) == 2


@pytest.mark.parametrize("value, lang, text", [(1371000000, "Indonesian", "1.371.000.000"),
                                               (1371000000, "English", "1,371,000,000"),
                                               (47.1, "Indonesian", "47,1"), (18646.79, "English", "18,646.79"),
                                               (5944409000.0, "English", "5,944,409,000"), ("n/a", "English", "n/a")])
def test_figures_are_written_in_the_reply_style(value, lang, text):
    assert dashboard.figure(value, lang) == text


def test_summary_copying_the_figures_is_grounded(monkeypatch):
    kpis = [{"label": "Monthly service fees", "window": "all_time", "value": 1371000000},
            {"label": "Average hours to close a ticket", "window": "all_time", "value": 47.1}]
    text = "Biaya layanan bulanan 1.371.000.000 dan rata-rata 47,1 jam untuk menutup tiket."
    monkeypatch.setattr(dashboard.llm, "chat", lambda m, **kw: SimpleNamespace(content=text))
    assert dashboard.summarize("buatkan ringkasan", {"kpis": kpis}, "executive") == text


def test_summary_with_an_invented_number_is_dropped(monkeypatch):
    monkeypatch.setattr(dashboard.llm, "chat", lambda m, **kw: SimpleNamespace(content="There are 31 tenants."))
    assert dashboard.summarize("summary", {"kpis": [{"label": "Tenants", "window": "all_time", "value": 24}]},
                               "executive") is None


# --------------------------------------------------------------------------- Superset layout and charts

def test_grid_fills_rows_of_twelve_columns():
    rows = superset_setup.grid([{"w": 12}, {"w": 2}, {"w": 2}, {"w": 6}, {"w": 6}, {"w": 6}])
    assert [[i["w"] for i in r] for r in rows] == [[12], [2, 2, 6], [6, 6]]


def test_position_links_every_item_to_its_row():
    rows = superset_setup.grid([{"markdown": "top", "code": "hi", "w": 12, "h": 10},
                                {"chart": 7, "name": "Tons", "w": 6, "h": 50}])
    pos = superset_setup.position("Title", rows)
    assert pos["GRID_ID"]["children"] == ["ROW-0", "ROW-1"] and pos["HEADER_ID"]["meta"]["text"] == "Title"
    assert pos["ROW-1"]["children"] == ["CHART-7"] and pos["CHART-7"]["meta"]["chartId"] == 7
    assert pos["MARKDOWN-top"]["meta"]["code"] == "hi" and pos["MARKDOWN-top"]["parents"][-1] == "ROW-0"


@pytest.mark.parametrize("item, viz", [
    ({"label": "Tenants", "window": "all_time", "format": "number"}, "big_number_total"),
    ({"chart": "line", "group_by": "time", "label": "Net tons", "x_label": "Date", "format": "number"},
     "echarts_timeseries_line"),
    ({"chart": "bar", "group_by": "zone", "label": "Area", "x_label": "Estate zone", "format": "number"},
     "echarts_timeseries_bar"),
    ({"chart": "pie", "group_by": "category", "label": "Tickets", "x_label": "Category", "format": "number"}, "pie"),
    ({"chart": "table", "group_by": "tenant_id", "label": "Fees", "x_label": "Tenant", "format": "idr"}, "table"),
])
def test_ai_chart_types_and_their_saved_query(item, viz):
    got, params, w, _ = superset_setup.ai_chart(item)
    assert got == viz and 0 < w <= 12
    q = superset_setup.query_context(1, viz, params)["queries"][0]
    assert q.get("metrics") or q.get("columns")


def test_prompt_and_summary_are_plain_text_in_superset_markdown():
    text = superset_setup.plain('<iframe src="https://example.com/"></iframe> ![x](javascript:alert(1)) **bold**')
    assert "<" not in text and ">" not in text.replace("&gt;", "")
    assert "\\!\\[x\\]\\(javascript:alert\\(1\\)\\)" in text and "\\*\\*bold\\*\\*" in text


def dash_result(**over):
    d = {"prompt": "p", "title": "T", "kpis": [{"sql": "SELECT 1", "window": "all_time"}],
         "charts": [{"sql": "SELECT 2", "chart": "bar", "title": "c", "window": "all_time"}]}
    return d | over


def test_slug_changes_with_anything_visible_on_the_dashboard():
    base = superset_setup.slug_for(dash_result(), "executive")
    assert base == superset_setup.slug_for(dash_result(), "executive")
    assert base.startswith("ai-executive-")
    variants = [dash_result(prompt="q"), dash_result(title="U"),
                dash_result(charts=[{"sql": "SELECT 2", "chart": "pie", "title": "c", "window": "all_time"}]),
                dash_result(charts=[{"sql": "SELECT 2", "chart": "bar", "title": "d", "window": "all_time"}])]
    assert all(superset_setup.slug_for(v, "executive") != base for v in variants)
    assert superset_setup.slug_for(dash_result(), "tenant_t003") != base


def test_superset_connection_only_for_plain_identities():
    api = object.__new__(superset_setup.Superset)  # no login: the name check comes first
    with pytest.raises(ValueError):
        api.database("executive@evil:1/x")
