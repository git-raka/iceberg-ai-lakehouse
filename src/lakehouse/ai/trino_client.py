"""Run SQL in Trino *as the calling user*, so row filters and column masks apply to people and AI alike."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import trino

from ..settings import settings


@dataclass
class Result:
    columns: list[str]
    rows: list[list]

    def as_dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def to_text(self, max_rows: int = 50) -> str:
        """Compact table for an LLM prompt."""
        if not self.rows:
            return "(no data visible to this user: no rows match. Do not state any value.)"
        if all(v is None for r in self.rows for v in r):
            return ("(no data visible to this user: every value is NULL, so it is missing or restricted by "
                    "access policies. Do not state any value.)")
        lines = [" | ".join(self.columns)]
        lines += [" | ".join("" if v is None else str(v) for v in r) for r in self.rows[:max_rows]]
        if len(self.rows) > max_rows:
            lines.append(f"... {len(self.rows) - max_rows} more rows")
        return "\n".join(lines)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def query(sql: str, user: str, max_rows: int = 1000) -> Result:
    s = settings()
    # no default schema: every table must be schema-qualified, so a name can only resolve to what the
    # SQL guard checked (an unqualified name can then only be a CTE)
    conn = trino.dbapi.connect(host=s.trino_host, port=s.trino_port, user=user, catalog="iceberg",
                               http_scheme="http")
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(max_rows)
        cols = [d[0] for d in cur.description or []]
        return Result(cols, [[_jsonable(v) for v in r] for r in rows])
    finally:
        conn.close()
