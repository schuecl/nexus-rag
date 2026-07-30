# Roles, permissions, and privacy across the pipeline

Who can do what, per component, and where the code enforces it — plus an honest
gap analysis of where least privilege currently stops (§7). Companion to
[`governance.md`](governance.md), which covers lifecycle/curation/retention;
this document covers **authorization and privacy** specifically.

Convention note: every cell in the matrices below names its enforcement point.
If a claim here has no code reference, treat it as a documentation bug. This
page describes what is *implemented and tested against mocks/BDD scenarios*
(and, where noted, *validated live*) — per the confidence-labeling convention
in [`testing.md`](testing.md).

## 1. Identity model — where authority comes from

Every access decision derives **server-side** from a verified Keycloak OIDC
token, parsed once by `services/common/common/claims.py` (`parse_claims`,
RS256 via JWKS). Nothing is ever trusted from client input; there is no
client-supplied filter anywhere in the pipeline.

| Claim | Carries | Used for |
|---|---|---|
| `rag_roles` | capability roles (below) | route gates (`ingestion-api/app/deps.py`) |
| `rag-clearance:<value>` (in `rag_roles`) | the user's single clearance level | classification ceiling, ranked via admin-configured `classification_levels` (`deps.allowed_classifications`) |
| `rag-releasability:<value>` (repeatable) | caveats the user holds (FVEY, NATO, ...) | upload validation (FR-18), curator authority (FR-14.1), retrieval filter (FR-26) |
| `groups`, `org`, `sub` | need-to-know scopes | `access_scope` matching at retrieval; org-scoped curation; uploader ownership |

Capability roles, deliberately small and non-overlapping:

