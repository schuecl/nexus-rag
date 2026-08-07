# Configuration by use case

Every knob lives in `.env` (Compose) or `values.yaml` (Helm); this page
organizes the ones that matter by the machine and workload you have. Defaults
are deliberately conservative: **everything works on CPU with no drivers**,
optional AI pipelines are off until you name a model, and a missing optional
dependency degrades a feature — never a document.

## CPU baseline (the default)

Nothing to configure. `docker compose up` runs the full pipeline on CPU:

| Component | Model (pinned) | Notes |
|---|---|---|
| Dense embeddings | `nomic-embed-text` (Ollama) | ~275 MB |
| Sparse leg | BM25 model via fastembed | ~10 MB, pinned to an exact revision |
| Reranker | `cross-encoder/ms-marco-MiniLM-L6-v2` | pinned to an exact revision |
| Dev generation | `qwen2.5:3b-instruct` | chat-plane sandbox only |

All model pulls happen on first boot only and are revision-pinned — an
air-gapped mirror should mirror exactly those revisions.

## GPU hosts

Two components can use an NVIDIA GPU; both are opt-in:

=== "Reranker"

    The torch wheel is baked at **build** time from `TORCH_INDEX_URL`:

    ```bash title=".env"
    TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124   # match your CUDA
    ```

    …then uncomment the reranker's `deploy.resources` GPU reservation in
    `docker-compose.yml` and rebuild. *Honest label:* the CUDA build path is
    implemented but not exercised by CI (no GPU runners) — validate on your
    hardware.

=== "Ollama (embeddings + generation)"

    Uses the GPU automatically once its `deploy.resources` reservation is
    uncommented — no other change.

**Host prerequisites:** NVIDIA driver, `nvidia-container-toolkit`, Docker
configured with the `nvidia` runtime. **Air-gapped:** mirror your chosen
torch index internally and point `TORCH_INDEX_URL` at it. **On a local
Kubernetes sandbox:** minikube can pass the GPU through — the full sequence
is in the [Kubernetes quickstart](quickstart-helm.md).

## Optional AI pipelines

Each activates by naming a model; empty means byte-identical-to-off. All
three share the same failure posture: **degrade, never fail the document** —
gaps surface as Prometheus counters, not failed ingestions.

### Figure captioning (vision model)

Extracts embedded images from PDF/DOCX/PPTX and stores each caption as its
own retrievable chunk (`content_type: "image"`).

```bash title=".env"
VISION_MODEL=moondream          # ~1.7 GB, fine on CPU
# VISION_MODEL=granite3.2-vision  # ~2.4 GB, stronger on charts
```

Bounded by `MAX_IMAGES_PER_DOCUMENT` (20), a per-pass timeout
(`CAPTIONING_TIMEOUT_SECONDS`, 90 s), and minimum-size filters that skip
glyphs and logos.

### Classification suggestion (text model)

Zero-shot second opinion for curators: the model suggests a Classification
(matched strictly against the admin-configured list — never invented),
guesses `doc_type`, and only *surfaces* when it disagrees with the assigned
tags. Advisory by design — a hint in the curation UI, never a decision.

```bash title=".env"
CLASSIFICATION_MODEL=qwen2.5:3b-instruct   # reusing the dev generation model works
```

### OCR (always on)

Not optional, because it's parsing: Tesseract is baked into the worker image
(no runtime downloads). Image uploads become documents via OCR; scanned PDF
pages get a per-page fallback that only fires when a page yields no text.
OCR'd chunks carry `content_type: "ocr"` — visible provenance for curators,
and the retrieval response hedges accordingly ("the scanned copy reads…").

```bash title=".env (defaults shown)"
OCR_LANG=eng                       # add the matching Debian package for others
MAX_OCR_IMAGES_PER_DOCUMENT=50
```

## Retrieval tuning

| Knob | Default | Why you'd touch it |
|---|---|---|
| `RERANK_SCORE_FLOOR` | unset | The abstention knob: below the floor, retrieval returns *nothing* instead of noise. The live baseline measured ~4 irrelevant docs on off-topic queries with it unset — calibrate it |
| `CONTENT_TYPE_BOOSTS` | unset | JSON map of per-content-type score multipliers, e.g. `{"table": 1.15}` |
| `CHUNK_TARGET_WORDS` / `CHUNK_OVERLAP_RATIO` | 512 / 0.15 | Chunking geometry (applies at ingest — re-ingest to change existing docs) |
| `VECTOR_BACKEND` | `qdrant` | `milvus` runs the identical pipeline and filter semantics against Milvus (`--profile milvus`). One backend per deployment; switching = re-ingest |

!!! warning "Any model or chunking change ⇒ re-evaluate"
    Baselines are config-fingerprinted. After changing the embedding model,
    reranker, floor, or chunking, re-run the
    [evaluation harness](../evaluation-results.md) — cross-config baseline
    comparisons are refused by design, so you'll produce a new baseline
    rather than a fake regression (or fake pass).

## Observability (optional profile)

```bash
docker compose --profile observability up -d
```

The profile brings up Prometheus, Grafana, Loki, Tempo, Alertmanager and
Pyroscope — but tracing/JSON-logs/profiling are **also** opt-in in `.env`
(put them there, not inline, or a container restart silently drops them):

```bash title=".env"
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
LOG_FORMAT=json
PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040
```

Then: Grafana <http://localhost:3000> (dashboards and all four datasources
auto-provisioned; log lines carry `trace_id` for one-click log↔trace jumps),
Prometheus <http://127.0.0.1:9090>. On Kubernetes the chart deploys no
monitoring — set `observability.serviceMonitor.enabled=true` for an existing
Prometheus Operator, or use the separate `helm/observability` chart for
clusters that have nothing.

## Security posture you get for free

- Every published Compose port binds `127.0.0.1` only.
- Every service runs non-root with `no-new-privileges`; the four custom
  services add read-only rootfs and `cap_drop: ALL`, mirroring the Helm
  chart's securityContext exactly — CI enforces the two stay in lockstep.
- All images and models are version/revision-pinned; a floating tag fails CI.

## Sources

Authored for this site; the canonical, exhaustive references (including
every default, bound, failure mode, and the live-validation history) are:

- [Dev environment setup](../dev-setup.md) — the full engineering reference
  behind this page
- [`.env.example`](https://github.com/schuecl/nexus-rag/blob/main/.env.example)
  — every variable, annotated
- [Observability](../observability.md) — the monitoring reference in depth
