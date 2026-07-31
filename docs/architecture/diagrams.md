# Architecture diagrams

Visual reference for the full system (issue #129): every running application,
the ingestion data pipeline, the query/retrieval pipeline, the
classification/tagging model, the document lifecycle, and the production
(Helm) topology. All diagrams are Mermaid, derived from the code
(`docker-compose.yml`, `services/*`, `infra/*`, `helm/nexus-rag`) — when a
diagram and the code disagree, the code wins and the diagram has a bug; please
file it.

Feature references: table extraction (#88), content-type tagging (#89),
image captioning (#92), OCR (#241), and the durable queue hand-off (#164)
are drawn as they exist on their branches/PRs at the time of writing — the
per-feature issue numbers in node labels are the pointer to their exact
status.

`ARCHITECTURE.md` remains the prose source of truth; this page is the visual
companion.

---

## 1. System context — all applications

The stack splits into the **existing chat plane** (LibreChat, LiteLLM,
Ollama, MongoDB — throwaway dev stand-ins for what MPNexus already runs), the
**new RAG plane** this repo adds (four custom Python services plus Postgres,
Qdrant, NATS, and the object store), the **identity plane** (Keycloak), and
an **opt-in observability plane** (`--profile observability`, issue #133).

```mermaid
flowchart LR
    subgraph chat["Chat plane (existing MPNexus stand-ins)"]
        proxy["librechat-proxy<br/>nginx TLS"] --> lc["LibreChat"]
        lc --> mongo[("MongoDB")]
        lc --> litellm["LiteLLM"]
        litellm --> ollama["Ollama<br/>generation + embeddings<br/>+ vision model (#92, opt-in)"]
    end

    subgraph identity["Identity"]
        kc["Keycloak<br/>realm nexus-rag<br/>claims: clearance, releasability,<br/>groups, org, rag_roles"]
    end

    subgraph rag["RAG plane (this repo)"]
        api["ingestion-api<br/>upload + curation UI/API"]
        worker["ingestion-worker<br/>parse / OCR / caption /<br/>chunk / embed"]
        mcp["orchestration-mcp<br/>rag_search MCP tool"]
        rr["reranker-service<br/>cross-encoder"]
        pg[("Postgres<br/>system of record")]
        qd[("Qdrant<br/>chunk vectors + payload")]
        nats[["NATS JetStream<br/>durable job queue"]]
        os[("Object store<br/>original uploads")]
    end

    subgraph obs["Observability plane (opt-in, #133)"]
        prom["Prometheus + Alertmanager"]
        graf["Grafana dashboards"]
        loki["Loki + Alloy (logs)"]
        tempo["Tempo + otel-collector (traces)"]
        bb["blackbox-exporter (probes)"]
    end

    user(("Analyst")) --> proxy
    uploader(("Uploader / Curator"))--> api

    lc -- "MCP over streamable HTTP<br/>+ OAuth bearer" --> mcp
    lc -- "OIDC login" --> kc
    api -- "OIDC login + JWKS verify" --> kc
    mcp -- "JWKS verify (#215)" --> kc

    api --> pg
    api --> os
    api -- "publish UUID only" --> nats
    api -- "curation payload flips" --> qd
    nats -- "durable consumer" --> worker
    worker --> pg
    worker --> os
    worker -- "embeddings + captions + OCR'd text" --> ollama
    worker -- "upsert pending_review" --> qd
    mcp -- "read-only API key" --> qd
    mcp -- "dense-leg embedding" --> ollama
    mcp -- "X-Reranker-Shared-Secret (#216)" --> rr

    prom -.-> rag
    graf -.-> prom
```

What to note:

- Every claim-gated decision goes through one shared library
  (`services/common/common/claims.py`), verified against Keycloak's JWKS, at
  **three enforcement points**: tagging (upload), curation (approval), and
  retrieval (query filter).
- `orchestration-mcp` holds a **read-only** Qdrant API key (NFR-15); only
  `ingestion-api` (curation flips) and the worker (upserts) can write vectors.
- The audit log is append-only from the app role's perspective — the
  `lock-down-db-grants` one-shot leaves every application role with INSERT and
  nothing else -- not even SELECT (NFR-2, #278).
- NATS uses per-service credentials with subject-level ACLs (#212):
  `ingestion-api` may only publish, the worker may only consume.
- One-shot containers (`ollama-model-init`, `seed-sample-data`,
  `ensure-db-roles`, `provision-metrics-view`, `lock-down-db-grants`,
  `eval-retrieval` under `--profile eval`) run to completion and exit; they
  are omitted above for legibility.

---

## 2. Ingestion data pipeline

From file upload to searchable, approved chunks. The 202 is returned only
after the Postgres row, the object-store write, and the queue publish have
all succeeded (NFR-11/NFR-12) — from that point the pipeline is durable.

```mermaid
sequenceDiagram
    autonumber
    actor U as Uploader (rag-ingest)
    participant API as ingestion-api
    participant OS as Object store
    participant PG as Postgres
    participant Q as NATS JetStream
    participant W as ingestion-worker
    participant OL as Ollama
    participant QD as Qdrant

    U->>API: POST /documents (file + tags)
    API->>API: validate type/magic bytes (#211)<br/>validate tags vs verified claims (FR-18)
    API->>OS: store original bytes (NFR-12)
    API->>PG: INSERT document status=queued
    API->>Q: publish document UUID (no content on the queue)
    API-->>U: 202 Accepted
    Q->>W: deliver (durable consumer, ack-wait 300s)
    W->>PG: claim row (lock), status=processing
    W->>OS: fetch original
    W->>W: parse (FR-3): prose + tables (#88)<br/>+ OCR fallback for text-less pages / image uploads (#241)
    opt VISION_MODEL set (#92)
        W->>OL: caption embedded figures (degrade-on-failure)
    end
    W->>W: chunk (FR-4): sliding window --<br/>table/image sections atomic (#90), ocr text windowed (#241)
    W->>OL: dense embeddings (FR-5)
    W->>W: BM25 sparse vectors
    W->>QD: upsert points, payload status=pending_review
    W->>PG: status=pending_review + audit entry
    W->>Q: ack (terminal outcome only)
```

Failure semantics:

```mermaid
flowchart TD
    msg["JetStream delivery"] --> outcome{"processing outcome"}
    outcome -- "success" --> ack["ack -- terminal"]
    outcome -- "permanent failure<br/>(unparseable, no extractable text,<br/>timeout #208)" --> failed["Postgres status=failed<br/>+ FR-8 message, then ack"]
    outcome -- "transient failure<br/>(DB / Qdrant / NATS down)" --> nak["nak with 30s backoff"]
    nak --> redeliver{"under 5 attempts?"}
    redeliver -- "yes" --> msg
    redeliver -- "no" --> exhausted["status=failed<br/>(delivery exhausted)"]
```

What to note:

- Chunks enter Qdrant as `pending_review` and are **invisible to retrieval**
  until a curator approves; approval flips the Qdrant payload *before* the
  Postgres commit, with a best-effort revert if the commit fails (NFR-13).
- Messages carry only the document UUID — no content transits the queue.
- Captioning (#92) and the scanned-page OCR fallback (#241) degrade rather
  than fail: a down vision model or missing tesseract costs captions/OCR
  text, never the document. An *image upload* with no readable text is the
  exception — OCR is that format's only content path, so it fails with an
  actionable message.
- Superseding a document (FR-7): on approval of the new version, the new
  chunks flip to `approved` *before* the old version's chunks are deleted —
  never a window where neither version is retrievable.

---

## 3. Query / retrieval pipeline

From an analyst's chat message to grounded, access-controlled references.

```mermaid
sequenceDiagram
    autonumber
    actor A as Analyst
    participant LC as LibreChat
    participant KC as Keycloak
    participant M as orchestration-mcp
    participant OL as Ollama
    participant QD as Qdrant
    participant RR as reranker-service
    participant PG as Postgres (audit)

    A->>LC: question
    LC->>KC: MCP OAuth (authorization_code, rag-app client)
    LC->>M: rag_search(query, top_k) + Bearer token
    M->>M: verify bearer vs JWKS (#215)<br/>parse claims, require rag-query
    M->>M: build mandatory access filter server-side (FR-26):<br/>status=approved AND clearance AND releasability AND scope
    M->>OL: embed query (dense leg)
    M->>QD: one query, two Prefetch legs (dense + BM25),<br/>filter applied to BOTH, fused via RRF (FR-24)
    M->>RR: cross-encode fused candidates (FR-25)<br/>X-Reranker-Shared-Secret (#216)
    alt reranker unreachable
        M->>M: fall back to fused order, note it in the response
    end
    M->>PG: audit entry: identity, outcome, filter,<br/>query LENGTH only (#125)
    M-->>LC: model-facing reference text (#204):<br/>passages + [filename, classification] citations,<br/>Provenance line on machine-derived text (#241),<br/>content delimited as untrusted (P1)
    LC-->>A: grounded answer with citations
```

What to note:

- The access filter is built **server-side from verified claims**, never
  client input, and is injected into *both* hybrid legs — neither the dense
  nor the BM25 path can bypass it (the core FR-26 invariant).
- Hybrid retrieval is Qdrant-native: two Prefetch legs fused with Reciprocal
  Rank Fusion, then a cross-encoder rerank over the fused pool.
- Privacy posture: the audit log stores query *length*, not query text
  (#125); similarity scores are never returned to callers
  (membership-inference mitigation); "no results passed the access filter"
  is a valid, explicit answer (FR-28).
- Machine-derived passages carry a `Provenance:` line so the model presents
  OCR'd text as "the scanned copy reads…" and figure captions as
  descriptions, not verbatim quotes (#241, pre-wired for #92).
- The same logic is exposed as `POST /debug/rag_search` for curl-based
  testing; it returns the full structured diagnostic object instead of the
  model-facing text.

---

## 4. Data classification and tagging model

Every document — and every chunk derived from it — carries a mandatory tag
set. Every tag is constrained by the **uploader's** verified claims at
ingest, re-checked against the **curator's** claims at approval, and matched
against the **query user's** claims at retrieval, all through the same
`services/common` helpers.

### 4.1 Tag schema and controlled vocabularies

```mermaid
erDiagram
    DOCUMENT ||--o{ CHUNK : "parsed into"
    DOCUMENT {
        uuid id PK
        string classification "ranked ladder, Postgres-configurable"
        string_list releasability "NONE | NOFORN | NATO | FVEY ..."
        string_list access_scope "org / group / user-sub / ALL_AUTHENTICATED"
        string status "lifecycle, section 5"
        string uploader_sub "verified OIDC identity"
    }
    CHUNK {
        uuid point_id PK "uuid5(doc, chunk_index) -- replay-safe (#164)"
        string content_type "text | table | image | ocr (#89)"
        string classification "copied from document"
        string_list releasability "copied"
        string_list access_scope "copied"
        string status "the retrieval enforcement field"
    }
    CLASSIFICATION_LEVEL ||--o{ DOCUMENT : "constrains"
    CLASSIFICATION_LEVEL {
        string value
        int rank "admin-configurable ladder"
        bool active
    }
```

What to note:

- `NONE` is an explicit, first-class releasability value (not NULL) meaning
  "no caveat" — anyone may assign it and everyone can see it, unlike
  `NOFORN`/`NATO`/`FVEY`, which gate on the caller's coalition claims.
- `ALL_AUTHENTICATED` waives only org/group/user scoping; it is deliberately
  **not** named PUBLIC — classification and releasability still apply.
- Classification levels are a **ranked list in Postgres**, not hardcoded —
  admins can extend/re-order the ladder and ingest + retrieval both pick it
  up via `common/classification.py`.
- Chunk payloads carry a copy of the access fields so retrieval filters
  inside Qdrant without a per-query Postgres round trip.

### 4.2 Claims → authorization at the three enforcement points

```mermaid
flowchart TD
    jwt["Verified OIDC claims<br/>clearance / releasability / groups / org / rag_roles"]
    jwt --> t1["1 - Tagging (upload, FR-18)<br/>may not tag above own clearance<br/>may not assign a caveat not held<br/>needs rag-ingest"]
    jwt --> t2["2 - Curation (approval)<br/>needs rag-curate:&lt;org&gt; for the doc's org<br/>clearance must cover the doc<br/>re-checked against the OLD doc on supersession"]
    jwt --> t3["3 - Retrieval (query, FR-26)<br/>mandatory payload filter, both hybrid legs<br/>status=approved AND clearance rank AND<br/>releasability subset AND scope match"]
```

Worked examples with the seeded dev users (`docs/dev-setup.md`):

| User | Claims | Can tag at ingest | Sees at retrieval |
|---|---|---|---|
| `alice-ingest` | clearance CUI, org USAREUR-AF | UNCLASSIFIED or CUI, releasability NONE | approved docs ≤ CUI, NONE, in her org/groups/self scope or ALL_AUTHENTICATED |
| `bob-query` | clearance SECRET, FVEY + NATO | (no rag-ingest role) | approved docs ≤ SECRET, NONE/FVEY/NATO, in scope |
| `carol-curator` | SECRET, rag-curate:USAREUR-AF | (query role only) | bob's visibility + approval authority over USAREUR-AF queue items her clearance covers |
| `dave-admin` | SECRET, both orgs, all roles | anything ≤ SECRET | widest visibility |

Key invariant: a user can never tag a document above their own clearance,
never assign a caveat they don't hold, and never retrieve a chunk whose tags
exceed their claims — the filter is server-built and injected into both
hybrid legs, so there is no client-controlled path around it.

---

## 5. Document status lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued : 202 accepted
    queued --> processing : worker claims (row lock)
    processing --> embedded : vectors upserted (checkpoint)
    embedded --> pending_review : commit + audit
    processing --> failed : permanent parse/embed failure,<br/>timeout, delivery exhausted
    pending_review --> approved : curator approves<br/>(Qdrant flip, then Postgres)
    pending_review --> rejected : curator rejects (with reason)
    approved --> superseded : new version approved (FR-7)
    queued --> purged : spillage remediation (#123)
    processing --> purged
    pending_review --> purged
    approved --> purged
    rejected --> purged
    failed --> purged
    purged --> [*]
```

What to note:

- Retrieval eligibility comes **only** from the Qdrant payload `status` —
  Postgres is the system of record, Qdrant is the enforcement point.
- `embedded` is a checkpoint, not a terminal state: a crash between it and
  `pending_review` is replayed by JetStream redelivery, and the
  deterministic point IDs (#164) make the replayed upsert overwrite rather
  than duplicate.
- `purged` (#123) destroys content in every store — object-store original,
  Qdrant points, content-bearing Postgres fields — leaving a tombstone so
  audit entries still resolve. It requires the dedicated `rag-purge` role.

---

## 6. Production deployment (Helm)

`helm/nexus-rag` deploys **only the RAG plane** (NFR-10): LibreChat,
LiteLLM, Keycloak, generation-serving Ollama, Postgres, and the S3 object
store are assumed to exist and are configured, not installed
(`existingSecret` pattern throughout; air-gapped registry prefix via
`global.imageRegistry`).

```mermaid
flowchart TD
    subgraph cluster["Kubernetes (nexus-rag chart)"]
        ing["Ingress<br/>50m body size"] --> api["ingestion-api<br/>Deployment, 2 replicas"]
        api2["ingestion-worker<br/>Deployment"] 
        mcp["orchestration-mcp<br/>Deployment"]
        rr["reranker-service<br/>Deployment + model-cache PVC 5Gi"]
        emb["embedding-service<br/>dedicated Ollama, GPU-backed,<br/>models PVC 20Gi (NFR-8)"]
        qd["Qdrant StatefulSet<br/>PVC 50Gi"]
        nats["NATS StatefulSet<br/>JetStream, PVC 10Gi,<br/>per-service ACL accounts (#212)"]
        np["default-deny NetworkPolicies (#110)"]
    end
    subgraph external["External (existing MPNexus)"]
        kc["Keycloak"]
        pg[("PostgreSQL")]
        s3[("S3-compatible object store")]
        lc["LibreChat / LiteLLM / gen-Ollama"]
    end
    api --> pg & s3 & nats & qd
    api2 --> pg & s3 & nats & qd & emb
    mcp --> qd & emb & rr
    lc --> mcp
    api & mcp --> kc
```

What to note:

- The dedicated embedding Ollama is a **separate GPU allocation** from the
  cluster's generation-serving instance (NFR-8) — the chart never touches
  the chat plane's model serving.
- Optional Milvus backend (#160): `vectorBackend: milvus` renders a
  single-node Milvus StatefulSet (16Gi) in place of Qdrant — one backend per
  deployment, behind `common/vector_store.py`'s seam.
- Compose mirrors the chart's securityContext (uid 10001, read-only root,
  `cap_drop: ALL`, `no-new-privileges`, sized tmpfs) and this is enforced
  mechanically by `scripts/check_compose_hardening.py` (#111), so dev runs
  exercise the production posture.
- The worker image bakes in tesseract + eng traineddata (#241) — OCR makes
  no network call and downloads nothing at runtime (NFR-1).

---

## Known gaps (kept honest, per the repo's labeling convention)

- The Helm chart passes `helm lint` in CI (`security.yml`) but has **not
  been deployed against a real cluster** — "implemented", not "validated".
- LibreChat-driven OBO token exchange (RFC 8693) was investigated and does
  not fit two-clients-one-realm Keycloak; the shipped design uses per-user
  `authorization_code` MCP OAuth instead (see `docs/dev-setup.md`).
- `<untrusted_document_content>` markers and the Provenance line are
  prompt-injection *mitigations*, not guarantees (REQUIREMENTS.md §11).
- Encryption at rest relies on an encrypted StorageClass; PyKMIP integration
  was deferred.
- Captioning (#92) and OCR (#241) reference their issues in the diagrams;
  check those issues/PRs for merge status when reading this page at a
  distance from its last update.
