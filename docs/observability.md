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

**The dashboards hard-code datasource UIDs.** None has an `__inputs` block;
the quality dashboard has one Prometheus-backed `profile` selector and the
others have an empty `templating.list`. They reference the literal UIDs
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

Issue #384 adds a fifth, batch-oriented source: sanitized Q-to-C-to-A report
metrics published through Pushgateway. It is not a fifth application service and
does not accept corpus content. The publisher allowlists numeric scores/counts,
hashes case and configuration identities, and rejects malformed reports rather
than serializing arbitrary labels.

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

14 dashboards, in `infra/observability/grafana/dashboards/` (Compose) and
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
| RAG quality evaluation | prometheus (via Pushgateway; issue #384) |
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

### Q-to-C-to-A quality dashboard (#384)

[`scripts/publish_rag_quality_metrics.py`](../scripts/publish_rag_quality_metrics.py)
turns a completed issue #74/#383 evaluation report into an allowlisted Prometheus
payload. The separation is deliberate: `evaluate_rag_quality.py` remains the
fail-closed evaluator and its JSON remains the local audit artifact; publishing
is an explicit second action that can be omitted where the approved monitoring
boundary is different.

For Compose, Pushgateway is part of the opt-in `observability` profile and its
write port is bound to host loopback on `127.0.0.1:9092` (container port 9091).
Host port 9092 avoids a collision with Milvus's host-side health/metrics port.

```bash
docker compose --profile observability up -d pushgateway prometheus grafana

# First create a content-free report with #383's evaluator.
python scripts/evaluate_rag_quality.py \
  --agent-id <librechat-agent-id> \
  --output eval-history/qca-current.json

# Publish latest state. Add --baseline for delta/regression panels.
python scripts/publish_rag_quality_metrics.py \
  --report eval-history/qca-current.json \
  --baseline eval-history/qca-baseline.json \
  --profile nightly
```

`--dry-run` prints the exact exposition payload without making a request. The
publisher supports an HTTPS gateway through `--ca-file` and a reverse proxy's
bearer token through `--bearer-token-file`; credentials are never accepted in
the URL. Keep the endpoint inside the accreditation boundary. Pushgateway is a
metrics cache for ephemeral/batch jobs, not an event store; Prometheus's TSDB is
what provides the 30-day trend displayed by the dashboard.

The Helm observability chart deploys its gateway as ClusterIP only. Publish from
an allowed namespace, use the environment's existing approved Pushgateway, or
use a controlled `kubectl port-forward` for administrative validation. The chart
does not create an unauthenticated public write endpoint. With
`networkPolicy.enabled=true`, writer namespaces come from
`networkPolicy.allowedNamespaces`; Prometheus in the observability release is
always allowed to scrape it.

Dashboard panels include latest aggregate gauges, score time series, comparable
baseline deltas, run-validity/regression/error/coverage stats, a hashed case-ID
table, a hashed configuration table, and configuration-fingerprint annotations.
The aggregate gauges are intentionally neutral blue. Judge scores are relative,
so only the publisher's same-configuration baseline decision is rendered as a
red/green regression state.

### Running it unattended (#388)

Everything above is a manual two-step: an admin runs `evaluate_rag_quality.py`,
then `publish_rag_quality_metrics.py`, usually through a `kubectl port-forward`.
That is the right shape for validation and the wrong one for a steady state —
the trend, baseline-delta, and regression panels only advance when someone
remembers to run both scripts.

**Scheduling the publisher alone would not fix that.** Pushgateway holds what it
is given until something overwrites it, and Prometheus scrapes it on the normal
interval, so a single push already yields a continuous series. Re-pushing an
unchanged report on a timer produces a newer push timestamp over the same
measurement, not a new measurement. The step worth scheduling is the *evaluator*;
publishing is its last line.

#### Why the evaluator is not schedulable as it stands

`evaluate_rag_quality.py` is host-side by construction, not by oversight. It
imports its login and generation path from
[`adversarial_injection_probe.py`](../scripts/adversarial_injection_probe.py),
whose docstring already explains why *that* script cannot run as a container on
the Compose network. The same facts block an in-cluster CronJob:

| What | Where | Why it blocks an unattended run |
|---|---|---|
| Endpoints are literals, not configuration | `adversarial_injection_probe.py`'s `KEYCLOAK_URL`, `KEYCLOAK_HTTPS_URL`, `LIBRECHAT_URL`, `INGESTION_API_URL` | Module constants pointing at `localhost` and the `keycloak` `/etc/hosts` alias, with no environment override. `evaluate_rag_quality.py` makes only `ORCHESTRATION_MCP_URL`/`OLLAMA_URL` overridable — contrast `evaluate_retrieval.py`, which reads every endpoint from the environment and is exactly why *it* runs as a one-shot container. |
| Both redirect URIs are `https://localhost:3080` | `infra/librechat/librechat.yaml`'s `DOMAIN_SERVER` | LibreChat's OIDC redirect and the `rag` MCP server's OAuth redirect resolve only from the host running the stack, and the scripted login follows that exact chain. A real deployment's LibreChat has its own URI, which nothing here reads. |
| Login is a Keycloak password grant as a seeded user | `scripts/_keycloak.py`'s `SEED_PASSWORD` | A dev constant, and a human password grant is the wrong credential to put on a timer (below). |
| The LibreChat session JWT is minted directly | same technique as `create_librechat_agent.sh` | Needs LibreChat's JWT signing secret at run time. |
| `--agent-id` refers to a manually created agent | `scripts/create_librechat_agent.sh` | Nothing creates or discovers it automatically. This is the same prerequisite that keeps #383's harness out of CI. |
| No published image carries these scripts | `scripts/Dockerfile`, built from context by Compose | The chart publishes four images and this is not one of them, so the job's image must be built, pushed to the environment's registry, and pinned (NFR-16) first. |

Making those endpoints configurable is the smallest real unblocking step, and it
is deliberately **not** done here — it changes a security-probe script that
cannot be re-validated against a live LibreChat from this repo, and it would
still leave the credential questions below open.

#### Credentials and tokens an unattended run would need

The issue asks what provisioning is required. Concretely:

- **A non-interactive identity.** The evaluator authenticates as `EVAL_PERSONA`
  (default `dave-admin`) — an admin persona chosen so access filtering does not
  truncate golden-set coverage. That is defensible for an operator-run
  measurement and a broad standing credential to hand a timer: it can read every
  classification in the corpus. A scheduled run should use a dedicated
  evaluation account carrying exactly the clearance and releasability the golden
  set needs, provisioned as the environment's approved non-interactive
  credential — not a password grant against a person's account.
- **LibreChat's JWT signing secret**, mounted as a Secret, for the session JWT.
- **The per-user MCP OAuth consent.** The probe automates the browser "Connect"
  step by reusing the Keycloak SSO cookie from its own login; the scheduled
  identity still needs that consent to exist, or to be re-established each run,
  before `rag_search` is callable at all.
- **Token lifetime shorter than the run.** `accessTokenLifespan` is 900 s in
  dev and a CPU-bound judge run outlives it, which is why the evaluator
  re-mints before every case. Any scheduled wrapper must keep that behavior
  rather than authenticating once up front.
- **For the publish step only:** `--bearer-token-file` and `--ca-file` where the
  gateway sits behind an authenticating proxy. Credentials are never accepted in
  the URL.

#### The CronJob shape, once those are met

Publishing goes straight to the ClusterIP Service — no port-forward. The job's
namespace must appear in the observability release's
`networkPolicy.allowedNamespaces`, which is what its Pushgateway policy admits.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: rag-quality-evaluation
  namespace: nexus-rag          # must be in networkPolicy.allowedNamespaces
spec:
  schedule: "0 2 * * *"         # the --profile nightly example above
  concurrencyPolicy: Forbid     # a judge run can outlast its own interval
  startingDeadlineSeconds: 3600
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 0           # a re-run is a new measurement, not a retry
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: evaluate-and-publish
              image: registry.example/nexus-rag-scripts:<pinned-tag>
              command: ["/bin/sh", "-c"]
              args:
                - |
                  set -e
                  python3 evaluate_rag_quality.py \
                    --agent-id "$AGENT_ID" \
                    --no-fail-on-regression \
                    --history-dir /var/lib/qca \
                    --baseline /var/lib/qca/baseline.json \
                    --output /var/lib/qca/current.json
                  python3 publish_rag_quality_metrics.py \
                    --report /var/lib/qca/current.json \
                    --baseline /var/lib/qca/baseline.json \
                    --profile nightly \
                    --pushgateway-url "$RAG_QUALITY_PUSHGATEWAY_URL"
              env:
                - name: RAG_QUALITY_PUSHGATEWAY_URL
                  value: >-
                    http://nexus-rag-obs-observability-pushgateway.observability.svc.cluster.local:9091
                - name: AGENT_ID
                  valueFrom:
                    secretKeyRef:
                      name: rag-quality-eval
                      key: agent-id
              volumeMounts:
                - name: history
                  mountPath: /var/lib/qca
          volumes:
            - name: history
              persistentVolumeClaim:
                claimName: rag-quality-eval-history
```

Two things in there are load-bearing and easy to get wrong:

- **`--no-fail-on-regression` is deliberate.** The evaluator is fail-closed by
  default: a regression against the baseline makes it exit non-zero. Under
  `set -e` that skips the publish step, so the one run whose numbers most need
  to reach the dashboard is the one that never publishes them. The publisher
  computes its own same-configuration regression verdict, and the dashboard
  renders *that*, so let the evaluation report land and read the verdict there.
  Keep the fail-closed default for interactive and gating use.
- **The volume is not optional.** `--history-dir`/`--baseline` have to survive
  between runs. On an `emptyDir` every run is baseline-less, the delta and
  regression panels stay empty, and the result is the reported problem with a
  schedule attached.

#### Status

Documented pattern, not a shipped template. The chart does not include this
CronJob, on purpose: the evaluator cannot run unattended until at least the
endpoint-configuration and credential items above are resolved, and a chart
template that renders cleanly while being unable to work would read as a
supported feature. That is the same reasoning `docs/governance.md` applies to
the retention job it also declines to ship. None of the manifest above has been
run against a cluster — it is the shape the pieces imply, checked against the
chart's actual Service name, port, and NetworkPolicy, not an executed
configuration.

### Periodic object-store integrity re-verification (#432)

NFR-18 shipped event-triggered content-integrity verification (#285): every
time a document's original is fetched for parsing or re-embedding,
ingestion-worker re-hashes it against `documents.content_sha256` and refuses
to proceed on a mismatch. That never runs for a document that's simply
sitting approved in the object store, untouched, for months — the common
case, not an edge case — so NFR-18's own text named a scheduled,
event-independent sweep as a follow-on. `services/ingestion-worker/app/
integrity_sweep.py` (`python -m app.integrity_sweep`) is that sweep: each run
re-hashes a bounded rolling window of documents (`--batch-size`, default
500), ordered oldest-`last_verified_at`-first so a corpus larger than one
run's batch is covered incrementally across successive runs rather than
paying a full-corpus re-hash's I/O cost every time.

A mismatch or an unreadable original is deliberately **not** an automatic
status change -- unlike the event-triggered check, there's no processing in
flight to refuse, and the cause (bit rot, a backup-restore artifact, or real
tampering) needs a human to triage. Each finding becomes a
`document.integrity_check_failed` audit_log entry (`reason`:
`digest_mismatch` or `original_unreadable`, no digest values in `detail` --
same re-identification reasoning `common/purge.py` already applies to a
purge tombstone) and is counted toward the `nexus_rag_integrity_check_
failures_total` Pushgateway metric, alongside a `nexus_rag_integrity_check_
last_run_timestamp_seconds` heartbeat -- same flagged-count/heartbeat shape
as `detect_query_anomalies.py`'s pair above.

Locally: `docker compose run --rm ingestion-worker python -m
app.integrity_sweep` (reuses the ingestion-worker image/role, same pattern
`app/reembed.py`'s CLI already uses -- no new DB grant needed, since
`worker_role` already holds `SELECT, UPDATE` on `documents` and `INSERT` on
`audit_log`). `RAG_INTEGRITY_PUSHGATEWAY_URL` (`docker-compose.yml`) points
it at the `observability` profile's Pushgateway; pass `--no-push` (or leave
that env var unset without the `observability` profile running) to skip
publishing and just see the stdout report.

#### Status: shipped, unlike the Q-to-C-to-A CronJob above

`helm/nexus-rag/templates/ingestion-worker-integrity-sweep-cronjob.yaml`
(`ingestionWorker.integritySweep.enabled`, default `true`, nightly by
default) **is** an actual chart template, not a documented-but-declined
pattern -- the blockers that keep the Q-to-C-to-A CronJob doc-only
(hardcoded `localhost` endpoints, dev-only OIDC redirect URIs, a
password-grant login, a manually-created LibreChat agent-id, no published
image containing `scripts/`) don't apply here: the sweep reuses the
`ingestion-worker` image the chart already publishes, authenticates with the
same Postgres/object-store credentials that image's Deployment already
holds, and needs no browser-facing OIDC round trip at all. `Values.
ingestionWorker.integritySweep.pushgatewayUrl` is left empty by default
(same "cluster-specific, don't guess" posture as
`networkPolicy.ingressControllerSelectors`) -- the sweep still runs and
still writes `audit_log` findings without it, just without the
Prometheus-visible metrics, until an operator points it at their
observability release's Pushgateway Service.

## Alerts

14 rules in four groups, in `infra/observability/prometheus/rules/nexus-rag.yml`
(and the chart's vendored copy):

- **availability** — `NexusRagServiceDown`, `NexusRagWorkerConsumerStopped`,
  `NexusRagDependencyDown`
- **ingestion** — `NexusRagQueuePublishFailure`,
  `NexusRagUnpublishedDocumentStale`, `NexusRagWorkerTransientFailures`,
  `NexusRagWorkerDeliveryExhausted`
- **retrieval** — `NexusRagHighQueryLatency`, `NexusRagRerankerFallbackHigh`,
  `NexusRagQueryDeniedSpike`
- **security** — `NexusRagQueryAnomalyDetected`,
  `NexusRagQueryAnomalyDetectionStale`, `NexusRagTaggingCalibrationStale`
  (issues #426/#527: fed by `scripts/detect_query_anomalies.py` and
  `scripts/calibrate_tagging_advisory.py` via Pushgateway, not scraped from a
  service -- see `docs/testing.md`'s "Reconnaissance-shaped query detection"
  section). To run the equivalent detection inside the deployment's own SIEM
  instead of, or alongside, that batch job, see
  [docs/siem-detection-runbook.md](siem-detection-runbook.md) (issue #436) --
  the same four signals expressed against the RFC 5424 audit export (#73).
  `NexusRagIntegrityCheckFailureDetected`, `NexusRagIntegritySweepStale`
  (issue #432: fed by `app/integrity_sweep.py` via Pushgateway, same
  flagged-count/heartbeat shape -- see "Periodic object-store integrity
  re-verification" above).

Neither offline job depends on an operator remembering a cadence anymore
(issue #527): the dev stack schedules both with
`docker compose --profile scheduling up -d` (hourly detection, weekly
calibration by default -- `RAG_ANOMALY_INTERVAL_SECONDS` /
`RAG_CALIBRATION_INTERVAL_SECONDS` override), and production enables the
chart's default-off CronJobs (`auditReporting` in `values.yaml`, backed by the
scripts image released in lockstep with the four service images). The
staleness alerts assume those default cadences; both fire only after a first
run has published -- a deployment that never enables the jobs sees no series
and no alert, which is why they stay opt-in rather than silently required.

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
  render, trace correlation works, the service map populates, and Pyroscope
  itself comes up and is reachable from Grafana as a datasource. **Not
  independently re-confirmed live since #349 landed the actual profile
  push** — that run predates #349; `setup_profiling()` itself is
  unit-tested (`tests/unit/common/test_profiling.py`), but whether flame
  graphs actually populate end to end for all four services against a real
  Pyroscope instance has not yet been re-validated against a live stack.
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