| Role | Grants | Explicitly does NOT grant |
|---|---|---|
| `rag-ingest` | upload documents | seeing anyone else's documents |
| `rag-query` | `rag_search` retrieval | anything above the FR-26 filter |
| `rag-curate:<org>` | curation queue **for that org only** | curating other orgs; approving above own clearance/releasability |
| `rag-admin` | classification/releasability vocabulary routes | **any document or query access at all** — a boundary `claims.py` and `deps.py` state in code comments, on purpose |
| `rag-purge` | audited destruction (#123) | deliberately separate from `rag-admin` so vocabulary admin and destruction can be different people (`deps.require_purge` docstring) |

## 2. Role × capability matrix (human identities)

✔ = allowed and enforced · ✖ = refused (HTTP code noted where it matters) ·
— = no route exists

| Capability (component) | anonymous | `rag-ingest` | `rag-query` | `rag-curate:<org>` | `rag-admin` | `rag-purge` |
|---|---|---|---|---|---|---|
| Upload + tag document (`ingestion-api` `POST /documents`) | ✖ 401 | ✔ tags validated against **own** claims, FR-18 (`upload.py` → `validate_against_claims`) | ✖ 403 | ✖ 403 | ✖ 403 | ✖ 403 |
| List/poll documents (`GET /documents/mine`, `/{id}`) | ✖ | ✔ **own uploads only** — `uploader_sub == sub`, else 404 (`upload.py:get_document`) | ✖ | ✖ (own queue view instead) | ✖ | ✖ |
| See curation queue (`GET /curate`) | ✖ | ✖ | ✖ | ✔ **filtered to `owner_org ∈ curatable_orgs`**, plus classification/releasability (`curate.py:list_queue`/`list_documents`) and, for a pending document still inside `CURATOR_SCOPE_GRACE_PERIOD` of entering review, `access_scope` (issue #277, gap G1 narrowed — see below) | ✖ | ✖ |
| Read a pending document's content | ✖ | own only | ✖ | ✔ within org + clearance + releasability (`_check_curator_authority`) — **`access_scope` is preferred, not hard-enforced, at approve/reject: see gap G1** | ✖ | ✖ |
| Approve / reject / correct tags (`POST /curate/{id}/approve\|reject`) | ✖ | ✖ | ✖ | ✔ org (else **404**, not 403 — existence-oracle fix #215) + clearance ceiling (403) + releasability held (403, FR-14.1); re-checked against the *old* doc on supersession (FR-7, `_validate_supersede`) | ✖ | ✖ |
| Query the corpus (`orchestration-mcp` `rag_search` / `/debug/rag_search`) | ✖ | ✖ | ✔ under the mandatory FR-26 filter (§4) | ✖ | ✖ | ✖ |
| Edit classification/releasability vocabulary (`ingestion-api` admin routes) | ✖ | ✖ | ✖ | ✖ | ✔ (`deps.require_admin`) | ✖ |
| Purge a document everywhere (`DELETE /documents/{id}`) | ✖ | ✖ | ✖ | ✖ | ✖ 403 | ✔ audited, reason required (#123) |
| Read the audit log | — | — | — | — | — | — (no route exists for anyone; see gap G2) |
| Download original uploaded bytes | — | — | — | — | — | — (no route exists; originals are write-only from the app, NFR-12) |

## 3. Data-visibility matrix (the privacy view)

The same facts inverted: **which document content can each identity read?**

| Document state | Uploader | Same-org curator (cleared) | Curator, *not* in the doc's `access_scope` group | `rag-query` user (cleared + caveats + in scope) | `rag-query` user outside any one dimension | `rag-admin` |
|---|---|---|---|---|---|---|
| `queued`/`processing`/`embedded` | metadata only (status poll) | not yet in queue | — | ✖ | ✖ | ✖ |
| `pending_review` | metadata only | **full content** (review requires it) | ✖ while inside `CURATOR_SCOPE_GRACE_PERIOD` of entering review; **full content** once it elapses (or if never tracked) — gap G1, narrowed by #277 | ✖ (FR-11/FR-26: only `approved` matches) | ✖ | ✖ |
| `approved` | metadata only | full content via queue history? — no: once decided it leaves the queue | ✖ | ✔ retrievable chunks | ✖ (fails closed) | ✖ |
| `rejected` / `superseded` | metadata only | ✖ | ✖ | ✖ (validated live: golden-query harness asserts non-retrievability regardless of persona, FR-26) | ✖ | ✖ |
| `purged` | metadata (scrubbed row) | ✖ | ✖ | ✖ — chunks swept from **every** collection (#267 fix) | ✖ | ✖ |

## 4. Anatomy of the retrieval filter (FR-26)

Built server-side per request in `common/qdrant_filters.build_access_filter`,
applied to **both** the dense and BM25 legs, inside **every** per-classification
collection queried (#229 — the collection split bounds a store-level reader; it
does not replace this filter). All four conditions are conjunctive:

```
status        == "approved"
classification ∈ allowed_classifications(clearance)      # admin-configured rank
releasability  ∈ {NONE} ∪ user's rag-releasability roles
access_scope   ∈ {ALL_AUTHENTICATED, sub, org} ∪ groups   # need-to-know
```

Milvus (`VECTOR_BACKEND=milvus`, #160) enforces the same four conditions as a
boolean expression with injection-escaped values (`milvus_store.py`), without
the per-classification collection split (recorded "not yet" in its docstring).

## 5. Privacy protections for users of the system

Distinct from document confidentiality: what the pipeline knows **about its
users** and who can see that.

- **Query text is never stored** (#125). The FR-31 audit row records actor,
  outcome, applied filter, result count, and `query_chars` — not the question.
  Denied queries are the sharpest case (they record what someone tried to
  reach); they carry no text either.
- **Similarity scores are never returned** (#127) — score access enables
  membership inference against documents the caller cannot read. Latencies go
  to the audit log, not the response, for the same reason.
- **Existence oracles closed** (#215): a curator outside the owning org gets
  404, indistinguishable from "no such document"; uploader lookups behave the
  same for foreign documents.
- **Session tokens encrypted at rest** (#213/#234): the ingestion UI's stored
  OIDC access/refresh/id tokens are Fernet-encrypted in Postgres.
- **Audit trails are identity-keyed by design** (FR-31): queries and curation
  decisions are attributable. This is a deliberate accountability/privacy
  trade — see `governance.md` for the retention side, and gap G2 for who can
  technically read the trail today.

## 6. Machine identities (service accounts)

Least privilege between components, as deployed by compose/chart:

| Credential | Held by | Scope | Enforced by |
|---|---|---|---|
| Qdrant **read-only** API key | `orchestration-mcp` | `query_points` only | Qdrant RBAC (NFR-15; coarse — the FR-26 filter remains the real boundary) |
| Qdrant **read/write** API key | `ingestion-api`, `ingestion-worker` | create collections, upsert, payload updates, deletes | Qdrant RBAC |
| NATS per-subject accounts (#212/#232) | `ingestion-api` (publish), `ingestion-worker` (consume) | own subjects + `_INBOX.>` only | `infra/nats/nats.conf` permissions blocks |
| `APP_DB_USER` Postgres credential | both API services | full application schema — **gap G2** | Postgres auth |
| `grafana_ro` Postgres role | dashboards | `SELECT` on `document_metrics` only | `GRANT` in db-roles setup |
| Reranker shared secret (#216) | `orchestration-mcp` → `reranker-service` | `/rerank` | HMAC-style header check |
| Object-store credential | `ingestion-api` (put), purge path (delete) | originals bucket | store-side policy; no read-back route exists in the app |
| LiteLLM master key, Keycloak client secret | chat plane / OIDC confidential client | out of this repo's enforcement scope | respective services |

## 7. Gap analysis — where least privilege stops today

Documented so each can become its own issue; none is hidden behind a green
checkmark above. Ordered by how much they matter in a
documents-must-not-be-broadly-viewable deployment.

**G1 — Curators are not bound by `access_scope` (need-to-know) — narrowed,
not closed, by #277.** `_check_curator_authority` (approve/reject/supersede)
still checks only org, clearance ceiling, and releasability — approving a
document doesn't grant it any access beyond what its own `access_scope`
already encodes, so the actual leak is a curator *reading* a document they
have no need-to-know for while it's still `pending_review`.

Issue #277 addressed the read side: `curate.py:list_queue`/`list_documents`
now hide a pending document from a same-org, cleared, releasability-holding
curator who is *not* in its `access_scope` for `CURATOR_SCOPE_GRACE_PERIOD`
(default 24h, env-configurable) after it enters review, tracked by
`Document.pending_review_since`. Past that window — or for a row where that
timestamp is null (pre-#277 data) — visibility falls back to the pre-#277
org+clearance+releasability-only behavior, so a document can never rot
unreviewed for want of a scope-matching curator.

This is a *time-based approximation* of "prefer a scope-matching curator
when one exists," not the literal thing: there is no curator directory to
check against (identity is per-request, decoded from that request's own
OIDC token — `common/claims.py` has no concept of "every user who holds
`rag-curate:<org>`"), so the system cannot know whether a scope-matching
curator actually exists, only how long it's been since one had a chance to
act. Residual gaps, accepted for now:

- A curator who already holds (or is handed) a document's id some other way
  — an audit log entry, a notification — can still call
  `POST /curate/{id}/approve|reject` directly during the grace window even
  without `access_scope` membership; only the *queue listing* is
  scope-gated, not the write path. Approving doesn't widen access beyond the
  document's own `access_scope`, so this isn't a confidentiality leak beyond
  what already existed, but it does mean the "prefer a scope-matching
  curator" intent can be bypassed by someone who already suspects a document
  exists.
- A grace period long enough to usually find a scope-matching curator in a
  small org is a window an urgent same-org document sits unreviewed in a
  large one, and vice versa — there's no per-org tuning, just the one
  env var.

Building a real curator directory (a Keycloak admin API integration: new
service credential, admin REST calls, caching, a failure mode for when
Keycloak's admin API is unreachable) would let the queue check "does a
scope-matching curator exist" directly instead of approximating it with a
clock, and would also let it enforce the same check at approve/reject. Not
done here — deferred as its own, larger decision if the grace-period
approximation proves insufficient in practice.

**G2 — One Postgres identity reads everything, including the audit log.**
Stated in `rag_search`'s own #125 docstring: no *route* exposes the audit log,
but any holder of `APP_DB_USER` can read every table — audit rows, document
metadata, session rows. The `grafana_ro` role proves the repo already has the
narrow-grant pattern; it stops at dashboards. Improvement: per-service DB
roles (`ingestion_api`, `orchestration_mcp`) with explicit grants; audit_log
INSERT-only for the query path; SELECT on audit_log granted to no application
role at all.

**G3 — Destruction is single-person.** `rag-purge` is separate from
`rag-admin` (good), but one person holding it can irreversibly destroy alone.
Usual production expectation for destruction is a two-person rule: a purge
*request* row plus an independent confirmation before execution.

**G4 — Conflicting `rag-clearance:*` roles — resolved (#280).** A token
carrying two or more distinct `rag-clearance:<value>` roles is now rejected
at the verification boundary: `parse_claims` raises
`ConflictingClearanceError` (a `jwt.InvalidTokenError`), so every entry
point maps it to 401 like any other malformed token. Duplicate *identical*
values and zero clearance roles remain valid. Previously the first role in
`rag_roles` order won silently.

**G5 — Static service credentials with no rotation story.** Qdrant keys, NATS
account passwords, the reranker secret, and `APP_DB_USER` are long-lived
values in env/config. No documented rotation procedure or dual-key overlap
window exists. At minimum, document rotation; better, support two concurrently
valid values per secret so rotation needs no downtime.

**G6 — `rag-query` retrieval has no per-document view audit granularity.**
The audit row records the filter and result count, not *which* documents were
returned (a deliberate privacy choice mirroring #125 — recording returned doc
ids would rebuild query-content inference). Recorded here as a decision, so
the next person asking "can we know who saw document X?" finds the answer
("no, by design — only who *could* have") written down.

## 8. Reviewing a change against this document

A PR touches authorization if it adds a route, widens a query, adds a role, or
touches `claims.py` / `qdrant_filters.py` / `deps.py` / `curate.py`'s
authority helpers. For such PRs: update the matrix in the same change, and
state which gap (if any) it narrows or widens. The BDD scenarios under
`tests/e2e/features/` and the golden-query harness's persona checks are the
executable form of §§2–4; a matrix change without a matching scenario change
is a red flag.
