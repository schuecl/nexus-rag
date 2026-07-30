# Query development cookbook

Reference context for the Grafana agent (loaded on demand — see
`skills/grafana-metrics` and `skills/grafana-logs`). Every example uses **real
metrics and labels from this stack's dashboards** (`helm/observability/dashboards/`),
so answers can be cross-checked against the panels that already graph them.

The method in every example is the same senior-engineer loop:
**question → pick the signal → build the narrowest query stepwise → sanity-check
against a known panel → report with the exact query.**

---

## 1. Counter → rate → total (the bread-and-butter pattern)

**Question:** "How many RAG queries are we serving?"

```promql
# Step 1 — does the metric exist and what labels does it carry? (instant, cheap)
nexus_rag_queries_total

# Step 2 — traffic rate, per outcome (range, for a trend)
sum by (outcome) (rate(nexus_rag_queries_total[5m]))

# Step 3 — "how many in the last hour" wants increase(), not rate()
sum(increase(nexus_rag_queries_total[1h]))
```

Rules of thumb:
- `_total` suffix ⇒ counter ⇒ never read raw; always `rate()` (per-second) or
  `increase()` (count over window).
- `sum by (label)` keeps only the label the question asks about; naked `sum()`
  answers "overall".
- Cross-check: the Retrieval dashboard's "Queries (1h)" stat panel runs exactly
  step 3 — same number = query is right.

## 2. Error/denial ratio with a safe denominator

**Question:** "What fraction of queries are denied?"

```promql
sum(increase(nexus_rag_queries_total{outcome="denied"}[1h]))
/
clamp_min(sum(increase(nexus_rag_queries_total[1h])), 1)
```

- `clamp_min(x, 1)` prevents divide-by-zero on quiet systems — this stack's own
  "Reranker fallback ratio" panel uses the same idiom (`clamp_min(..., 0.001)`).
- `or vector(0)` is the companion idiom when a *numerator* series may not exist
  yet (no denials ever recorded): `sum(increase(...{outcome="denied"}[1h])) or vector(0)`.
  Dashboards here use it heavily; without it a stat panel shows "No data"
  instead of 0.

## 3. Latency percentiles from a histogram

**Question:** "What's the p95 span latency, per operation?"

```promql
histogram_quantile(
  0.95,
  sum by (le, span_name) (rate(traces_spanmetrics_latency_bucket[5m]))
)
```

- `_bucket` suffix ⇒ histogram ⇒ `histogram_quantile` over `rate()`d buckets.
- `le` MUST survive the `sum by (...)` — dropping it silently breaks the math.
- Add exactly one more label (`span_name` here) for per-thing percentiles;
  more labels = more series = slower and noisier.
- The Logs-and-traces dashboard's "Span latency p95 by span name" panel is the
  cross-check.

## 4. Health / up checks

**Question:** "Is everything running?"

```promql
sum(up{job=~"nexus-rag-.*"})                      # app services up (overview panel)
nexus_rag_ingestion_worker_consumer_running        # 1/0 gauge, worker specifically
max(probe_success{instance=~".*litellm.*"}) or vector(0)   # blackbox probe (chat plane)
```

Gauges (`up`, `_running`, `probe_success`) are read raw — no `rate()`. Use
`max(...)` when multiple scrapers might report the same target.

## 5. Pipeline-specific: ingestion health

**Question:** "Is ingestion keeping up?"

```promql
# Throughput: documents successfully processed, last hour
sum(increase(nexus_rag_ingestion_worker_jobs_total{outcome="succeeded"}[1h]))

# Failures in the same window (or vector(0): may not exist yet)
sum(increase(nexus_rag_ingestion_worker_jobs_total{outcome="failed"}[1h])) or vector(0)

# Backlog signal: age of the oldest unpublished hand-off (gauge, seconds)
nexus_rag_ingestion_queue_oldest_unpublished_seconds

# Stage timing: where time goes per document (histogram count as throughput)
sum by (stage) (rate(nexus_rag_ingestion_worker_stage_seconds_count[5m]))
```

Interpretation guide: rising `oldest_unpublished_seconds` + flat `succeeded`
rate = the worker is stuck or slow, go to logs (§7) for the worker only.

## 6. LogQL: from service to signal

**Question:** "What is the ingestion worker complaining about?"

```logql
# Step 1 — always selector + filter, never a bare selector
{service="ingestion-worker"} |= "ERROR"

# Step 2 — widen the net only if step 1 is empty (case-insensitive classes)
{service="ingestion-worker"} |~ `(?i)\b(error|warn|exception|traceback)\b`

# Step 3 — count instead of read, to find *when* it started
sum(count_over_time({service="ingestion-worker"} |= "ERROR" [5m]))
```

Real label keys in this stack: `service` (per component: `ingestion-api`,
`ingestion-worker`, `orchestration-mcp`, `reranker`, `keycloak`, ...) and
`compose_project="nexus-rag"` for everything. The Component-logs dashboard shows
per-service streams; Keycloak auth events use
`{service="keycloak"} |~ \`type="LOGIN_ERROR"\`` (Login outcomes panel).

## 7. Worked example: full triage (protocol §3.6 end-to-end)

**User asks:** "Retrieval feels slow since lunch."

```text
1. ALERTS   list_alert_rules → anything firing on retrieval/reranker? note times.
2. METRICS  pin the symptom:
            histogram_quantile(0.95, sum by (le) (rate(traces_spanmetrics_latency_bucket{service="orchestration-mcp"}[5m])))
            → range query 11:00–now; find the step change (say 12:40).
3. METRICS  suspects at 12:40: reranker fallback ratio (§2 idiom); Qdrant/Ollama
            probe_success; ingestion stage rates (§5) for resource contention.
4. LOGS     only the implicated service, only 12:30–12:50:
            {service="reranker"} |~ `(?i)(error|timeout)`
5. REPORT   evidence per claim + query; symptom (p95 up 3×) vs probable cause
            (reranker timeouts → fallback ordering); next step referred to admin.
```

## 8. Pitfalls checklist (things that produce confidently wrong answers)

| Pitfall | Symptom | Fix |
|---|---|---|
| `rate()` on a gauge | nonsense near-zero values | read gauges raw |
| raw read of a `_total` counter | huge ever-growing number | `rate()`/`increase()` |
| dropping `le` in a histogram sum | wrong percentiles, no error | keep `le` in `sum by` |
| unresolved `$var` / `$__rate_interval` from a copied panel | empty result or parse error | interpolation protocol (INSTRUCTIONS §3.5) |
| missing `or vector(0)` | "no data" reported as unknown when the true answer is 0 | add it for possibly-absent series |
| `increase()` window shorter than scrape interval | zeros | window ≥ 2× scrape interval, prefer ≥ 5m |
| unanchored `{compose_project="nexus-rag"}` log query | flood, token blowout | always add a `service` label + filter |
