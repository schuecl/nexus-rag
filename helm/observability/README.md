# observability Helm chart (issue #257)

The observability backends for a Nexus RAG deployment, for the specific topology
where **Grafana already exists outside the cluster** on the air-gapped network.

This chart deploys **no Grafana**. It deploys the four stores that Grafana reads,
plus a ClusterIP-only Pushgateway for sanitized batch-evaluation metrics,
publishes the stores on LoadBalancer addresses, generates a datasource
provisioning file with those addresses filled in, and vendors the 14 dashboards
as importable files.

## When NOT to use this

If the cluster already has a monitoring stack inside the accreditation boundary
— which a DoD cluster usually does — **do not install this**. Use the nexus-rag
chart's ServiceMonitor path instead, which hands your existing Prometheus the
scrape targets and deploys nothing:

```bash
helm upgrade nexus-rag ./helm/nexus-rag --set observability.serviceMonitor.enabled=true
```

That remains the preferred arrangement, and it is why `helm/nexus-rag` ships no
monitoring stack of its own (NFR-10). This chart is the fallback for a cluster
that has none, and it is installed separately so the default `helm install
nexus-rag` posture is unchanged.

## What it deploys

| Component | Image | Service | Why that Service type |
|---|---|---|---|
| Prometheus | `prom/prometheus:v3.11.3` | LoadBalancer :9090 | Grafana datasource |
| Loki | `grafana/loki:3.7.2` | LoadBalancer :3100 | Grafana datasource |
| Tempo | `grafana/tempo:2.10.5` | LoadBalancer :3200 | Grafana datasource |
| Alertmanager | `prom/alertmanager:v0.32.1` | LoadBalancer :9093 | Grafana datasource |
| Pushgateway | `prom/pushgateway:v1.11.3` | ClusterIP :9091 | sanitized Q-to-C-to-A batch metrics (#384) |
| Tempo OTLP | (same pod) | ClusterIP :4317/:4318 | trace *ingest* is a write path |
| OTLP collector | `otel/opentelemetry-collector-contrib:0.153.0` | ClusterIP :4317/:4318 | in-cluster senders only |
| Alloy | `grafana/alloy:v1.16.1` | none (DaemonSet) | push-only to Loki |
| postgres-exporter | `quay.io/prometheuscommunity/postgres-exporter:v0.19.1` | ClusterIP :9187 | scraped in-cluster |
| nats-exporter | `natsio/prometheus-nats-exporter:0.20.1` | ClusterIP :7777 | scraped in-cluster |
| blackbox-exporter | `prom/blackbox-exporter:v0.28.0` | ClusterIP :9115 | scraped in-cluster |

Only the four things Grafana actually connects to get a LoadBalancer.
Pushgateway is a write endpoint and stays ClusterIP-only; an exporter or write
receiver on a LoadBalancer would be attack surface without a direct consumer.

Every tag matches `docker-compose.yml` exactly, so the Compose stack and the
cluster run the same builds (NFR-16). Mirror all ten into the air-gapped
registry and set `global.imageRegistry` — nothing here is fetched at runtime
(NFR-1).

## Install

```bash
cp helm/observability/examples/airgapped-values.yaml my-values.yaml
# replace every CHANGEME
helm install nexus-rag-obs ./helm/observability \
  --namespace observability --create-namespace \
  --values my-values.yaml
```

Then follow the four steps the post-install NOTES print.

## Fail-closed defaults

Two things make the template **fail rather than render**, both deliberate:

1. **`externalAccess.loadBalancerSourceRanges` is empty.** Prometheus, Loki,
   Tempo, and Alertmanager have no authentication of their own — Loki's config
   literally sets `auth_enabled: false`, carried over from Compose where
   everything is bound to loopback. On a LoadBalancer, an empty source-range
   list publishes read access to this deployment's operational telemetry:
   aggregate query volume, denial counts, per-document ingestion signal. There
   is no safe default, so the chart refuses. Set the CIDRs the external Grafana
   connects from, or `externalAccess.allowUnrestricted=true` if access is
   restricted by an external firewall or a private LB class.
2. **`exporters.postgres.enabled` with no `existingSecret`.** The chart will not
   accept a database URI as a plaintext value; supply a Secret holding
   `DATA_SOURCE_NAME` for a read-only monitoring role (NFR-3 — not the
   application's own credentials).

`networkPolicy.enabled` without `grafanaCIDRs` fails for the same reason.

## Dashboards

Vendored under `dashboards/` — 14 JSON files plus `system-flow.svg`. They are
files in the chart, not objects in the cluster; import them into the external
Grafana from a checkout.

`scripts/check_observability_assets_sync.py` (CI's `pin-check` job) keeps them
byte-identical to `infra/observability/grafana/dashboards/`, so the Compose
stack and the air-gapped Grafana can never end up with different copies.

### The datasource UIDs are not free-form

All dashboards have no `__inputs` block and reference **literal** datasource
UIDs. The RAG quality dashboard has one Prometheus-backed `profile` selector;
the other dashboards keep an empty `templating.list`.

| UID | Type | Used by |
|---|---|---|
| `prometheus` | prometheus | operational and quality dashboards |
| `loki` | loki | 51 references |
| `tempo` | tempo | log→trace derived field, service map |
| `alertmanager` | alertmanager | alert state |
| `documents-pg` | postgres | Documents dashboard (16 references) |

Rename any of these in Grafana and every panel bound to it imports broken —
with no error at import time and nothing at panel time beyond "No data". The
generated datasource file uses exactly these UIDs; leave them alone.

### Publishing Q-to-C-to-A evaluation metrics

The chart's Pushgateway is ClusterIP-only and persistent by default. It accepts
only what a caller sends, so use
`scripts/publish_rag_quality_metrics.py` rather than posting report JSON or
hand-built labels. That publisher emits numeric scores/counts and one-way hashes
only; it never emits query, answer, context, source, model, user, or error text.

Publish from an allowed namespace, point the script at an existing approved
gateway, or use a controlled port-forward for administrative validation:

```bash
kubectl port-forward -n observability \
  svc/nexus-rag-obs-observability-pushgateway 9092:9091
python scripts/publish_rag_quality_metrics.py \
  --report eval-history/qca-current.json \
  --baseline eval-history/qca-baseline.json \
  --profile nightly \
  --pushgateway-url http://127.0.0.1:9092
```

When NetworkPolicy is enabled, in-cluster writers must be in
`networkPolicy.allowedNamespaces`. Do not change the Service to LoadBalancer
without adding the environment's approved authentication and network controls.

For running this on a schedule instead of by hand — the CronJob shape, the
credentials the evaluator would need, and why the chart does not ship that
template yet — see "Running it unattended" in
[`docs/observability.md`](../../docs/observability.md) (#388).

### Three dashboards need pre-staged plugins

`nexus-rag-system-flow.json` requires `andrewbmchugh-flow-panel`,
`grafana-graphviz-panel`, and `jdbranham-diagram-panel`. The Compose stack
installs them with `GF_INSTALL_PLUGINS`, which downloads them at container
start — an NFR-1 violation on the air-gapped side, and impossible there anyway.

**Pre-stage those three plugins in the external Grafana before importing.**
Without them the dashboard imports successfully and renders blank panels, which
looks like a data problem and is not.

## Getting the datasources into the external Grafana

The chart renders a provisioning file with this install's addresses:

```bash
kubectl get configmap -n observability nexus-rag-obs-observability-grafana-datasources \
  -o jsonpath='{.data.datasources\.yaml}' > datasources.yaml
```

Copy it to Grafana's `provisioning/datasources/` directory and restart Grafana.
Nothing in the cluster consumes that ConfigMap — it is a delivery mechanism for
a file that belongs on another machine.

If you did not pre-assign `loadBalancerIP` values, the file contains
`REPLACE_WITH_*_LB_ADDRESS` placeholders; substitute the real addresses from
`kubectl get svc`. Placeholders rather than guesses, on purpose — a
plausible-looking wrong address is harder to notice than an obvious one.

## The Documents dashboard needs more than this chart

`documents-pg` is Grafana → Postgres **directly**, not through anything deployed
here. It needs all three of:

1. a network path from the external Grafana to the database,
2. `infra/postgres/document-metrics-view.sql` already applied,
3. `grafana_ro` granted `SELECT` on `document_metrics`.

None of those are things this chart can do. Without them the whole Documents
dashboard reads "No data" while every other dashboard works.

## Tracing is off until you turn it on

Deploying the collector does not enable tracing. The app services no-op when
`OTEL_EXPORTER_OTLP_ENDPOINT` is unset (`services/common/common/tracing.py`), so
point the nexus-rag release at the collector:

```bash
helm upgrade nexus-rag ./helm/nexus-rag \
  --set observability.tracing.otlpEndpoint=http://nexus-rag-obs-observability-otel-collector.observability.svc:4318 \
  --set observability.logFormat=json
```

`logFormat=json` matters as much as the endpoint: Loki's `trace_id` derived field
matches a JSON log field, so with the default text format the log→trace links
never appear.

## What this chart does NOT do

- **No Grafana.** No Deployment, no admin secret, no dashboard-provisioning
  sidecar, no Grafana API calls at install time.
- **No HA.** Prometheus, Loki, Tempo, and Alertmanager are single-replica
  StatefulSets. Two Prometheis with two TSDBs and no dedup would give the
  external Grafana two disagreeing datasources; that is a worse failure than one
  that is briefly down during an upgrade.
- **No long-term storage.** Filesystem backends and PVCs, matching Compose.
  Object-store backends (the reason you would run Loki/Tempo at scale) are a
  larger change than a port.
- **No alert routing.** The default Alertmanager receiver is Compose's no-op
  `local-ui`: alerts are visible in Alertmanager and Grafana but page nobody.
  Replace `alertmanager.config` with the environment's approved integration.
  A plausible-looking email receiver would be worse — configured-looking and
  delivering nowhere.
- **It does not create the Postgres monitoring role or the metrics view.**
- **No public Pushgateway.** The batch-metrics write endpoint is ClusterIP-only.

## Confidence

Per CLAUDE.md's labelling, this chart is **implemented, with every rendered
config validated by its own upstream binary** — at the pinned image versions,
in a container, against the output of `helm template`:

| Config | Validator |
|---|---|
| `prometheus.yml` + rules | `promtool check config` (16 scrape jobs, 10 rules) |
| `alertmanager.yml` | `amtool check-config` |
| `loki.yml` | `loki -verify-config` |
| `tempo.yml` | `tempo -config.verify` |
| otel collector config | `otelcol validate` |
| `config.alloy` | `alloy validate` |

That is stronger than "parses as YAML" and it earned its keep: `alloy validate`
rejected two real bugs in the Kubernetes rewrite — a repeated `namespaces` block,
and a `labels` attribute that exists on `loki.source.docker` but not on
`loki.source.file`. Both would have deployed cleanly and shipped no logs.

It has **not been validated against a live environment**: no cluster with a real
LoadBalancer provider, no external Grafana import, no pod-log line observed
arriving in Loki, no PVC bound. The Compose profile remains the only
observability path run end to end.
