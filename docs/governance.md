# Data Governance

How this system governs the data that feeds retrieval: what is controlled, by
what mechanism, and — stated as plainly as the controls themselves — what is
not controlled yet.

Related: [REQUIREMENTS.md](../REQUIREMENTS.md) for the numbered requirements
this cites, [ARCHITECTURE.md](../ARCHITECTURE.md) for component/flow diagrams,
[testing.md](testing.md) for how the controls below are verified,
[roles-and-permissions.md](roles-and-permissions.md) for the per-role
authorization/privacy matrix and its least-privilege gap analysis, and
[SECURITY.md](../SECURITY.md) for the vulnerability-reporting surface.

## Why this document exists

Most of the practices a RAG data-governance framework asks for are already
implemented here — role-based access control, a controlled metadata
vocabulary, a mandatory human curation gate, an append-only audit log. They
were built as requirements (FR-*/NFR-*) rather than as "governance", so
nothing in the repo presented them as a coherent answer to *"how is this data
governed?"* This document assembles that answer, and gives the remaining gaps
somewhere to live.

It is deliberately a map of existing behaviour, not a policy aspiration. Every
"implemented" row below points at code.

## Status convention

This document uses the same three-level convention as `docs/dev-setup.md`
(P1, REQUIREMENTS.md Section 11), because "we have access control" conflates
very different confidence levels:

- **Implemented** — the code exists and does what it says.
- **Tested against mocks** — exercised, but against in-process substitutes.
- **Validated live** — exercised against the real stack.

Anything not in one of those states is marked **Not done** and linked to an
issue.

## Practice map

