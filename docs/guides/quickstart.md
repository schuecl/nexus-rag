# Quickstart

Stand up the full stack on one workstation, watch seven sample documents flow
through ingest → curate → approve, and run your first claims-filtered query —
in about fifteen minutes, most of it download time.

!!! info "What you'll have at the end"
    A complete local deployment: Keycloak with seeded users, the four RAG
    services, Postgres, Qdrant, NATS, Ollama with the embedding model, a
    seeded and *curated* 7-document corpus — and a working query that only
    returns what your test user is cleared to see.

## 1. Prerequisites

- Docker with the Compose plugin (the stack is ~15 containers)
- ~10 GB free disk and an internet connection **for the first boot only**
  (model pulls: `nomic-embed-text`, the cross-encoder reranker, the BM25
  sparse model — all pinned to exact revisions)
- Linux or macOS; on a hardened host check your `umask` first (see
  [Dev environment setup](../dev-setup.md) if config bind-mounts fail with
  permission errors)

## 2. Start the stack (Docker Compose)

The self-contained sandbox: everything below — seeded users, sample corpus,
throwaway Keycloak and chat plane — comes up on one machine.

```bash
git clone https://github.com/schuecl/nexus-rag.git
cd nexus-rag
cp .env.example .env
docker compose up --build
```

!!! info "Starting on Kubernetes instead? Same sandbox, real Helm chart"
    The [Kubernetes quickstart](quickstart-helm.md) reaches this exact same
    place — seeded personas, curated sample corpus, first query — on a local
    kind/minikube cluster: throwaway dev Postgres and Keycloak (with the
    same realm import Compose uses), the chart's Secrets filled with dev
    values, and the same seed script run as a one-off pod. Steps 3 onward on
    this page then apply unchanged.

    That sandbox is deliberately **not** the production path — production is
    [Helm with your real infrastructure](deploy-helm.md), promoted
    [across the air gap](deploy-airgapped.md) via the verified bundle.

First Compose boot does real work before it's ready: Keycloak imports the realm,
`ollama-model-init` pulls models, and — once every service reports healthy —
`seed-sample-data` submits and curates **seven sample documents through the
real API**, exactly as a human uploader and curator would.

!!! tip "You're ready when…"
    the log shows `seed-sample-data` exiting 0. From then on the corpus is
    queryable. Subsequent boots skip the downloads and the re-seeding.

## 3. Meet the seeded users

Every dev account uses password `devpass123` — never reuse these anywhere real.

| User | Role | Clearance / Releasability | Use them to… |
|---|---|---|---|
| `alice-ingest` | upload | CUI / FVEY | submit documents |
| `carol-curator` | curate | SECRET / FVEY+NATO | approve or reject |
| `bob-query` | query | SECRET / FVEY+NATO | search the corpus |
| `dave-admin` | everything | SECRET / all | admin + broadest reads |

The interesting part: these users see **different corpora**. The seeded
`incident-response-plan.md` is SECRET with access scope `Signal-Corps` — dave
retrieves it, bob (right clearance, wrong group) never does. That asymmetry
is the product working, not a bug.

## 4. Get a token

The browser UI logs in through Keycloak directly. For curl, use the dev-only
password grant:

```bash
TOKEN=$(curl -s http://localhost:8080/realms/nexus-rag/protocol/openid-connect/token \
  -d grant_type=password \
  -d client_id=rag-app \
  -d client_secret=dev-rag-app-secret \
  -d username=bob-query \
  -d password=devpass123 \
  | jq -r .access_token)
```

Swap `username` for any seeded account. Tokens live 15 minutes.

## 5. Run your first query

```bash
curl -s -X POST http://localhost:8002/debug/rag_search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "how often should passwords be rotated", "top_k": 5}' | jq
```

You should see `results` containing chunks from `password-policy.md`, each
carrying its source metadata (`classification`, `releasability`,
`access_scope`, `filename`), plus `hybrid_retrieval` and `reranking` fields
describing what the pipeline actually did.

!!! note "Why the query goes in the body"
    A question asked of a classified corpus is itself sensitive. The body
    form keeps it out of every proxy and ingress access log in the path; the
    audit log deliberately stores no query text either.

Now prove the access filter with the same query as different users: request a
token for `dave-admin` and ask about `network intrusion notification
procedure` — the SECRET incident-response plan appears at rank 1. Repeat as
`bob-query`: it never appears, and nothing in the response hints it exists.

## 6. Push a document through by hand

The seed script already did this seven times, but doing it once yourself
makes the pipeline concrete:

```bash title="1 — submit as alice-ingest (expect 202, status: queued)"
curl -s http://localhost:8001/documents \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -F file=@mydoc.pdf \
  -F classification=CUI \
  -F 'releasability=["FVEY"]' \
  -F 'access_scope=["USAREUR-AF"]' \
  -F source_originator="Test Org" -F doc_type="SOP"
```

Parsing, chunking and embedding run in the background off a durable queue —
poll `GET /documents/<id>` until `status` reaches `pending_review`. Then
approve it as `carol-curator` at <http://localhost:8001/curate> (or reject
it, and check `alice-ingest`'s notifications page for the decision). Only
approval makes it retrievable — query it as `bob-query` with a phrase from
your file.

!!! warning "Try to break it"
    Submit with `classification=SECRET` as `alice-ingest` (cleared only to
    CUI): the upload itself is rejected with a 403. Tag validation is
    synchronous and server-side, derived from her verified claims — the form
    values are requests, not decisions.

## 7. Where everything lives

| Service | URL | Notes |
|---|---|---|
| Ingestion UI (upload / curate / search) | <http://localhost:8001> | Keycloak login |
| Retrieval debug API | <http://localhost:8002> | `/health`, `/debug/rag_search` |
| Keycloak admin | <http://localhost:8080> | `admin` / `admin` |
| Qdrant dashboard | <http://localhost:6333/dashboard> | see your chunks |
| LibreChat (chat plane) | <https://localhost:3080> | needs the one-time TLS host setup |

LibreChat's OIDC login needs a one-time host setup (trusted dev CA, an
`/etc/hosts` entry) — everything above works without it; do it when you want
the full chat-plane experience. See
[Dev environment setup](../dev-setup.md).

## Next steps

- [Understand the architecture](understanding-the-architecture.md) — what
  just happened, end to end
- [Deploy with Compose](deploy-compose.md) — configuration knobs, profiles,
  GPU, Milvus backend
- [Evaluation results](../evaluation-results.md) — run the golden-query
  harness against your stack

## Sources

Written from the canonical repo docs — read these for full depth and the
live-validation history behind every claim:

- [Dev environment setup](../dev-setup.md) (`docs/dev-setup.md`) — the
  authoritative walkthrough, seeded users, token grant, troubleshooting
- [Querying the corpus](../querying-the-corpus.md) — the three query paths
- [Architecture](../architecture-overview.md) (`ARCHITECTURE.md`) — sequence
  diagrams for every flow exercised above
