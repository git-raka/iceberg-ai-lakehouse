"""Command line used by the Makefile: `python -m lakehouse.cli <command>`."""

from __future__ import annotations

import argparse
import json
import time


def _print(title: str, value) -> None:
    print(f"{title}: {json.dumps(value, default=str)}", flush=True)


def pipeline() -> None:
    """The whole batch pipeline in dependency order (what the Dagster job runs)."""
    from .ai import documents, vectors
    from .pipeline import bronze, transform

    t0 = time.monotonic()
    _print("bronze.erp (CDC)", bronze.ingest_erp())
    _print("bronze.landing", bronze.ingest_landing_files())
    _print("bronze.documents", bronze.ingest_documents())
    _print("silver", transform.silver())
    documents.ensure_tables()
    _print("ai.document_text (OCR)", documents.extract_text())
    _print("ai.document_fields (LLM)", documents.extract_fields())
    _print("gold", transform.gold())
    _print("ai.document_index (embeddings)", vectors.index_documents())
    print(f"pipeline finished in {time.monotonic() - t0:.0f}s")


def run_summary() -> None:
    """Steps, asset metadata and check results of the latest Dagster run (`make pipeline` prints this)."""
    import dagster as dg

    instance = dg.DagsterInstance.get()
    run = instance.get_runs(limit=1)[0]
    print(f"run {run.run_id[:8]}  {run.job_name}  {run.status.value}")
    for e in instance.all_logs(run.run_id):
        d = e.dagster_event
        if d is None:
            continue
        if d.event_type_value == "ASSET_MATERIALIZATION":
            m = d.event_specific_data.materialization
            meta = ", ".join(f"{k}={getattr(v, 'value', v)}" for k, v in m.metadata.items())
            print(f"  materialized  {m.asset_key.to_user_string():<18} {meta}")
        elif d.event_type_value == "ASSET_CHECK_EVALUATION":
            c = d.event_specific_data
            outcome = "passed" if c.passed else f"FAILED ({c.severity.value.lower() if c.severity else 'error'})"
            print(f"  check         {c.check_name:<26} {outcome}")
        elif d.event_type_value == "STEP_FAILURE":
            print(f"  FAILED        {d.step_key}: {e.message[:200]}")


def _show(sql: str, user: str) -> None:
    from .ai.trino_client import query
    try:
        r = query(sql, user)
        print("    " + " | ".join(r.columns))
        for row in r.rows:
            print("    " + " | ".join("NULL" if v is None else str(v) for v in row))
    except Exception as e:  # access denied is part of the demo
        print("    " + str(getattr(e, "message", e)).splitlines()[0][:150])


def governance() -> None:
    """The same three queries as four identities: row filters and column masks in Trino."""
    queries = [
        "SELECT tenant_id, pic_phone, tax_id FROM silver.tenant ORDER BY tenant_id LIMIT 3",
        "SELECT count(*) AS tenants_visible FROM gold.tenant_overview",
        "SELECT full_name, national_id, salary FROM silver.employee ORDER BY employee_id LIMIT 2",
    ]
    for user in ("admin", "executive", "analyst_finance", "tenant_t003"):
        print(f"\n=== as {user}")
        for sql in queries:
            print(f"  {sql}")
            _show(sql, user)


def timetravel() -> None:
    """Every Iceberg snapshot of silver.tenant, and what tenant T003's contact phone was at each one."""
    from .ai.trino_client import query
    print("snapshots of silver.tenant (T003's pic_phone as of each snapshot):")
    snaps = query('SELECT snapshot_id, committed_at, operation FROM silver."tenant$snapshots" ORDER BY committed_at',
                  "admin").rows
    for sid, ts, op in snaps:
        r = query(f"SELECT pic_phone FROM silver.tenant FOR VERSION AS OF {sid} WHERE tenant_id = 'T003'", "admin")
        print(f"  {ts}  {op:<9}  {sid:>20}  {r.rows[0][0] if r.rows else '(no row yet)'}")
    print("\nbronze.erp_tenant keeps every captured version (CDC history):")
    _show("SELECT tenant_id, pic_phone, updated_at, _ingested_at FROM bronze.erp_tenant "
          "WHERE tenant_id = 'T003' ORDER BY updated_at", "admin")


def main() -> None:
    ap = argparse.ArgumentParser(prog="lakehouse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="generate all synthetic sources")
    sub.add_parser("increment", help="simulate a day of changes (CDC demo)")
    sub.add_parser("pipeline", help="run bronze -> silver -> AI -> gold -> embeddings")
    sub.add_parser("models", help="pull the LLM and embedding models into Ollama")
    sub.add_parser("iot", help="run the e-bus telemetry simulator")
    sub.add_parser("superset", help="provision the Superset database, datasets and dashboard")
    sub.add_parser("eval", help="score text-to-SQL against the evaluation set")
    sub.add_parser("run-summary", help="steps and checks of the latest Dagster run")
    sub.add_parser("governance", help="run the same queries as four identities")
    sub.add_parser("timetravel", help="show Iceberg snapshots and CDC history of one tenant")
    ask = sub.add_parser("ask", help="ask the agent a question")
    ask.add_argument("question")
    ask.add_argument("--user", default="executive")
    a = ap.parse_args()

    if a.cmd == "seed":
        from .sources.seed import seed_all
        _print("seeded", seed_all())
    elif a.cmd == "increment":
        from .sources.seed import increment
        _print("changed", increment())
    elif a.cmd == "pipeline":
        pipeline()
    elif a.cmd == "models":
        from .ai.llm import ensure_models
        _print("pulled", ensure_models())
    elif a.cmd == "iot":
        from .sources.iot import run
        run()
    elif a.cmd == "superset":
        from .superset_setup import provision
        _print("superset", provision())
    elif a.cmd == "eval":
        from .ai.evaluate import run as run_eval
        run_eval()
    elif a.cmd == "run-summary":
        run_summary()
    elif a.cmd == "governance":
        governance()
    elif a.cmd == "timetravel":
        timetravel()
    elif a.cmd == "ask":
        from .ai.agent import ask as agent_ask
        r = agent_ask(a.question, a.user)
        print(r["answer"])
        for s in r["steps"]:
            print(f"  - {s['tool']} {json.dumps(s['args'])}" + (f"\n    SQL: {s['sql']}" if s.get("sql") else "")
                  + (f"\n    error: {str(s['error'])[:160]}" if s.get("error") else ""))


if __name__ == "__main__":
    main()