| Governance practice | Status | Mechanism |
|---|---|---|
| Access control (RBAC) | **Validated live** | Keycloak client roles → OIDC claims → server-side filter |
| Metadata management | **Validated live** | Section 6.3 schema + admin-configurable vocabulary (C9) |
| Data quality | **Validated live** | Mandatory curation queue (FR-10..FR-16) |
| Auditability | **Validated live** | FR-31 audit log, append-only (NFR-2) |
| Document versioning | **Validated live** | FR-7 supersede chain |
| Durability of source | **Validated live** | NFR-12 object store retains every original |
| Embedding provenance | **Not done** | [#122](https://github.com/schuecl/nexus-rag/issues/122) |
| Query/response lineage | **Not done** | [#122](https://github.com/schuecl/nexus-rag/issues/122) |
| Retention & destruction | **Partial** | Purge path implemented+validated ([#136](https://github.com/schuecl/nexus-rag/pull/136)); schedule proposed in [Retention and destruction](#retention-and-destruction), awaiting ratification ([#123](https://github.com/schuecl/nexus-rag/issues/123)) |
| Query confidentiality | **Partial** | See [Query confidentiality](#query-confidentiality-and-user-privacy) |
| Privacy threat model | **Not done** | [#127](https://github.com/schuecl/nexus-rag/issues/127) |
| Observability of quality | **Partial** | FR-30 harness exists; not tracked over time ([#71](https://github.com/schuecl/nexus-rag/issues/71)) |
| SIEM export | **Not done** | [#73](https://github.com/schuecl/nexus-rag/issues/73) |
| Ontology / taxonomy management | **Out of scope** | Graph-RAG concern; see [Non-goals](#non-goals) |

## Access control

**The governing rule: authorization is derived server-side from verified
claims and is never accepted from a caller.**

Identity comes from Keycloak as an OIDC access token. `common/claims.py`
verifies the signature against JWKS (RS256, audience- and issuer-checked) and
derives every authorization-relevant attribute from client roles:

| Attribute | Source | Notes |
|---|---|---|
| Clearance | `rag-clearance:<value>` role | Single ranked value |
| Releasability | `rag-releasability:<value>` roles | One or more caveats |
| Curator authority | `rag-curate:<org>` roles | Per-org |
| Capability | `rag-ingest`, `rag-query`, `rag-admin` | |

These are **client roles, not user attributes** (#104, #116). That is a
governance decision, not an implementation detail: granting or revoking a
caveat becomes a discoverable, auditable role assignment in the Keycloak admin
console, rather than a free-text attribute a typo could silently get wrong.

At query time `common/qdrant_filters.py` builds a mandatory Qdrant filter from
those claims — approved status, classification at or below clearance,
releasability the user holds, and access scope — and applies it to **both**
legs of hybrid retrieval, so neither the dense nor the sparse path can be used
to bypass it (FR-26).

Two properties worth stating explicitly for a reviewer:

- **The filter is not a tool argument.** `orchestration-mcp` reads the bearer
  token from the transport, never from an MCP tool parameter, so a model
  cannot be induced to widen its own access by anything in a prompt or a
  retrieved document.
- **It fails closed.** An unknown clearance resolves to an empty allowed-
  classification list, which matches nothing rather than everything.

### The reranker-service boundary (issue #216)

`reranker-service` is downstream of that filter, not a participant in it. By
the time a chunk's text reaches `POST /rerank`, FR-26 has already run in
`orchestration-mcp` and the content is cleared for that specific caller —
`reranker-service` receives it with no notion of who that caller was.

That makes it a deliberate exception to "authorization is derived server-side
from verified claims": this service does not check claims at all, because it
is not an access-control decision point. Its authorization question is
narrower — *is this caller orchestration-mcp* — and is answered by a shared
secret (`rerankerService.sharedSecret` in the Helm chart,
`RERANKER_SHARED_SECRET` in Compose), checked in addition to the Helm chart's
NetworkPolicy (issue #110/#131) restricting who can reach it on the network.

Forwarding the caller's own OIDC token instead and re-verifying it in
`reranker-service` was considered and rejected: FR-26 enforcement already
happened once, upstream, and duplicating it here would put the same
classification logic in a second place it can drift out of sync with the
first — a maintenance liability with no additional access-control benefit,
since a caller who can reach `reranker-service` at all has already been
authorized by `orchestration-mcp` for the specific content in the request.

The shared secret is optional and unset by default — the chart and Compose
stack both run exactly as before this issue if it isn't configured, and
`reranker-service` logs a startup warning in that case rather than staying
silent about it. Set it in any deployment where the NetworkPolicy might not
be the only thing standing between an unauthorized caller and retrieved
chunk content, which — given a NetworkPolicy's dependence on CNI support — is
any deployment that hasn't specifically verified its CNI enforces policy.

## Data quality

The curation queue **is** the data-quality control. Nothing reaches retrieval
without a human decision: ingestion drives a document to `pending_review`, and
only a curator's approval flips its chunks to `approved` (FR-11).

Curator authority is itself bounded (FR-14): a curator may only act on
documents owned by an org they hold `rag-curate:<org>` for, may not approve a
classification above their own clearance, and may not approve releasability
caveats they do not themselves hold.

**What it validates:** that tagging is within the submitter's authority, and
that a human with sufficient clearance has seen it.

**What it does not validate:** whether the document's *content* is accurate,
current, or internally consistent. There is no automated profiling, freshness
check, or duplicate detection. A correctly-tagged, human-approved document
that happens to be wrong will be retrieved and cited. This is a real limit of
the current model and should not be read as an accuracy guarantee.

## Metadata and controlled vocabulary

Every document carries the Section 6.3 metadata set (classification,
releasability, access scope, originator, doc type, program/community,
effective date). Classification and Releasability are drawn from
admin-configurable tables (C9) rather than free text, so the vocabulary is
governed centrally and values cannot drift per-uploader.

Uploader tagging is re-validated server-side against the submitter's own
claims (FR-18) — the UI constrains the dropdown, but the constraint is
enforced again in `common/metadata.py`, which is what actually holds.

## Lineage and provenance

**Partial, and the weakest area.**

What is traceable today:

- A chunk carries `document_id`, `chunk_index`, `heading`, `page_or_slide`,
  and `filename`, so a retrieved passage can be traced to its source document
  and location (FR-27's citation basis).
- The original file is retained in the object store (NFR-12), independent of
  Postgres and Qdrant, so the source of any chunk can be re-examined.
- Every ingestion, curation decision, and query attempt is recorded in the
  audit log keyed on OIDC identity (FR-31).

What is **not** traceable:

- **Which embedding model produced a stored vector.** Nothing records it, and
  the ingestion and query paths resolve `EMBEDDING_MODEL` independently. A
  model change silently breaks the comparability of query and document vectors
  with no error — see [#122](https://github.com/schuecl/nexus-rag/issues/122).
- **A query to its answer.** Audit entries have no correlation id, so a
  specific response cannot be tied to the specific retrieval that produced it.
- **Reranker version**, chunking parameters, or filter version at the time a
  chunk was written.

For a system whose value proposition is grounded, citable answers, this is the
gap most worth closing next.

## Query confidentiality and user privacy

A user's queries are sensitive in their own right — often more revealing than
the documents they return, because they disclose *intent*: what someone is
investigating, when, and how persistently. In this deployment context that is
not a hypothetical concern.

### What the application exposes

**No API route reads the audit log.** `AuditLogEntry` is written by
`ingestion-api` and `orchestration-mcp` and never read back by any endpoint.
The `rag-admin` role gates only the Classification/Releasability vocabulary
routes in `app/routes/admin.py` — it grants **no** access to queries,
documents, or audit records.

Document access is scoped by ownership, not by privilege:

| Route | Scope |
|---|---|
| `GET /documents/mine` | `uploader_sub == caller` |
| `GET /documents/{id}` | 404 unless the caller is the uploader |
| `GET /curate/queue` | `pending_review` in the curator's own orgs |
| `GET /notifications` | `recipient_sub == caller` |

So an application administrator cannot read another user's queries or
documents through the application. That is the intended property and it holds
today.

### Where the property does not hold

**At the database layer.** `audit_log.detail` stores the **raw query text**
verbatim, plus the retrieved `result_document_ids` and the applied filter —
which itself embeds the user's `sub`, groups, and org. Anyone holding
`APP_DB_USER` credentials can `SELECT * FROM audit_log` and read every user's
query history; the bootstrap superuser can do the same.

The NFR-2 hardening makes that table append-only *for the application role*
(`GRANT SELECT, INSERT`, ownership moved away) — it prevents tampering, which
is what it was designed for. It does **not** restrict reading, and nothing
documents who is entitled to hold those credentials.

Two consequences worth naming:

1. **A DBA or platform administrator has a surveillance capability** that the
   application deliberately denies to `rag-admin`. The control boundary the
   application draws is not mirrored at the storage layer.
2. **The audit log is an aggregation risk.** Query text about classified
   material is plausibly classified itself, and it is being written to a table
   with weaker access control than the documents it describes — and, per
   [#123](https://github.com/schuecl/nexus-rag/issues/123), one that cannot be
   selectively expunged.

### The intended model

Accountability and content confidentiality are separable, and should be
separated:

| Data | Purpose | Who should see it |
|---|---|---|
| Actor, timestamp, action | Accountability (FR-31) | Auditors, admins |
| Allow/deny outcome + reason | FR-26 verification | Auditors, admins |
| Applied filter *shape* | Proving the filter was enforced | Auditors, admins |
| Result count | Anomaly detection | Auditors, admins |
| **Raw query text** | Investigation only | **Break-glass, itself audited** |
| **Document content** | — | **Nobody, via audit** |

The first four answer every accountability question FR-31 exists for —
*did the filter apply, to whom, and what did it permit* — without disclosing
what anyone asked. Options for the query text are to omit it, store a hash
(preserving repeat-query correlation without content), or hold it in a
separately-granted table behind an audited break-glass path.

Tracked as [#125](https://github.com/schuecl/nexus-rag/issues/125).

### Inference, as distinct from access

The section above is about what an operator can *read*. A separate question is
what anyone can *infer* without reading anything they are not entitled to —
document-membership inference from retrieval behaviour, and the fact that a
reader of the vector store gets the corpus in cleartext because the chunk
payload carries `text` alongside the vector.

Authorization (FR-26) does not address either: it governs what is returned,
not what the act of returning reveals. There is no articulated privacy threat
model here yet, and two concrete leaks are already known — similarity scores
are returned to callers, and every classification level shares one Qdrant
collection. Tracked as
[#127](https://github.com/schuecl/nexus-rag/issues/127), which maps this
system against the OWASP RAG Security guidance and records which of those
controls are already correct.

## Lifecycle

```
submit ──▶ queued ──▶ processing ──▶ embedded ──▶ pending_review
                                                        │
                                          ┌─────────────┴─────────────┐
                                          ▼                           ▼
                                      approved                    rejected
                                          │
                                          ▼
                                     superseded  (FR-7, on approval of a successor)

any state ──▶ purged  (#123/#136: admin-gated destruction; tombstone remains)
```

Spillage **can** now be remediated: the purge path
([#136](https://github.com/schuecl/nexus-rag/pull/136), merged and
validated) destroys a document's chunks in the vector store, deletes the
object-store original, and tombstones the Postgres row with every
content-bearing field scrubbed to `[purged]` — gated by the dedicated
`rag-purge` role (deliberately not `rag-admin`, which must stay a
no-data-access role), and itself audited as `document.purged` without
retaining the filename.

What remains unbounded is everything the purge path is not pointed at:
rejected and superseded documents that nobody purges, notifications, and the
audit log (append-only by design). The retention schedule below is the
proposed answer; until it is ratified and its expiry mechanism exists,
growth outside the purge path is still monotonic. See
[Retention and destruction](#retention-and-destruction) and
[#123](https://github.com/schuecl/nexus-rag/issues/123).

## Retention and destruction

Issue [#123](https://github.com/schuecl/nexus-rag/issues/123)'s three policy
items, resolved into a concrete **proposed** schedule the requirement owner
can ratify, adjust, or reject line by line. Status labels: **Implemented**
means the mechanism exists on `main` today; **Proposed** means this document
is the design and nothing enforces it yet. Nothing here is silently in
force — until ratification, the implemented pieces (purge, session reaping)
are the only destruction that happens.

### Proposed retention schedule, per data class

| Data class | Retention (proposed) | Destruction mechanism | Status |
|---|---|---|---|
| Original files (object store) | Life of the document; destroyed on purge. Rejected documents: eligible for purge review after **90 days**; superseded originals: retained **1 year** after supersession, then eligible | `purge_document()` — deletes the original via `ObjectStore.delete()` | Purge **Implemented**; the 90-day/1-year eligibility sweep **Proposed** |
| Chunks (vector store, either backend) | Lifecycle-bound to the document; never outlive it | Deleted on supersede (FR-7) and on purge; backend-agnostic via the #160 seam | **Implemented** |
| `documents` rows | Active rows: life of the document. Purged rows become **tombstones, kept indefinitely** — the tombstone *is* the destruction evidence (id, timestamps, `purged` status; every content field scrubbed to `[purged]`) | Tombstoning on purge; no hard delete of tombstones | **Implemented** |
| Notifications | **90 days** after creation, read or not | A reaper on the #108 pattern (scheduled sweep in-app; the app role may DELETE its own notifications) | **Proposed** |
| `oauth_states` / `user_sessions` | Bounded lifetimes, reaped continuously | #108 (merged) | **Implemented** |
| `audit_log` | Local minimum **1 year** (configurable, `AUDIT_RETENTION_DAYS`), then eligible for administrative expiry — **except** destruction-evidence entries (`document.purged`, `audit.expired`), retained **7 years** | The audited-expiry process below; never the application role | **Proposed** |
| SIEM copy of audit events | Governed by the environment's SIEM retention schedule, not this system | #73 export (in review) hands custody off; the SIEM is the long-term audit store | Out of this system's control — stated, not assumed |
| Traces / metrics / process logs | Backend-governed (Tempo/Prometheus/Loki retention). Carry ids, counts, and sizes only — no corpus content or query text by construction (#125's rule, enforced in #132/#134/#158) | Observability backend configuration | Out of scope for this schedule |

### Destruction authority and evidence

| Question | Answer |
|---|---|
| Who may purge a document? | Holders of the dedicated **`rag-purge`** client role only. Deliberately not `rag-admin`: that role gates vocabulary and grants no data access — a boundary this document states elsewhere, and hanging irreversible destruction on it would widen the most privileged role into a data-touching one. The two authorities can (and in a real deployment should) be held by different people |
| Who may expire audit entries? | No application role, ever — the append-only grant (NFR-2) stays exactly as it is. Only the administrative expiry process below, run by the platform admin under the bootstrap credentials, on the ratified schedule |
| What evidence survives a purge? | The tombstoned `documents` row (id, uploader sub, timestamps, `purged` status) and a `document.purged` audit entry recording who, when, and the stated reason — deliberately **not** the filename or any other content-bearing field, so the evidence chain does not itself become a lower-classification aggregation of the destroyed content |
| What evidence survives an audit expiry? | An `audit.expired` entry per run: the count of rows destroyed and the time range they covered — never their contents |

### Reconciling NFR-2 with scheduled destruction (the audited-expiry design)

NFR-2's append-only audit log and "records must be destroyed on a schedule"
are in direct conflict, and the repo previously resolved it by silently
choosing the first. The explicit resolution proposed here:

**Audit entries are subject to retention, with the append-only grant
untouched.** The application role keeps exactly its current `SELECT`/`INSERT`
grant — expiry is not an application capability, cannot be reached from any
route, and adds no new grant. Instead, a separate administrative one-shot (the
`lock-down-db-grants` pattern (#278, formerly `harden-audit-log`): same
credentials, same invocation shape, Compose
one-shot / Kubernetes CronJob) performs the schedule:

1. **Precondition, checked every run:** SIEM export (#73) is configured
   (`SIEM_SYSLOG_HOST` set) — local expiry without an off-box custody chain
   would be destruction of the only copy. If unset, the job refuses and exits
   non-zero rather than warning and proceeding.
2. Delete `audit_log` rows with `created_at < now() − AUDIT_RETENTION_DAYS`,
   **excluding** `action IN ('document.purged', 'audit.expired')`, which
   follow the 7-year evidence retention.
3. Write one `audit.expired` entry recording the run: rows destroyed, range
   covered, retention setting in force. (The job holds superuser credentials,
   so writing to the append-only table is unproblematic; the entry is the
   run's evidence.)
4. Emit the same summary to the SIEM (the entry is an `AuditLogEntry`, so
   the #73 hook forwards it like any other).

The job is deliberately **not implemented in this change**: its parameters
are exactly what ratification decides, and shipping an unratified destroyer
of audit records would invert this document's own rule that destruction is a
deliberate, owned decision.

### Audit-content minimization

The concern: audit details can accumulate into a lower-classification
aggregation of higher-classification content that cannot be selectively
expunged. Current state and the remaining decision:

- **Query text** — resolved: removed from audit details by #125/#128
  (merged); only `query_chars` remains.
- **Purge entries** — resolved: #136 keeps the filename out of
  `document.purged` details by design.
- **Filenames elsewhere** — open: `document.submit`/`document.embedded`/
  curation audit details still record the filename. **Proposal:** stop —
  `target_id` (the document id) is the stable key, the filename is
  resolvable through the `documents` row while the document exists, and
  after a purge the row is scrubbed, which is precisely the property audit
  details currently defeat. Notifications keep the filename (they are
  user-facing messages, and fall under the 90-day notification retention).
  One-line-per-call-site change; filed as the follow-up to ratification.

### What ratification needs to decide

1. The four proposed periods: 90 days (rejected-purge eligibility, and
   notifications), 1 year (superseded originals, and `AUDIT_RETENTION_DAYS`),
   7 years (destruction evidence). Each is a placeholder for the governing
   records schedule, chosen to be defensible defaults rather than claims
   about any specific mandate.
2. Whether audit expiry is wanted at all, or NFR-2's "keep forever" stands
   with the SIEM as the retention-schedule surface instead.
3. The filename-minimization proposal above.

Once decided: implement the notification reaper and the expiry one-shot,
flip this section's **Proposed** rows to **Implemented**, and record the
decision (who, when) here.

## Roles and responsibilities

In this system's own vocabulary rather than generic governance terms:

| Governance role | Here | Authority |
|---|---|---|
| Data owner | Uploader (`rag-ingest`) | Submits and tags, bounded by own clearance/releasability (FR-18) |
| Data steward | Curator (`rag-curate:<org>`) | Approves, rejects, or corrects tags for their org, capped by own clearance (FR-14) |
| Vocabulary admin | `rag-admin` | Manages the Classification/Releasability lists (C9). **No** data access |
| Consumer | `rag-query` | Retrieval only, always through the FR-26 filter |
| Platform admin | *(not an application role)* | Holds DB/object-store credentials. Ungoverned by the application — see above |

That last row is the one a reviewer should ask about: it is the only role with
access the application does not mediate, and it is currently undefined.

## Non-goals

Stated so their absence reads as a decision rather than an oversight:

- **Ontology and taxonomy management.** These are graph-RAG governance
  concerns. This system is vector + BM25 hybrid retrieval with a flat,
  admin-managed controlled vocabulary; there is no knowledge graph to govern.
  Would become in scope if
  [#91](https://github.com/schuecl/nexus-rag/issues/91) is pursued.
- **PII detection and redaction.** The corpus is organizational documents
  under classification control, not personal data; the governing regime is
  classification/releasability, not GDPR-style data-subject rights.
- **Data sovereignty via region selection.** The deployment target is
  air-gapped (NFR-1), which is a stronger constraint than regional residency.
- **Cross-organization data cataloging.** The metadata schema serves retrieval
  filtering, not enterprise-wide asset discovery.

## Known gaps

| Gap | Issue |
|---|---|
| Embedding-model provenance; no re-embedding path | [#122](https://github.com/schuecl/nexus-rag/issues/122) |
| Retention: schedule drafted below but unratified; audited-expiry job unimplemented | [#123](https://github.com/schuecl/nexus-rag/issues/123) |
| Raw query text readable at the DB layer | [#125](https://github.com/schuecl/nexus-rag/issues/125) |
| No privacy threat model; scores enable membership inference | [#127](https://github.com/schuecl/nexus-rag/issues/127) |
| Retrieval quality not tracked over time | [#71](https://github.com/schuecl/nexus-rag/issues/71) |
| No runtime metrics or latency instrumentation | [#72](https://github.com/schuecl/nexus-rag/issues/72) |
| NFR-2 SIEM export unimplemented | [#73](https://github.com/schuecl/nexus-rag/issues/73) |
| No generation-side (Q→C→A) evaluation | [#74](https://github.com/schuecl/nexus-rag/issues/74) |
| No NetworkPolicy; storage reachable in-namespace | [#110](https://github.com/schuecl/nexus-rag/issues/110) |
