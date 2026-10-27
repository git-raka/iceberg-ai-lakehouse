"""A thin LLM gateway: every call to the local model goes through here and is traced.

The trace (Postgres `ai.llm_trace`) records who asked, for what purpose, which model answered,
how long it took and which tools were called. It never stores document text or answers in full,
only sizes, so it is safe to keep for audit.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

import ollama
import psycopg

from ..settings import settings

TRACE_DDL = """
CREATE TABLE IF NOT EXISTS llm_trace (
  id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), purpose text, user_name text, model text,
  prompt_chars int, response_chars int, latency_ms int, tool_calls text, ok boolean, error text)"""


@lru_cache
def client() -> ollama.Client:
    return ollama.Client(host=settings().ollama_host, timeout=600)


def _trace(**row: Any) -> None:
    try:
        with psycopg.connect(settings().pg_dsn("ai"), autocommit=True) as conn:
            conn.execute(TRACE_DDL)
            conn.execute(
                "INSERT INTO llm_trace (purpose, user_name, model, prompt_chars, response_chars, latency_ms, "
                "tool_calls, ok, error) VALUES (%(purpose)s, %(user)s, %(model)s, %(prompt_chars)s, "
                "%(response_chars)s, %(latency_ms)s, %(tool_calls)s, %(ok)s, %(error)s)", row)
    except psycopg.Error:
        pass  # tracing must never break the caller


def chat(messages: list[dict], *, purpose: str, user: str = "pipeline", tools: list | None = None,
         schema: dict | None = None, model: str | None = None) -> ollama.Message:
    """One chat completion with deterministic settings; `schema` forces JSON output of that shape."""
    model = model or settings().llm_model
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    started = time.monotonic()
    try:
        resp = client().chat(model=model, messages=messages, tools=tools, format=schema,
                             options={"temperature": 0, "num_ctx": 8192})
    except Exception as exc:
        _trace(purpose=purpose, user=user, model=model, prompt_chars=prompt_chars, response_chars=0,
               latency_ms=int((time.monotonic() - started) * 1000), tool_calls=None, ok=False, error=str(exc)[:300])
        raise
    msg = resp.message
    calls = [c.function.name for c in (msg.tool_calls or [])]
    _trace(purpose=purpose, user=user, model=model, prompt_chars=prompt_chars,
           response_chars=len(msg.content or ""), latency_ms=int((time.monotonic() - started) * 1000),
           tool_calls=json.dumps(calls) if calls else None, ok=True, error=None)
    return msg


def embed(texts: list[str], *, purpose: str = "embedding", user: str = "pipeline") -> list[list[float]]:
    model = settings().embed_model
    started = time.monotonic()
    resp = client().embed(model=model, input=texts)
    _trace(purpose=purpose, user=user, model=model, prompt_chars=sum(map(len, texts)),
           response_chars=0, latency_ms=int((time.monotonic() - started) * 1000), tool_calls=None, ok=True, error=None)
    return [list(v) for v in resp.embeddings]


def ensure_models() -> list[str]:
    """Pull the configured models into Ollama if they are missing (first run downloads a few GB)."""
    have = {m.model for m in client().list().models}
    pulled = []
    for name in (settings().llm_model, settings().embed_model):
        if name not in have and f"{name}:latest" not in have:
            client().pull(name)
            pulled.append(name)
    return pulled
