# The AI layer

Where a large language model fits in a lakehouse, and how this project keeps it governed.
The model never lives in storage. It is a compute service that reads governed data and writes
results back to the lakehouse with lineage.

| | Where | What the LLM does | Here | Managed-platform equivalent |
|---|---|---|---|---|
| **A** | Processing → Silver | Reads unstructured documents: text layer or OCR, then typed field extraction with a JSON schema | `ai/documents.py` → `silver.document_text`, `silver.document_fields` | AI SQL functions (`ai_query`, Cortex AI functions) |
| **B** | Gold → retrieval | Embeds document chunks for semantic search | `ai/vectors.py` → `gold.document_chunk` (Iceberg) + `doc_chunk` (pgvector) | Vector Search, Cortex Search |
| **C** | Consumption | Text-to-SQL grounded on a semantic model | `ai/text2sql.py` + `ai/semantic_model.yaml` | Genie, Cortex Analyst |
| **D** | Consumption | An agent that calls tools (SQL, document search) and an MCP server for any MCP client | `ai/agent.py`, `ai/mcp_server.py` | Agent frameworks, managed MCP servers |

## A. Documents to structured data

1. **Text.** `pdfplumber` reads the text layer. If there is none (a scan), the pages are rendered and OCR'd
   with Tesseract. `method` records which one was used.
2. **Fields.** The local model returns JSON that must match a schema (`format=` in Ollama), at temperature 0.
   The prompt says to return `null` instead of guessing.
3. **Validation.** Plain rules, not the model, decide if a result is trustworthy: dates must parse, the end
   must come after the start, the tenant in the text must match the tenant in the file name, and a lease
   needs start, end and fee. Anything else sets `needs_review` and a readable `review_reason`.
4. **Lineage.** Every row stores `model`, `prompt_version`, `parser_version` and `extracted_at`. Changing the
   prompt version or the model re-extracts automatically. Old results stay in the table for comparison.

`gold.contract_expiry` and the dashboard show the extracted lease dates together with the review flag. A
person can always see which values came from AI and which need checking.

## B. Semantic search

Chunks of about 800 characters, with overlap, are embedded with `nomic-embed-text`. The chunk text is stored
in Iceberg (`gold.document_chunk`), which is the source of truth. Vectors go into pgvector with an HNSW
index. The pipeline re-embeds any chunk that is in Iceberg but missing from pgvector, for example after a new
Postgres or a different embedding model.

**Governance.** Before ranking, the search asks Trino, *as the caller*, which documents are visible. Row
filters decide the answer, so a tenant can never retrieve another tenant's contract through the AI. Only the
latest version of each document is searched.

## C. Text-to-SQL

The semantic model (`semantic_model.yaml`) describes tables, columns, units and example questions. The model
writes Trino SQL from it. Before anything runs, `sqlglot` parses the SQL and the guard enforces these rules:

- exactly one statement, and it must be a `SELECT`, `UNION` or CTE;
- no writes or DDL anywhere in the tree;
- only tables listed in the semantic model, in the `iceberg` catalog (raw PII tables are not listed);
- a `LIMIT` is added when missing.

The query then runs in Trino as the caller. A failing query gets one repair attempt, with the error message
fed back to the model.

## D. Agent and MCP

The agent has three tools: `query_data` (C), `search_documents` (B) and `list_datasets`. It loops at most five
times. Its system prompt tells it to answer from tool results only and to treat masked values (`***`, `NULL`)
as restricted, never guessed.

The MCP server exposes `list_datasets`, `describe_table`, `run_sql` (guarded), `ask_data` (C) and
`search_documents` (B). It runs as one identity chosen by an API key. Use stdio for a desktop client, or
streamable HTTP on port 8765.

```bash
LAKEHOUSE_API_KEY=<a key from API_KEYS_JSON> python -m lakehouse.ai.mcp_server --stdio
```

## Observability

Every model call goes through `ai/llm.py`, which writes a row to `llm_trace` (Postgres `ai` database). Each row
records the purpose, user, model, prompt and response size, latency, tool calls and errors. Text content is
not stored, so the table is safe to keep for audit.

## Choosing a model

The defaults (`qwen2.5:7b` and `nomic-embed-text`) fit in 8 GB of VRAM. Any Ollama model with tool calling
works for the agent; set `LLM_MODEL` in `.env`. Larger models give better text-to-SQL and fewer documents in
the review queue. The validation and governance rules stay the same whichever model you choose.
