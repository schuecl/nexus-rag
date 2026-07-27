# Data Governance

How this system governs the data that feeds retrieval: what is controlled, by
what mechanism, and — stated as plainly as the controls themselves — what is
not controlled yet.

Related: [REQUIREMENTS.md](../REQUIREMENTS.md) for the numbered requirements
this cites, [ARCHITECTURE.md](../ARCHITECTURE.md) for component/flow diagrams,
[testing.md](testing.md) for how the controls below are verified, and
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
| Retention & destruction | **Not done** | [#123](https://github.com/schuecl/nexus-rag/issues/123) |
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
```

Retention is **unbounded at every stage**. Rejected and superseded documents
keep their Postgres row and their original file indefinitely; only the
supersede path removes anything, and only from Qdrant. There is no delete
route, `ObjectStore.delete()` has no callers, and the audit log is append-only
by design.

The practical consequence: **a mis-classified document cannot be destroyed.**
Flipping its status removes it from retrieval promptly — the FR-26 filter
requires `approved` — so the exposure is closed, but the bytes remain. For a
deployment where spillage remediation is a defined procedure, that is a gap
with a deadline attached. See
[#123](https://github.com/schuecl/nexus-rag/issues/123), which also covers the
unresolved tension between NFR-2's append-only audit log and any retention
schedule.

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
| No deletion path; no retention policy | [#123](https://github.com/schuecl/nexus-rag/issues/123) |
| Raw query text readable at the DB layer | [#125](https://github.com/schuecl/nexus-rag/issues/125) |
| No privacy threat model; scores enable membership inference | [#127](https://github.com/schuecl/nexus-rag/issues/127) |
| Retrieval quality not tracked over time | [#71](https://github.com/schuecl/nexus-rag/issues/71) |
| No runtime metrics or latency instrumentation | [#72](https://github.com/schuecl/nexus-rag/issues/72) |
| NFR-2 SIEM export unimplemented | [#73](https://github.com/schuecl/nexus-rag/issues/73) |
| No generation-side (Q→C→A) evaluation | [#74](https://github.com/schuecl/nexus-rag/issues/74) |
| No NetworkPolicy; storage reachable in-namespace | [#110](https://github.com/schuecl/nexus-rag/issues/110) |
