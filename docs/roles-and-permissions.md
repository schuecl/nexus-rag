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
| `rag-purge` | audited destruction (#123); in a two-person deployment (#279, gap G3), only *requesting* — a second, different `rag-purge` holder must independently hold it too, to *confirm* | deliberately separate from `rag-admin` so vocabulary admin and destruction can be different people (`deps.require_purge` docstring); confirming your own request (`common.purge.purge_confirmation_authorized`) |

## 2. Role × capability matrix (human identities)

✔ = allowed and enforced · ✖ = refused (HTTP code noted where it matters) ·
— = no route exists

| Capability (component) | anonymous | `rag-ingest` | `rag-query` | `rag-curate:<org>` | `rag-admin` | `rag-purge` |
|---|---|---|---|---|---|---|
| Upload + tag document (`ingestion-api` `POST /documents`) | ✖ 401 | ✔ tags validated against **own** claims, FR-18 (`upload.py` → `validate_against_claims`) | ✖ 403 | ✖ 403 | ✖ 403 | ✖ 403 |
| List/poll documents (`GET /documents/mine`, `/{id}`) | ✖ | ✔ **own uploads only** — `uploader_sub == sub`, else 404 (`upload.py:get_document`) | ✖ | ✖ (own queue view instead) | ✖ | ✖ |
| See curation queue (`GET /curate`) | ✖ | ✖ | ✖ | ✔ **filtered to `owner_org ∈ curatable_orgs`**, plus classification, releasability, and — for a pending document — `access_scope` (`curate.py:list_queue`/`list_documents`; issue #277, gap G1 closed for the read path) | ✖ | ✖ |
| Read a pending document's content | ✖ | own only | ✖ | ✔ within org + clearance + releasability + `access_scope` (`_check_curator_authority`) — issue #277 added the last of these; see gap G1 for what's still not covered | ✖ | ✖ |
| Approve / reject / correct tags (`POST /curate/{id}/approve\|reject`) | ✖ | ✖ | ✖ | ✔ org (else **404**, not 403 — existence-oracle fix #215) + clearance ceiling (403) + releasability held (403, FR-14.1); re-checked against the *old* doc on supersession (FR-7, `_validate_supersede`) | ✖ | ✖ |
| Query the corpus (`orchestration-mcp` `rag_search` / `/debug/rag_search`) | ✖ | ✖ | ✔ under the mandatory FR-26 filter (§4) | ✖ | ✖ | ✖ |
| Edit classification/releasability vocabulary (`ingestion-api` admin routes) | ✖ | ✖ | ✖ | ✖ | ✔ (`deps.require_admin`) | ✖ |
| Purge a document everywhere (`DELETE /documents/{id}`) | ✖ | ✖ | ✖ | ✖ | ✖ 403 | ✔ audited, reason required (#123) -- **only** when `PURGE_TWO_PERSON_REQUIRED` is unset (dev default); returns 409 otherwise (#279, gap G3) |
| File / confirm a purge request (`POST .../purge-request`, `.../confirm`) | ✖ | ✖ | ✖ | ✖ | ✖ 403 | ✔ file: any holder; confirm: **a different** holder only -- same `sub` as the requester gets 409 (#279, gap G3; `common.purge.purge_confirmation_authorized`) |
| Read the audit log | — | — | — | — | — | — (no route exists for anyone; see gap G2) |
| Download original uploaded bytes | — | — | — | — | — | — (no route exists; originals are write-only from the app, NFR-12) |

## 3. Data-visibility matrix (the privacy view)

The same facts inverted: **which document content can each identity read?**

| Document state | Uploader | Same-org curator (cleared) | Curator, *not* in the doc's `access_scope` group | `rag-query` user (cleared + caveats + in scope) | `rag-query` user outside any one dimension | `rag-admin` |
|---|---|---|---|---|---|---|
| `queued`/`processing`/`embedded` | metadata only (status poll) | not yet in queue | — | ✖ | ✖ | ✖ |
| `pending_review` | metadata only | **full content** (review requires it) | ✖ — hard-denied, no fallback (issue #277, gap G1) | ✖ (FR-11/FR-26: only `approved` matches) | ✖ | ✖ |
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

**G1 — Curators are not bound by `access_scope` (need-to-know) — closed by
#277.** `_check_curator_authority` (approve/reject/supersede) and
`curate.py:list_queue`/`list_documents` now check `access_scope` the same
way they already checked clearance and releasability: a hard requirement for
reading, approving, or rejecting a *pending* document, with **no fallback
and no grace period**. A curator outside a document's `access_scope` never
sees it in the queue and cannot act on it directly by id either, no matter
how long it has sat in `pending_review`.

An earlier version of this fix used a time-based grace period (prefer a
scope-matching curator for N hours, then open the document to every
org-authorized curator) to guarantee a document could never sit unreviewed
for want of a scope-matching curator. That was deliberately reverted: a
fallback that widens access on a timer defeats the purpose of a
need-to-know control — it just delays the same leak G1 exists to prevent,
and would have left `access_scope` unenforced at the actual approve/reject
call the whole time regardless.

The consequence, accepted on purpose rather than an oversight: **if no
curator in a document's owning org holds a group/org/sub that matches its
`access_scope`, that document has no one who can review it.** There is no
system-level fallback for this — it is an admin/provisioning problem (assign
the right group to a curator, or correct the document's `access_scope` tag,
which itself requires whoever submitted it or an admin to notice and fix)
rather than something the software works around by widening access. A
deployment that hands out narrow `access_scope` values should make sure at
least one curator per org actually holds each group in use, the same way it
already has to make sure at least one curator per org holds each
classification/releasability combination in use.

There is still no curator directory in this system (identity is
per-request, decoded from that request's own OIDC token —
`common/claims.py` has no concept of "every user who holds
`rag-curate:<org>`"), so nothing here can proactively warn an admin that a
document has no eligible reviewer; it can only be discovered by the document
staying in `pending_review`. A Keycloak admin API integration (new service
credential, admin REST calls, caching, a failure mode for an unreachable
admin API) could support that kind of proactive check, or a "documents with
no eligible curator" report. Not done here — deferred as its own, larger
decision if the lack of one proves painful in practice.

**G2 — One Postgres identity reads everything, including the audit log.**
Stated in `rag_search`'s own #125 docstring: no *route* exposes the audit log,
but any holder of `APP_DB_USER` can read every table — audit rows, document
metadata, session rows. The `grafana_ro` role proves the repo already has the
narrow-grant pattern; it stops at dashboards. Improvement: per-service DB
roles (`ingestion_api`, `orchestration_mcp`) with explicit grants; audit_log
INSERT-only for the query path; SELECT on audit_log granted to no application
role at all.

**G3 — Destruction is single-person — narrowed by #279.** `rag-purge` was
separate from `rag-admin` already (good), but one person holding it could
irreversibly destroy a document alone. `POST /documents/{id}/purge-request`
now records intent only (`common.purge.request_purge`); nothing is destroyed
until a **different** `rag-purge` holder confirms via
`POST .../purge-request/{request_id}/confirm`
(`common.purge.confirm_purge`) -- same-`sub` confirmation is refused
server-side (`purge_confirmation_authorized`), and an unconfirmed request
stops being confirmable once `PURGE_REQUEST_EXPIRY_HOURS` (default 24) has
passed, so a stale request can't sit as a loaded gun. Whether the two-person
path is *mandatory* is a deployment flag: `PURGE_TWO_PERSON_REQUIRED`
defaults true in code and in the Helm chart; `docker-compose.yml` sets it
false for the dev loop, since `seed-sample-data` and the dev realm only ever
provisioned one purge-capable identity (`dave-admin`) until now. The seeded
realm also gained a second, independent purge-only user (`eve-purge`,
`infra/keycloak/realm-export/nexus-rag-realm.json`) specifically so the
two-person path has someone to confirm with in dev, addressing the issue's
second point -- `dave-admin` holding `rag-purge` alongside every other role
still collapses the separation on its own account, but that account is
otherwise exercised by too much of `docs/dev-setup.md` and `scripts/` to
narrow here without a wider, separate change.

Narrowed, not closed: there is still no UI for the confirm step (the
existing curation page's delete button only exercises the single-person
path, unaffected in dev since that path stays on there) -- a `rag-purge`
holder without `rag-curate:*` can't even reach `curate_list.html` today, so
building one belongs to its own change rather than growing this one further.
No expiry sweep job either; see `PurgeRequest`'s own docstring for why that's
deliberate rather than deferred.

**G4 — Conflicting `rag-clearance:*` roles — resolved (#280).** A token
carrying two or more distinct `rag-clearance:<value>` roles is now rejected
at the verification boundary: `parse_claims` raises
`ConflictingClearanceError` (a `jwt.InvalidTokenError`), so every entry
point maps it to 401 like any other malformed token. Duplicate *identical*
values and zero clearance roles remain valid. Previously the first role in
`rag_roles` order won silently.

**G5 — Static service credentials, rotation documented and two of six now
no-downtime (#281).** Qdrant keys, NATS account passwords, the reranker
secret, `APP_DB_USER`, the session-token Fernet key, and the Keycloak client
secret are long-lived values in env/config.
[`docs/credential-rotation.md`](credential-rotation.md) has an
order-of-operations runbook per credential (stage 1), including which side
has to restart first and what breaks if the order is reversed. Stage 2 —
dual-concurrently-valid values per secret, so rotation needs no downtime — is
done for the two credentials the issue scoped concretely: the reranker
secret now accepts an optional `RERANKER_SHARED_SECRET_PREVIOUS`
(`reranker-service/app/main.py`), and the session-token key uses
`cryptography.fernet.MultiFernet` with an optional
`SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS` (`common/token_crypto.py`), with
existing sessions migrating off the retired key automatically as normal
token-refresh writes touch each row. Qdrant/NATS/Keycloak rotation stays
config-level on those systems' own side (no code change proposed for them in
the issue); `APP_DB_USER` remains open as a candidate not yet picked up.

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
