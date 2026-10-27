"""AI layer C: questions in plain language -> governed SQL.

The LLM writes Trino SQL grounded on the semantic model. Before anything runs, the SQL is parsed and
checked: one read-only SELECT, only tables from the semantic model, a row limit. It then runs in Trino
as the person asking, so row filters and column masks still apply. A failing query gets one repair try.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp
from trino.exceptions import TrinoUserError

from . import llm
from .trino_client import Result, query

MAX_ROWS = 200
FORBIDDEN = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
                                              "Command", "Grant", "TruncateTable") if hasattr(exp, n))


class UnsafeSQL(ValueError):
    pass


@lru_cache
def model() -> dict:
    return yaml.safe_load((Path(__file__).with_name("semantic_model.yaml")).read_text(encoding="utf-8"))


def allowed_tables() -> set[str]:
    return set(model()["tables"])


def schema_prompt() -> str:
    m = model()
    parts = ["Tables (catalog iceberg):"]
    for name, t in m["tables"].items():
        cols = ", ".join(f"{c} ({d})" for c, d in t["columns"].items())
        parts.append(f"- {name}: {t['description']}\n  columns: {cols}")
    parts.append("Notes:\n" + "\n".join(f"- {n}" for n in m["notes"]))
    parts.append("Examples:\n" + "\n".join(f"Q: {e['question']}\nSQL: {e['sql'].strip()}" for e in m["examples"]))
    return "\n".join(parts)


def guard(sql: str) -> str:
    """Return a safe, limited version of `sql` or raise UnsafeSQL."""
    sql = sql.strip().rstrip(";").strip()
    try:
        statements = sqlglot.parse(sql, read="trino")
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"cannot parse SQL: {e}") from e
    if len(statements) != 1 or statements[0] is None:
        raise UnsafeSQL("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQL("only SELECT queries are allowed")
    if any(tree.find_all(*FORBIDDEN)):
        raise UnsafeSQL("only read-only queries are allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    allowed = allowed_tables()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            raise UnsafeSQL("table functions are not allowed")
        if name in ctes and not table.db:
            continue
        full = f"{table.db}.{name}".lower() if table.db else name
        if table.catalog and table.catalog.lower() != "iceberg":
            raise UnsafeSQL(f"catalog {table.catalog} is not allowed")
        if full not in allowed:
            raise UnsafeSQL(f"table {full} is not in the semantic model")
    if not tree.args.get("limit"):
        tree = tree.limit(MAX_ROWS)
    return tree.sql(dialect="trino")


SQL_SCHEMA = {"type": "object", "properties": {"answerable": {"type": "boolean"}, "reason": {"type": "string"},
                                              "sql": {"type": "string"}}, "required": ["answerable", "sql"]}


class Unanswerable(Exception):
    """The tables hold no data for the question; better said than answered with an unrelated query."""


def generate_sql(question: str, user: str, error: str | None = None, previous: str | None = None) -> str:
    system = ("You write one Trino SQL SELECT statement that answers the question using only these tables. "
              "Qualify tables with their schema (gold.x or silver.x). If no column holds what the question asks "
              "for (for example revenue, or what a contract clause says), do not write a query for something "
              "else: return answerable=false with a short reason and an empty sql. "
              "Return JSON {\"answerable\": true|false, \"reason\": \"...\", \"sql\": \"...\"}.\n\n"
              + schema_prompt())
    content = f"Question: {question}"
    if user.startswith("tenant_"):  # row filters already limit the rows; this only resolves "my" and "our"
        tid = user.removeprefix("tenant_").upper()
        content += (f"\nThe question is asked by tenant {tid}: 'my', 'our', 'saya', 'kami' mean "
                    f"tenant_id = '{tid}'.")
    if error:
        content += f"\n\nThe previous SQL failed.\nSQL: {previous}\nError: {error}\nReturn a corrected query."
    msg = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": content}],
                   purpose="text2sql", user=user, schema=SQL_SCHEMA)
    try:
        out = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return msg.content
    if out.get("answerable") is False or not str(out.get("sql", "")).strip():
        raise Unanswerable(out.get("reason") or "the tables do not hold this information")
    return out["sql"]


def ask(question: str, user: str) -> dict:
    """Generate, check and run SQL for `question` as `user`. Returns the SQL, the result and attempts."""
    error, sql, safe = None, None, None
    for attempt in (1, 2):
        try:
            sql = generate_sql(question, user, error, sql)
        except Unanswerable as e:
            return {"question": question, "sql": None, "unanswerable": str(e), "columns": [], "rows": [],
                    "attempts": attempt}
        try:
            safe = guard(sql)
            result: Result = query(safe, user=user, max_rows=MAX_ROWS)
            return {"question": question, "sql": safe, "columns": result.columns, "rows": result.rows,
                    "attempts": attempt}
        except (UnsafeSQL, TrinoUserError) as e:
            error = str(e)[:500]
    return {"question": question, "sql": safe or sql, "error": error, "columns": [], "rows": [], "attempts": 2}
