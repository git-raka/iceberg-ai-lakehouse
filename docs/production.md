# From simulation to production

This repository is a reference implementation. It is deliberately small and runs on one machine.
This page lists what a real deployment needs.

| Area | In this repo | For production |
|---|---|---|
| Identity | Trusted `X-Trino-User` header, demo API keys | OIDC SSO (e.g. Keycloak brokering your identity provider, with MFA) for Trino, Superset, the API and the MCP server. Superset impersonates the logged-in user. |
| Catalog security | Lakekeeper with `allowall` authorization | Lakekeeper's OpenFGA authorization, or another REST catalog with fine-grained grants. Vended or short-lived storage credentials instead of static keys. |
| Policies | Trino file-based rules | The same rules in a policy engine with an audit trail (e.g. Apache Ranger or OPA). Keep them as code and review changes. |
| Encryption | Plain HTTP on localhost | TLS everywhere through a reverse proxy, TLS for Kafka, and encryption at rest on object storage. |
| Availability | Single containers | Kubernetes with the official Helm charts (Trino, Superset, Dagster, Flink operator), replicated object storage, Postgres with backups. |
| Table maintenance | Weekly compaction and snapshot expiry job | Tune per table: compaction targets, snapshot retention, orphan-file cleanup, and partitioning by query pattern. |
| AI quality | Validation rules, review queue, trace table | An evaluation set of real questions and documents, scored per model and prompt version before changing either. Guardrails for prompt injection inside documents. Human sign-off on extracted contract terms. |
| AI serving | Ollama on one GPU | vLLM or another inference server with batching on dedicated GPUs, an LLM gateway (rate limits, cost tracking, model routing) and redundancy. |
| Vector search | pgvector in the shared Postgres | A dedicated pgvector instance or a vector database once the volume grows. Keep Iceberg as the source of truth. |
| Observability | Logs, Dagster checks | Metrics (Prometheus/Grafana), central logs, alerting on check failures and data freshness. |
| Lineage & catalog | Dagster asset lineage | OpenLineage into a data catalog (e.g. OpenMetadata or DataHub) for business glossary and ownership. |

## Interoperability with managed platforms

Iceberg is the common format of the major managed lakehouse platforms. Keeping data as Iceberg tables in
storage you own, behind an Iceberg REST catalog, means:

- a managed platform can be the primary engine while this open-source stack reads the same tables, as a
  standby or for specific workloads;
- moving between platforms is a catalog and compute change, not a data migration.

Keep one writer per table, which means one catalog in charge of commits. Keep pipeline logic in portable SQL
or PySpark. Keep governance rules as code, so they can be re-applied on another engine.
