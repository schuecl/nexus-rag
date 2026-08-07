# Deploy with Docker Compose

Compose is the single-host path: development, demos, evaluation runs, and
small commercial/internal deployments that fit on one machine. It stands up
the *entire* stack — including its own Postgres, Keycloak, and a throwaway
chat plane — unlike the [Helm chart](deploy-helm.md), which integrates with
existing cluster infrastructure.

## Baseline

```bash
cp .env.example .env      # then edit — see the knobs below
docker compose up --build -d
docker compose ps         # wait for healthy + one-shots exited 0
```

`.env.example` is annotated per variable and is the authoritative knob list;
the highlights:

| Knob | Default | What it changes |
|---|---|---|
| `EMBEDDING_MODEL` | `nomic-embed-text` | Dense-leg embeddings. Changing it mandates re-ingestion **and** re-running the golden eval (the report's config fingerprint will tell on you if you skip it) |
| `GENERATION_MODEL` | `qwen2.5:3b-instruct` | The throwaway dev generation model behind LiteLLM |
| `VECTOR_BACKEND` | `qdrant` | `milvus` runs the same pipeline against Milvus — see below |
| `RERANK_SCORE_FLOOR` | unset | Relevance floor: below it, retrieval returns *nothing* rather than noise. Unset = disabled; the eval's abstention-noise metric (3.6 docs) is the measured argument for calibrating it |
| `CHUNK_TARGET_WORDS` / `CHUNK_OVERLAP_RATIO` | `512` / `0.15` | Chunking geometry, applied at ingest time |
| `VISION_MODEL` / `CLASSIFICATION_MODEL` / `PII_LLM_MODEL` | unset | Optional advisory pipelines (figure captioning, tag suggestion, PII hints) — off unless a model is named |

!!! warning "Change a model → re-evaluate"
    Any embedding/reranker/chunking change invalidates prior quality
    baselines. Run the eval profile (below) after the change; reports carry a
    config fingerprint precisely so cross-config comparisons can't silently
    pass as drift.

## Profiles — opt-in slices

The default `up` starts the core stack. Everything else is a
`--profile`:

```bash title="Evaluation: golden-query gate + latency benchmark"
docker compose --profile eval run --rm eval-retrieval
docker compose --profile eval run --rm benchmark-latency
```

```bash title="Observability: Prometheus, Grafana, Alertmanager, Tempo, Loki…"
docker compose --profile observability up -d
```

```bash title="Milvus backend (instead of Qdrant) + Attu admin UI"
# .env: VECTOR_BACKEND=milvus
docker compose --profile milvus up -d
```

Switching vector backends is **either/or per deployment** and requires
re-ingesting (vectors don't copy between stores). Milvus gets the identical
mandatory-filter semantics — per-classification partitions instead of
per-classification collections.

## GPU, OCR, and the optional pipelines

All optional, all documented in depth in
[Dev environment setup](../dev-setup.md): a GPU host serves Ollama models on
the GPU; OCR (Tesseract) handles scanned PDFs and image uploads out of the
box; captioning/classification-suggestion/PII advisories switch on by naming
a model. Each advisory is deliberately **curator-facing hints, never
decisions**.

## Operating it

```bash
docker compose logs -f ingestion-worker      # watch a document process
docker compose down                          # stop, keep data volumes
docker compose down -v                       # stop and erase all data
```

Health endpoints: every custom service exposes `/health`; the retrieval
service also exposes Prometheus metrics at `/metrics` (stage latency
histograms — what the latency benchmark reads).

!!! note "Commercial vs air-gapped"
    This page's flow builds from source and pulls models from the internet —
    right for connected/dev/commercial single-host use. An **air-gapped
    deployment never does this**: it imports a verified release bundle and
    runs the Helm chart against an internal registry — see
    [Deploy air-gapped](deploy-airgapped.md).

## Sources

- [Dev environment setup](../dev-setup.md) (`docs/dev-setup.md`) — the
  canonical, live-validated reference for every option above, including GPU,
  hardening, the observability stack, and the full troubleshooting history
- [`.env.example`](https://github.com/schuecl/nexus-rag/blob/main/.env.example) —
  the annotated, authoritative knob list
- [Testing & CI quality gates](../testing.md) — the re-evaluation policy
  triggered by model/chunking changes
