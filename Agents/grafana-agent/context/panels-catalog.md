# Panels catalog — dashboard documentation

Reference context for the Grafana agent (see `skills/grafana-dashboards`): what
each dashboard in this deployment is *for*, its UID (for `get_dashboard_by_uid`
and `/d/<uid>` links), and the load-bearing panels with their queries. Source of
truth for panel JSON: `helm/observability/dashboards/` (kept in sync with the
compose stack's Grafana by CI, issue #257).

For *generic* visualization questions ("how do I read a heatmap?", "why does a
stat panel show a different number than my query?"), use the vendored official
Grafana docs in [`grafana-docs/`](grafana-docs/) — this catalog is only for what
*our* dashboards mean.

**Maintenance protocol:** when a dashboard is added or a key panel's query
changes, update the entry here in the same change — the agent answers from this
catalog first and only falls back to fetching full dashboard JSON when the
catalog doesn't cover the question (token conservation, INSTRUCTIONS §4.6).

---

## Routing table (which dashboard answers which question)

| The user asks about... | Dashboard | UID |
|---|---|---|
| "Is the system healthy overall?" | Nexus RAG Operations | `nexus-rag-operations` |
| Uploads, parsing, embedding, queue backlog | Ingestion pipeline | `nexus-rag-ingestion` |
| Search quality/volume, denials, reranker | Retrieval and reranking | `nexus-rag-retrieval` |
| Document counts, statuses, classifications | Documents | `nexus-rag-documents` |
| Chat UI, LiteLLM, generation models | Chat plane | `nexus-rag-chat-plane` |
| Logins, tokens, Keycloak health | Identity (Keycloak) | `nexus-rag-identity` |
| Postgres health | Datastores (Postgres) | `nexus-rag-datastores` |
| Qdrant / vector collections | Vector store | `nexus-rag-vector-store` |
| Milvus (opt-in backend, #160) | Milvus | `nexus-rag-milvus` |
| NATS JetStream queue internals | Messaging | `nexus-rag-messaging` |
| Raw logs per component | Component logs | `nexus-rag-component-logs` |
| Log volume trends, traces, span latency | Logs and traces | `nexus-rag-logs-traces` |
| End-to-end architecture with live rates | System flow | `nexus-rag-system-flow` |

## Dashboard entries

### Nexus RAG Operations — `nexus-rag-operations`
**Purpose:** the front door. One screen answering "is anything wrong right now".
**Key panels:**
| Panel | Query | Reading it |
|---|---|---|
| Application services up | `sum(up{job=~"nexus-rag-.*"})` | should equal the deployed service count |
| Worker consumer | `nexus_rag_ingestion_worker_consumer_running` | 1 = consuming; 0 = ingestion stalled |
| Queue hand-off age | `nexus_rag_ingestion_queue_oldest_unpublished_seconds` | sustained growth = hand-off stuck (issue #164 path) |
| Reranker fallback ratio | `sum(rate(nexus_rag_reranker_fallback_total[10m])) / clamp_min(sum(rate(nexus_rag_queries_total[10m])), 0.001)` | >0 means results are fused-order only — quality, not availability |

### Ingestion pipeline — `nexus-rag-ingestion`
**Purpose:** the upload → parse → embed → pending-review path (NFR-11 durability).
**Key panels:**
| Panel | Query | Reading it |
|---|---|---|
| Documents ingested (1h) | `sum(increase(nexus_rag_ingestion_worker_jobs_total{outcome="succeeded"}[1h]))` | throughput |
| Awaiting reconciliation | `nexus_rag_ingestion_queue_reconciliation_pending` | stuck Postgres→JetStream hand-offs |
| Queue hand-off age | (as overview) | crash-safety health |
| Stage timing | `nexus_rag_ingestion_worker_stage_seconds*` histograms, by `stage` | which stage (parse/chunk/embed/vector_upsert) eats the time |
**Correlates with:** `{service="ingestion-worker"}` logs; Messaging dashboard
when hand-off age grows.

### Retrieval and reranking — `nexus-rag-retrieval`
**Purpose:** the `rag_search` path — volume, outcomes, reranker behavior.
**Key panels:**
| Panel | Query | Reading it |
|---|---|---|
| Queries (1h) | `sum(increase(nexus_rag_queries_total[1h]))` | MCP tool-call volume |
| Denied (1h) | `sum(increase(nexus_rag_queries_total{outcome="denied"}[1h])) or vector(0)` | access-control denials (FR-26 working as intended — a spike is a *who*, not a *bug*, question) |
| Errored (1h) | `outcome=~"error\|unavailable"` variant | backend failures, incl. Qdrant unreachable |
| Reranker fallback ratio | (as overview, 30m windows) | reranker-service reachability |

### Documents — `nexus-rag-documents`
**Purpose:** corpus composition from Postgres (status lifecycle, classification
mix). Panels are datasource-driven (SQL), not PromQL — summarize from panel
titles; don't try to re-run these through `query_prometheus`.

### Chat plane — `nexus-rag-chat-plane`
**Purpose:** the user-facing stack this repo layers onto.
**Key panels:** LibreChat / LiteLLM / Ollama probes
(`max(probe_success{instance=~".*<svc>.*"}) or vector(0)`), MCP tool calls (1h).
**Note:** probe_success is blackbox reachability, not correctness — a 200 from a
broken model still probes 1.

### Identity (Keycloak) — `nexus-rag-identity`
**Purpose:** OIDC provider health. `up{job="keycloak"}`, password validations,
JVM heap, DB pool (`agroal_active_count`).
**Correlates with:** Component-logs Keycloak panels for *who/why* on failures:
`{service="keycloak"} |~ `type="LOGIN_ERROR"``.

### Datastores (Postgres) — `nexus-rag-datastores`
**Purpose:** system-of-record health. `pg_up`, `sum(pg_stat_activity_count)` vs
`pg_settings_max_connections`, deadlocks (1h).
**Reading it:** connections near max ⇒ suspect pool exhaustion before blaming
queries (pool_pre_ping work, #236).

### Vector store — `nexus-rag-vector-store` / Milvus — `nexus-rag-milvus`
**Purpose:** chunk-vector storage health per backend. Milvus dashboard is
all-zeros unless `VECTOR_BACKEND=milvus` (#160) — that's configuration, not an
outage; say so instead of reporting it as down.

### Messaging (NATS JetStream) — `nexus-rag-messaging`
**Purpose:** queue internals — streams/consumers/messages stored
(`jetstream_server_total_*`). First stop when ingestion stalls but the worker
consumer gauge reads 1.

### Component logs — `nexus-rag-component-logs`
**Purpose:** curated per-service Loki streams (ingestion-api, worker, MCP,
reranker, Keycloak events). Prefer these scoped selectors over ad-hoc queries.

### Logs and traces — `nexus-rag-logs-traces`
**Purpose:** cross-service log volume + OTel span metrics (#134).
**Key panels:**
| Panel | Query |
|---|---|
| Error/warn lines per service | ``sum by (service) (rate({compose_project="nexus-rag"} \|~ `(?i)\b(error\|warn\|warning\|exception\|traceback)\b` [5m]))`` |
| Span latency p95 by span name | `histogram_quantile(0.95, sum by (le, span_name) (rate(traces_spanmetrics_latency_bucket[5m])))` |

### System flow — `nexus-rag-system-flow`
**Purpose:** the architecture diagram with live rates on the arrows (ingestion
throughput, query rate, rerank calls). Best dashboard to *link* when a user asks
"how does this all fit together".

---

## Template for new entries

```markdown
### <Dashboard title> — `<uid>`
**Purpose:** <one sentence: what question does this dashboard answer>
**Key panels:**
| Panel | Query | Reading it |
|---|---|---|
| <title> | `<expr>` | <what a good/bad value means> |
**Correlates with:** <other dashboards/log streams to check together>
**Variables:** <template variables and sane defaults, if any — see INSTRUCTIONS §3.5>
```
