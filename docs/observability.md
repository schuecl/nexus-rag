# Observability

Three ways to observe a Nexus RAG deployment, in order of preference. They are
alternatives, not layers — pick the one that matches the environment.

| | Where it runs | Grafana | Use when |
|---|---|---|---|
| **ServiceMonitor** | your existing stack | yours | the cluster already has monitoring inside the accreditation boundary |
| **`helm/observability`** (#257) | this cluster | external, air-gapped | the cluster has no monitoring stack, but Grafana exists elsewhere |
| **Compose profile** (#133) | a laptop | deployed by the profile | local development and the e2e job |

## 1. ServiceMonitor — preferred on Kubernetes

`helm/nexus-rag` deploys no monitoring stack on purpose (NFR-10). A DoD cluster
usually already runs one, and shipping a second would duplicate it and put the
corpus's operational telemetry somewhere the platform team does not control.
What the chart owes that stack is a way to find the `/metrics` endpoints:

```bash
helm upgrade nexus-rag ./helm/nexus-rag --set observability.serviceMonitor.enabled=true
```

Off by default because rendering a `ServiceMonitor` into a cluster with no
Prometheus Operator fails on an unknown API kind. Note what it does *not* do: it
makes the endpoints scrapeable, not reachable. The chart's default-deny
NetworkPolicies (#110) block the scrape until the monitoring namespace is
allowed through.

Covers `ingestion-api`, `orchestration-mcp`, and `reranker-service`.
`ingestion-worker` is **not** covered — it is queue-driven, nothing calls it, so
`helm/nexus-rag` gives it no Service for a ServiceMonitor to select even though
it serves `/metrics` on :8004. Its nine metrics are visible through the
`helm/observability` chart's pod discovery, or not at all.

## 2. `helm/observability` — external, air-gapped Grafana

For the topology where the cluster has no monitoring stack and Grafana lives
outside it on the air-gapped network. The chart deploys the four stores Grafana
reads on LoadBalancer addresses, vendors the dashboards as importable files, and
deploys **no Grafana**.

Full install walkthrough, the fail-closed defaults, the pinned-UID constraint,
and the pre-staged-plugin prerequisite: [`helm/observability/README.md`](../helm/observability/README.md).

The short version:

```bash
cp helm/observability/examples/airgapped-values.yaml my-values.yaml   # replace every CHANGEME
helm install nexus-rag-obs ./helm/observability -n observability --create-namespace -f my-values.yaml
kubectl get svc -n observability -l app.kubernetes.io/instance=nexus-rag-obs   # the addresses
kubectl get cm -n observability nexus-rag-obs-observability-grafana-datasources \
  -o jsonpath='{.data.datasources\.yaml}' > datasources.yaml                   # → external Grafana
# then import helm/observability/dashboards/*.json in Grafana's UI
```

### Three things that bite

**The dashboards hard-code datasource UIDs.** All 13 have an empty
`templating.list` and no `__inputs` block; they reference the literal UIDs
`prometheus`, `loki`, `tempo`, `alertmanager`, `documents-pg`. Rename one in
Grafana and every panel bound to it imports broken — no error at import, nothing
at panel time beyond "No data". The generated provisioning file uses exactly
those UIDs.

**Three dashboards need plugins that cannot be downloaded.**
`nexus-rag-system-flow.json` needs `andrewbmchugh-flow-panel`,
`grafana-graphviz-panel`, and `jdbranham-diagram-panel`. Compose installs them
via `GF_INSTALL_PLUGINS`, which is an internet fetch — impossible air-gapped and
an NFR-1 violation regardless. Pre-stage them in the external Grafana, or those
panels render blank in a dashboard that imported "successfully".

**Four unauthenticated services on public addresses.** Prometheus, Loki, Tempo,
and Alertmanager carry no authentication (Loki's config sets `auth_enabled:
false`, inherited from Compose where everything is loopback-bound). The chart
refuses to render a LoadBalancer with no
`externalAccess.loadBalancerSourceRanges` for that reason. Aggregate query
volume and denial counts are operational signal about the corpus even though the
metrics themselves carry no user, query, or document identifiers (#127 keeps
label cardinality content-free deliberately).

## 3. Compose profile — local development

```bash
docker compose --profile observability up -d
```

Grafana at <http://localhost:3000> (`admin` / `nexus-rag-admin`), dashboards
pre-provisioned. Every other port is loopback-only. See
[dev-setup.md](dev-setup.md#observability-stack-optional-133) for the socket-proxy
rationale, the Postgres monitoring role, and the trace-correlation walkthrough.

The profile alone does not enable tracing, JSON logs, or profiling — set
`OTEL_EXPORTER_OTLP_ENDPOINT`, `LOG_FORMAT=json`, and `PYROSCOPE_SERVER_ADDRESS`
too, or Tempo/Pyroscope stay empty and the correlation links never appear.

Pyroscope (continuous CPU profiling, #349) is also in the profile, at
<http://127.0.0.1:4040> and wired as a Grafana datasource with
trace-to-profiles correlation on the Tempo datasource. All four services push
100Hz CPU samples once `PYROSCOPE_SERVER_ADDRESS` is set — continuous, not
triggered, matching Prometheus/Tempo's always-on posture in this stack. Memory
profiling stays off. Not (yet) part of `helm/observability`: that's a
separate, larger surface (StatefulSet/Service topology, NetworkPolicy,
dashboard-sync script) worth its own change now that there's real data to
validate a deployment against, rather than folding it into this one.

## What is actually instrumented

All four services expose `/metrics`, all `nexus_rag_*`, all with deliberately
content-free labels — stage and outcome names, never a user, query, or document
id (#127).

| Service | Port | Notable metrics |
|---|---|---|
| ingestion-api | 8001 | submissions, upload bytes, queue publish + reconciliation lag, curation decisions |
| orchestration-mcp | 8002 | per-stage query latency, query outcomes, reranker fallback rate, result counts |
| reranker-service | 8003 | request counts, model predict latency, batch sizes, model-loaded gauge |
| ingestion-worker | 8004 | job outcomes + duration, per-stage duration, chunks produced, delivery attempts, consumer-running gauge, last-success timestamp |

The reranker fallback rate matters more than it looks: FR-25 degrades to fused
order rather than failing, so a ranking-quality drop is otherwise invisible.

Tracing is opt-in and off by default —
`services/common/common/tracing.py` returns early when
`OTEL_EXPORTER_OTLP_ENDPOINT` is unset, so a deployment with no collector pays
nothing. Default head-sampling ratio is 0.05.

Continuous CPU profiling is opt-in and off by default, same posture as
tracing — `services/common/common/profiling.py`'s `setup_profiling()`
(and reranker-service's inline equivalent, which doesn't depend on
`services/common`) is a no-op when `PYROSCOPE_SERVER_ADDRESS` is unset. When
set, all four services push 100Hz CPU-only samples continuously for the life
of the process (#349) — memory profiling is off. `application_name` is
deliberately the same string tracing uses for `service.name`
(`nexus-rag-<service>`), which Pyroscope stores as the `service_name` label —
that agreement is what lets Grafana jump from a trace span straight to the
flame graph for the same service.

## Dashboards

13 dashboards, in `infra/observability/grafana/dashboards/` (Compose) and
`helm/observability/dashboards/` (chart). The two copies are kept byte-identical
by `scripts/check_observability_assets_sync.py`, which runs in CI's `pin-check`
job — the same arrangement #212 uses for `nats.conf`, and for the same reason:
a duplicated source of truth is only safe if something enforces it.

| Dashboard | Reads |
|---|---|
| Nexus RAG Operations | prometheus + loki |
| System flow | prometheus (needs the three flow plugins) |
| Ingestion pipeline | prometheus |
| Retrieval and reranking | prometheus |
| Messaging (NATS JetStream) | prometheus (via nats-exporter `-jsz=all`) |
| Datastores (Postgres) | prometheus (via postgres-exporter) |
| Vector store (Qdrant) | prometheus |
| Milvus | prometheus (#160, only on a Milvus deployment) |
| Identity (Keycloak) | prometheus |
| Chat plane (LibreChat, LiteLLM, Ollama) | prometheus + loki |
| Logs and traces | prometheus + loki |
| Component logs | loki |
| Documents | **postgres directly** — needs `document_metrics` + `grafana_ro` |

The Documents dashboard is the odd one out: Grafana queries Postgres directly
rather than going through Prometheus, so it needs
`infra/postgres/document-metrics-view.sql` applied and `grafana_ro` granted
`SELECT` on the view. Without those it reads "No data" while everything else
works.

## Alerts

10 rules in three groups, in `infra/observability/prometheus/rules/nexus-rag.yml`
(and the chart's vendored copy):

- **availability** — `NexusRagServiceDown`, `NexusRagWorkerConsumerStopped`,
  `NexusRagDependencyDown`
- **ingestion** — `NexusRagQueuePublishFailure`,
  `NexusRagUnpublishedDocumentStale`, `NexusRagWorkerTransientFailures`,
  `NexusRagWorkerDeliveryExhausted`
- **retrieval** — `NexusRagHighQueryLatency`, `NexusRagRerankerFallbackHigh`,
  `NexusRagQueryDeniedSpike`

`NexusRagHighQueryLatency`'s 5 s p95 threshold is a **provisional** number:
NFR-4's latency budget is still an open question in REQUIREMENTS.md, so this is
a stand-in, not an agreed target.

The default Alertmanager receiver is a no-op (`local-ui`) in both Compose and the
chart. Alerts are visible in Alertmanager and Grafana and page nobody. Replace it
with the environment's approved integration — a plausible-looking email receiver
would be worse, since it would look configured while delivering nowhere.

## Confidence

Per CLAUDE.md's labelling:

- **Validated against a live environment**: the Compose profile. Dashboards
  render, trace correlation works, the service map populates, Pyroscope
  reports ready and is reachable from Grafana (no profiles to show yet — #349).
- **Implemented, configs validated against their upstream binaries**:
  `helm/observability`. `helm lint`/`helm template` render every component, the
  dashboards' datasource UIDs are asserted to match the generated provisioning
  file, and each rendered config is checked by the tool that consumes it at the
  pinned version — `promtool check config`, `amtool check-config`,
  `loki -verify-config`, `tempo -config.verify`, `otelcol validate`,
  `alloy validate`. Not run on a cluster: no real LoadBalancer provider, no
  external-Grafana import, no pod-log line observed arriving in Loki.
- **Implemented**: the ServiceMonitor path. It renders; it has not been run
  against a real Prometheus Operator.
