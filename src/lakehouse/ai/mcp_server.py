"""MCP server: exposes the governed lakehouse to any MCP-capable AI client.

The server acts as one identity, chosen by an API key (LAKEHOUSE_API_KEY), so Trino row filters and
column masks apply to everything the client asks. Run it over stdio for a desktop client:

    LAKEHOUSE_API_KEY=demo-exec-key python -m lakehouse.ai.mcp_server --stdio

or over streamable HTTP (the `mcp` service in docker-compose, port 8765).
"""

from __future__ import annotations

import argparse
import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from trino.exceptions import TrinoUserError

from ..settings import settings
from . import agent, text2sql, vectors
from .trino_client import query


def identity() -> str:
    key = os.environ.get("LAKEHOUSE_API_KEY", "")
    user = settings().api_keys.get(key)
    if not user:
        raise SystemExit("set LAKEHOUSE_API_KEY to one of the keys in API_KEYS_JSON")
    return user


def build(user: str) -> MCPServer:
    server = MCPServer(
        "iceberg-ai-lakehouse",
        instructions=(f"Governed access to a fictional industrial estate lakehouse as user '{user}'. "
                      "Use ask_data for numbers and lists, search_documents for contract wording, "
                      "list_datasets/describe_table to explore, run_sql for an exact read-only query."),
    )

    @server.tool()
    def list_datasets() -> str:
        """Tables you can query, with what they contain."""
        return agent.list_datasets()

    @server.tool()
    def describe_table(table: str) -> str:
        """Columns and their business meaning for one table, e.g. gold.tenant_overview."""
        t = text2sql.model()["tables"].get(table)
        if not t:
            return f"Unknown table {table}. Use list_datasets."
        return json.dumps({"table": table, "description": t["description"], "columns": t["columns"]}, indent=1)

    @server.tool()
    def run_sql(sql: str) -> str:
        """Run one read-only Trino SELECT over the tables from list_datasets (limited to 200 rows)."""
        try:
            safe = text2sql.guard(sql)
        except text2sql.UnsafeSQL as e:
            return f"Refused: {e}"
        try:
            r = query(safe, user=user, max_rows=text2sql.MAX_ROWS)
        except TrinoUserError as e:
            return f"Query failed: {e.message}\nSQL: {safe}"
        return f"SQL: {safe}\n{r.to_text(200)}"

    @server.tool()
    def ask_data(question: str) -> str:
        """Answer a question in plain language with generated, checked SQL (text-to-SQL)."""
        r = text2sql.ask(question, user)
        if r.get("unanswerable"):
            return f"The tables cannot answer this: {r['unanswerable']}"
        if r.get("error"):
            return f"Could not answer: {r['error']}\nLast SQL: {r['sql']}"
        from .trino_client import Result
        return f"SQL: {r['sql']}\n{Result(r['columns'], r['rows']).to_text(100)}"

    @server.tool()
    def search_documents(query_text: str, k: int = 5) -> str:
        """Semantic search over lease agreements and handover certificates you are allowed to see."""
        hits = vectors.search(query_text, user=user, k=max(1, min(int(k), 10)))
        return "\n\n".join(f"[{h['file_name']} #{h['chunk_no']} score={h['score']}]\n{h['text']}" for h in hits) \
            or "No matching documents."

    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true", help="serve over stdio instead of streamable HTTP")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    server = build(identity())
    if a.stdio:
        server.run()
    else:
        # listens on all interfaces inside the container, so DNS-rebinding protection must be explicit:
        # only requests addressed to this machine (the published 127.0.0.1 port) or the compose network pass
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "mcp:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"])
        server.run(transport="streamable-http", host="0.0.0.0", port=a.port, transport_security=security)


if __name__ == "__main__":
    main()
