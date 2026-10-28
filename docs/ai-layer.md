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
| **E** | Consumption | Designs a dashboard from a prompt by choosing metrics from a catalog | `ai/dashboard.py` + `ai/dashboard_catalog.yaml` | AI/BI dashboards, generative BI |

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

The agent has three tools: `query_data` (C), `search_documents` (B) and `list_datasets`. It loops at most six
times. Its system prompt tells it to answer from tool results only and to treat masked values (`***`, `NULL`)
as restricted, never guessed.

A 7B model does not follow a prompt reliably, so the rules that matter are code in `agent.py`, not prompt text:

| Check | What it prevents |
|---|---|
| Write requests (`DROP`, `DELETE FROM`, ...) and raw SQL in a question are refused before the model sees them | An injected instruction making the agent claim it changed or dropped something |
| An on-topic answer given without a tool makes the agent call the tool itself (`query_data`, or `search_documents` for what a document says) | Answers from memory, and "no data" answers when the data exists |
| Document search is restricted to the tenants named in the question | Answering about one tenant from another tenant's lease |
| Every number and every whole date in the answer must appear in the tool results | Invented values, including a date assembled from a day and a year found in different documents |
| A refusal while the query returned visible values gets a second, tool-free answer from the results | Small models over-applying "restricted" |
| For a tenant identity, results carry a note that only its own rows are visible | Presenting one row as a ranking of all tenants |
| The reply language is detected from the question | Answers in a third language |

Questions outside the estate's data get a short note on what the assistant can answer, with no tool call.

The MCP server exposes `list_datasets`, `describe_table`, `run_sql` (guarded), `ask_data` (C) and
`search_documents` (B). It runs as one identity chosen by an API key. Use stdio for a desktop client, or
streamable HTTP on port 8765.

```bash
LAKEHOUSE_API_KEY=<a key from API_KEYS_JSON> python -m lakehouse.ai.mcp_server --stdio
```

## E. Prompt to dashboard

A request such as "buatkan dashboard eksekutif summary dari data yang kita punya" becomes a dashboard. The
model only *chooses*; code builds and checks everything:

1. **Spec.** The model returns JSON that must match a schema built from `dashboard_catalog.yaml`: up to 6 KPI
   tiles and 8 charts, each a metric id, a grouping, a time window and a chart type, all as enums. It never
   writes SQL, so it cannot ask for a table or a column the catalog does not list.
2. **Compile and run.** Each item becomes SQL from the catalog definition, passes the same SQL guard as
   text-to-SQL and runs in Trino *as the caller*. A tenant gets a dashboard of its own rows; a metric its role
   may not read (headcount, procurement) is left out with the reason.
3. **Validate.** Items with no data, an access error or a grouping the metric does not support are dropped and
   listed. The chart type is fitted to the data: a time axis is a line, at most 8 non-negative slices may be a
   pie, more than 25 rows become a table. If nothing usable comes back, the model gets one repair round with
   the reasons; after that the catalog's default dashboard is shown.
4. **Summary.** Two or three sentences over the KPI values. As in the agent, every number must appear in those
   values, or the summary is dropped. Title and summary are written in the language of the prompt.

The page `http://localhost:8000/dashboard` renders the result with ECharts. It is also embedded at the top of
the Superset executive dashboard. **Create in Superset** turns the result into a real Superset dashboard:

- The spec comes back from the browser, so the API treats it like model output: every metric, grouping and
  window must be in the catalog, titles are cut to 120 characters, and the items are compiled, checked and run
  again as the caller. SQL, results and the summary from the browser are never used; the summary is written
  again on the server.
- Each item becomes a virtual dataset (its compiled SQL) and a chart.
- The datasets use a Superset database connection for the caller's identity
  (`trino://<identity>@trino:8080/iceberg`), so the published dashboard keeps that identity's row filters and
  masks. `admin` (unfiltered) and vendor identities cannot publish.
- The prompt and the summary are shown as plain text: HTML and Markdown in them are escaped.
- Publishing the same result again opens the existing dashboard instead of creating a copy. A publish that fails
  half-way removes the datasets and charts it had created.

## Observability

Every model call goes through `ai/llm.py`, which writes a row to `llm_trace` (Postgres `ai` database). Each row
records the purpose, user, model, prompt and response size, latency, tool calls and errors. Text content is
not stored, so the table is safe to keep for audit.

## Choosing a model

The defaults (`qwen2.5:7b` and `nomic-embed-text`) fit in 8 GB of VRAM. Any Ollama model with tool calling
works for the agent; set `LLM_MODEL` in `.env`. Larger models give better text-to-SQL and fewer documents in
the review queue. The validation and governance rules stay the same whichever model you choose.
