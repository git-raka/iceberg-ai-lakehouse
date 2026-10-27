"""Score text-to-SQL against the evaluation set (`make eval`)."""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from . import text2sql
from .trino_client import query


def _values(rows: list[list]) -> set[tuple]:
    """Order- and name-insensitive comparison: each row as a sorted tuple of rounded, stringified values."""
    def norm(v):
        if isinstance(v, float):
            return f"{round(v, 1):g}"
        return str(v)
    return {tuple(sorted(norm(v) for v in r)) for r in rows}


def matches(expected: list[list], got: list[list]) -> bool:
    """Pass when every expected row appears in the answer (extra descriptive columns are fine)."""
    if not expected:
        return not got
    exp_rows, got_rows = _values(expected), [set(r) for r in _values(got)]
    return len(got) == len(expected) and all(any(set(e) <= g for g in got_rows) for e in exp_rows)


def run(user: str = "admin") -> dict:
    cases = yaml.safe_load(Path(__file__).with_name("eval_set.yaml").read_text(encoding="utf-8"))
    results, t0 = [], time.monotonic()
    for case in cases:
        answer = text2sql.ask(case["question"], user)
        if case.get("unanswerable"):  # the right answer is "the tables do not hold this"
            ok = bool(answer.get("unanswerable"))
        else:
            expected = query(case["reference"], user=user).rows
            ok = not answer.get("error") and not answer.get("unanswerable") and matches(expected, answer["rows"])
        results.append({"question": case["question"], "passed": ok, "sql": answer.get("sql"),
                        "error": answer.get("error") or answer.get("unanswerable")})
        detail = answer.get("sql") or f"(unanswerable: {answer.get('unanswerable')})"
        print(f"{'PASS' if ok else 'FAIL'}  {case['question']}\n      {detail}", flush=True)
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed ({100 * passed // len(results)}%) in {time.monotonic() - t0:.0f}s")
    return {"passed": passed, "total": len(results), "results": results}
