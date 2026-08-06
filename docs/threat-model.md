# Privacy threat model

Authorization and privacy are different questions. **Authorization** asks
*may this user retrieve this document* — that model is built, documented in
[`roles-and-permissions.md`](roles-and-permissions.md), and enforced by the
mandatory FR-26 filter. **Privacy** asks what a user, an operator, or an
adversary can infer about the corpus or about other users' activity
*without* retrieving anything they are not entitled to. Authorization being
correct does not make privacy correct — a caller can stay entirely inside
their own FR-26 filter and still learn things the filter was never designed
to hide, or the retrieval path can leak to someone who was never subject to
the filter at all.

This document is that second model: adversary classes, what each can
observe, what it can infer, and which control (if any) answers it. It is
scoped to inference and disclosure, not lifecycle/retention (see
[`governance.md`](governance.md)) or the authorization matrix itself (see
[`roles-and-permissions.md`](roles-and-permissions.md), which this document
cross-references rather than repeats).

Written against [#127](https://github.com/schuecl/nexus-rag/issues/127),
using the [OWASP RAG Security Cheat
Sheet](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)
as the control checklist and Arzanipour, Behnia, Ebrahimi, Dutta, *"RAG
Security and Privacy: Formalizing the Threat Model and Attack Surface"*
(arXiv:2509.20324) as the adversary taxonomy — the first formal threat model
for RAG systems in the literature, and a better fit than a from-scratch
taxonomy for a system that is otherwise ordinary hybrid retrieval.

## Adversary taxonomy

The paper's taxonomy is two axes: **model access** (black-box query access
only, vs. white-box access to internals) crossed with **data knowledge**
(unaware of the target corpus's contents, vs. aware of specific documents in
it — the capability membership inference actually tests). That gives four
classes, instantiated here against this platform's real identities rather
than left abstract:

| Class | Model access | Data knowledge | Instantiated as, on this platform |
|---|---|---|---|
| A-I | Black-box (query only) | Unaware | An `rag-query` holder exploring the corpus with no prior knowledge of what it contains |
| A-II | Black-box (query only) | Aware | An `rag-query` holder who suspects or knows a specific document exists and is testing whether it's retrievable |
| A-III | White-box (internals) | Unaware | A credential holder below the HTTP layer with no specific document in mind — e.g. anyone who can reach Qdrant/Postgres on the network but isn't targeting a document |
| A-IV | White-box (internals) | Aware | The same white-box position, now targeting a specific document or user's activity |

A-I/A-II are **observers**: everything they see comes through `rag_search`
(or `/debug/rag_search`), gated by the FR-26 filter and by holding
`rag-query` at all. A-III/A-IV are **insiders**: they hold a credential for
something the HTTP layer sits in front of — a Qdrant API key, a Postgres
role, the bootstrap superuser, or a compromised service — and FR-26 never
runs for them, because it is enforced by the querying service, not the
store.

The observer/insider split matters because the controls that answer each are
almost disjoint. A control that closes an observer channel (e.g. suppressing
similarity scores) does nothing for an insider who reads the store directly;
a control that bounds an insider's blast radius (e.g. splitting collections
by classification) does nothing for an observer who was never going to reach
the store at all. Treating "membership inference" as one problem obscures
that it needs two different answers depending on which side of the HTTP
boundary the adversary is on.

## Attack surfaces

Three attack surfaces, each read against all four adversary classes where
that combination is meaningful.

### 1. Document-level membership inference

*Can the adversary determine whether a specific document (or a document
matching a description) exists in the corpus, without being authorized to
retrieve it?*

**A-I / A-II (observers).** The only channel available to a black-box caller
is the shape of `rag_search`'s response: which chunks come back, in what
order, and — until this was fixed — their raw similarity scores. OWASP's
guidance is direct: *"Never return similarity scores to users or agents, as
scores enable differential analysis."* A caller who can see the score
gradient can probe with crafted queries and read the presence/strength of a
match even when the returned chunk itself is otherwise unremarkable — and
the *absence* of an expected score is informative too, including for
documents the caller's own FR-26 filter excludes.

- **Control:** `rag_search.py`'s response carries `id` + `payload` only,
  never `score` — removed by
  [#128](https://github.com/schuecl/nexus-rag/pull/128), see
  `services/orchestration-mcp/app/rag_search.py`'s `result["results"]`
  construction, which documents the reasoning inline. Reranking still uses
  scores internally; they never cross the response boundary. Latencies are
  written to the audit log, not the response, for the same reason.
- **Residual risk:** rank order itself is a (much weaker) signal —
  consistently high rank for a query naming a specific title/author is still
  some evidence the document exists. This is inherent to returning ranked
  results at all and is not separately mitigated; it is the residual channel
  the second issue comment on #127 discusses (see "Considered and rejected"
  below).
- **Scope boundary that already helps:** an A-I/A-II adversary is, by
  definition, a `rag-query` holder — FR-26 already denies them any result
  for a document outside their own clearance/releasability/scope. The
  membership-inference question that remains is narrower than "does this
  document exist at all": it's "does a document I *could* see, matching
  this description, exist" — the score-suppression above is what closes
  that.

**A-III / A-IV (insiders).** FR-26 does not apply to them at all, because it
is enforced in `orchestration-mcp`, not in Qdrant. Whether a document exists
is not inference for an insider who can read the store — it's direct
observation: `SELECT` (or `scroll`/`query_points` with no filter) against
the collection returns the payload verbatim, including `text`, `document_id`,
`classification`, and `releasability`. The mitigations here are therefore
about *bounding what an insider's credential reaches*, not about hiding a
gradient:

- **Per-classification collections** ([#229](https://github.com/schuecl/nexus-rag/issues/229),
  implemented in [#267](https://github.com/schuecl/nexus-rag/pull/267),
  `common/qdrant_store.py`'s `classification_collection_name`). A store-level
  reader with a key scoped to (or who only reaches) one collection sees one
  classification level's corpus, not every level's. This is a structural
  bound on blast radius, not a claims check — an insider who reaches *every*
  collection is unaffected, which is why it's listed under "bounds", not
  "closes".
- **Read-only vs. read-write Qdrant keys** (NFR-15): `orchestration-mcp`
  holds a key that can query but not write; only `ingestion-api` and
  `ingestion-worker` hold the read/write key. This narrows what a compromised
  `orchestration-mcp` can do (no tampering), not what it can read.
- **NetworkPolicy** ([#110](https://github.com/schuecl/nexus-rag/issues/110),
  [#131](https://github.com/schuecl/nexus-rag/pull/131)): default-deny
  policies restrict which pods can reach Qdrant/Postgres at all in the
  chart — an A-III/A-IV adversary who isn't one of the RAG services'
  own pods is denied at the network layer before the store-level bounds
  above are even relevant. Depends on the cluster's CNI actually enforcing
  NetworkPolicy, which the chart cannot itself guarantee.
- **Per-service Postgres roles** ([#278](https://github.com/schuecl/nexus-rag/issues/278),
  implemented in [PR #292](https://github.com/schuecl/nexus-rag/pull/292),
  detailed in `roles-and-permissions.md` gap G2): `orchestration-mcp`'s DB
  role cannot read `documents` at all; no application role can read
  `audit_log`. An insider holding one service's credential doesn't get the
  others'.

### 2. Retrieved-content leakage

*Once content is legitimately retrieved for an authorized caller, where does
it end up, and who else can reach it there?*

This surface is about **provenance-safe cleartext storage**, not
authorization — the chunk payload itself.

- **The core exposure:** `ingestion-worker/app/processing.py` writes
  `"text": chunk.text` into the Qdrant payload alongside the vector. OWASP's
  position is that embeddings warrant the same access controls and at-rest
  encryption as source documents; here it applies with more force, because
  the payload *is* the document, not a lossy representation of it —
  embedding inversion is not even the relevant attack when the plaintext
  sits right next to the vector.
- **This is bounded, not eliminated.** The collection split, NetworkPolicy,
  and RO/RW key separation above all reduce *who* can reach that cleartext
  and *how much* of the corpus a given reach exposes. None of them encrypt
  the payload at rest — that remains dependent on the deployment's storage
  layer (an encrypted StorageClass, `ARCHITECTURE.md`'s noted PyKMIP
  deferral) rather than anything this application does. Recorded here as an
  accepted, structural residual rather than a claimed fix.
- **Downstream of the store — the chat plane.** Retrieval's job ends at
  returning content to an authorized caller; what that caller's client does
  with it is a second boundary. LibreChat persists tool results and
  generated answers in its own Mongo store, and LiteLLM may log/spend-track
  the generation request depending on config — both outside this repo's
  enforcement (`roles-and-permissions.md` §6 states this plainly). Neither
  carries classification/releasability tagging or a retention policy this
  repo controls, and the purge path ([#123](https://github.com/schuecl/nexus-rag/issues/123))
  cannot reach either. **Decided in
  [#286](https://github.com/schuecl/nexus-rag/issues/286)** as accepted risk
  with an equipped response: the `document.purged` audit entry carries
  `chat_plane_action_required` and the retrievability window, the #73 SIEM
  export delivers it to chat-plane operators, and
  [`chat-plane-purge.md`](chat-plane-purge.md) is their sweep procedure.
  Retrieval-side minimization was considered and rejected (shrinks the copy,
  not the boundary).
- **Reranker boundary:** `reranker-service` receives already-authorized
  chunk text with no claims check of its own — a deliberate exception,
  documented in `governance.md`'s "The reranker-service boundary", bounded
  by a shared secret plus NetworkPolicy rather than a second FR-26
  enforcement point. Relevant here because it's one more place retrieved
  cleartext transits before reaching the caller.

### 3. Poisoning (content the curation gate didn't actually see)

*Can content that should have been caught by human review reach retrieval
anyway — not because FR-26 was bypassed, but because the human approving it
never saw what they were approving?*

This is a different failure mode from the first two — not information
disclosure, but *unreviewed influence* on what a retrieval-grounded answer
says. Included here because it's one of the three attack classes the
Arzanipour et al. taxonomy formalizes for RAG specifically, and because the
platform's primary anti-poisoning control (mandatory human curation,
FR-11/FR-12) has the same shape as the other two surfaces: a control that
looks complete until you check what it actually observes.

- **The gap, as found:** for most of this platform's history, no route
  showed a curator the document content they were approving — only
  filename, submitted tags, and (once #138 shipped) marking-mismatch
  advisory fragments. A curator could approve a document with a benign
  filename and clean tags but an adversarial body (hidden instructions
  aimed at the generation model, fabricated content) without ever reading
  it.
- **Control:** `GET /curate/{id}/content` now serves parsed chunk text back
  to a curator with the same authority checks as approve/reject — org
  (404), clearance/releasability (403), and `access_scope` (403) —
  implemented in [#284](https://github.com/schuecl/nexus-rag/issues/284).
  The `access_scope` check matters specifically because a content-viewing
  route is exactly where the G1 need-to-know gap (`roles-and-permissions.md`)
  would have mattered most; #284 and #277 (G1) close together.
- **Tamper-evidence:** no cryptographic link previously existed between the
  object-store original, the parsed text the worker chunks, and the Qdrant
  chunk payload — a document could be modified between approval and
  retrieval (store-side access, backup restore, a redelivered NATS job
  parsing different bytes than were approved) with nothing to detect it.
  SHA-256 digests computed at upload and re-verified before parsing, refusing
  on mismatch, close this — [#285](https://github.com/schuecl/nexus-rag/issues/285).
- **What remains a mitigation, not a guarantee:** the `<untrusted_document_content>`
  delimiter markers (`rag_search.py`'s module docstring, P1/REQUIREMENTS.md
  Section 11) reduce prompt-injection risk from retrieved text reaching the
  generation model, but a sufficiently adversarial document could still try
  to break out of the delimiter. This surface is about *human* review
  actually seeing content; it doesn't claim the generation model is immune
  to what it reads once reviewed.

### 4. Reconnaissance-shaped query patterns (detection, not prevention)

*Section 1 above is about closing the membership-inference channel itself
(score suppression, per-classification collections). This is the
complementary control OWASP and #127 gap #4 both ask for: since the channel
can't be fully closed for an A-I/A-II adversary (rank order is a residual
signal, see "Considered and rejected" below), can an operator at least detect
that someone is probing it?*

[#426](https://github.com/schuecl/nexus-rag/issues/426) implements this as
`scripts/detect_query_anomalies.py`, an offline batch job over the audit log
— not a Prometheus per-identity label. That deliberately-rejected design is
worth stating explicitly: `orchestration-mcp/app/metrics.py`'s own docstring
already argues a per-user metric label "would rebuild exactly the
surveillance surface #125 removed from the audit log," and #426's own
suggested direction flagged the same cardinality risk. The script instead
authenticates as the same `nexus_rag_audit_reporting` SELECT-only role
`calibrate_tagging_advisory.py` (#309) uses, computes four signals per
identity over a lookback window —

- **high_volume**: raw attempt-rate spike (naive scripted probing);
- **high_denial_ratio**: a sustained personal denial rate, distinct from
  `NexusRagQueryDeniedSpike`'s global volume threshold, which a slow,
  methodical single-identity prober can stay under;
- **narrow_probe_shaped**: a high share of successful queries resolving to 0
  or 1 chunks — the substitute for #426's literal "near-duplicate query
  text" suggestion, since #125 means there is no query text to diff;
  `result_count` carries the same probing shape without needing content;
- **boundary_mapping**: repeated denial-then-success sequences within a
  short window — narrower than it sounds, and narrower than #426's own
  suggested framing: `rag_search.py`'s only `query.denied` path is the
  coarse missing-`rag-query`-role gate, not a per-query FR-26 mismatch (an
  out-of-scope query returns a *successful* empty result, never a denial —
  confirmed live, see `detect_query_anomalies.py`'s module docstring), so
  this actually detects an identity's `rag-query` grant changing state
  mid-window and being used immediately after, not filter-boundary probing —

and publishes only a **count of flagged identities per signal** (plus a
staleness timestamp) to Prometheus via Pushgateway, gated by two new alert
rules (`NexusRagQueryAnomalyDetected`, `NexusRagQueryAnomalyDetectionStale`).
Actual `actor_sub`/`actor_username` attribution exists only in the script's
own stdout report — the same audience `docs/governance.md`'s "Query
confidentiality and user privacy" already names as able to read `audit_log`.

**Honest confidence label:** validated against a live environment. The
aggregation/exposition logic is unit tested against constructed audit rows
(`tests/unit/test_detect_query_anomalies.py`); the full path — real
`docker compose up`, real `bob-query`/`alice-ingest` tokens driving real
`/debug/rag_search` calls, the compose job reading real `audit_log` rows
through `nexus_rag_audit_reporting`, a real Pushgateway push, and a real
Prometheus alert firing on exactly the flagged signals — was exercised end
to end (`docs/dev-setup.md`'s "What's stubbed vs working"). That run is also
what caught `boundary_mapping`'s original "filter-boundary mapping"
description as wrong (see above) before merge. **Deliberately not built
here:**
detection rules inside the environment's actual SIEM (Splunk/Elastic/
ArcSight/QRadar query languages are environment-specific and outside this
repo's testable surface — the audit rows already reach a SIEM via #73's
export, so a deployment can build the equivalent there); and automated
scheduling of the batch job itself (it shares this gap with
`calibrate_tagging_advisory.py` — both are "run manually or on a schedule"
with nothing in this repo that schedules them).

[#436](https://github.com/schuecl/nexus-rag/issues/436) closes the first of
those two as far as it can be closed here — as *documentation*, not code:
[docs/siem-detection-runbook.md](siem-detection-runbook.md) gives the four
signal definitions, the RFC 5424 message shape a rule has to parse, and
adaptable Splunk SPL / Elastic ES|QL and EQL sketches, so an operator building
detection in their own SIEM works from a runbook instead of reverse-engineering
`detect_query_anomalies.py`. The rules themselves remain untested by this
repo's CI for the reason above, which is exactly why they ship as sketches to
adapt rather than as artifacts to copy.

## OWASP RAG Security Cheat Sheet — control-by-control

Recorded because the controls that are already right are the expensive ones,
and because a checklist read makes it easy to see what's covered vs. what
each attack-surface section above is actually answering.

| OWASP control | Status | Where |
|---|---|---|
| Pre-retrieval filtering, not post-retrieval | **Correct** | FR-26 filter is inside both `Prefetch` legs — out-of-scope chunks are never scored, not filtered after scoring |
| Never expose the vector store without authentication | **Correct** | NFR-15; `orchestration-mcp` holds a read-only key |
| Log queries with the querying identity | **Correct, content-minimized** | FR-31, plus #125/#128: identity/outcome/filter-shape/result-count logged, query text is not |
| Never return similarity scores | **Correct** | #127/#128, see membership-inference section above |
| Separate vector namespaces per classification/tenant | **Correct** | #229/#267, see membership-inference section above |
| Treat embeddings/payloads with the same controls as source documents | **Bounded, not eliminated** | see retrieved-content-leakage section — collection split + NetworkPolicy + RO/RW keys narrow reach; payload is not encrypted at rest |
| No third-party prompt egress | **Correct by architecture** | Generation is self-hosted Ollama, not a vendor API |
| Monitor for systematic/reconnaissance-shaped queries | **Validated against a live environment** | #426: `scripts/detect_query_anomalies.py` mines FR-31's audit trail for per-identity query-rate, denial-ratio, narrow-result-probing, and denial-then-success signals, with a content-free/bounded Prometheus alert on top, exercised end to end against a real stack — see "Reconnaissance-shaped query detection" below |
| Document integrity / tamper-evidence | **Correct** | #285, see poisoning section above |
| Human review of content before it becomes retrievable | **Correct** | FR-11/FR-12 gate, and #284 makes it actually see content, not just metadata |

## Considered and rejected: differentially-private retrieval

Arzanipour et al.'s proposed mitigation for document-level membership
inference is a differentially-private retriever — Laplace noise added to
relevance scores before top-k selection, so an adversary's score-based
distinguisher is bounded by a formal privacy budget.

**Rejected for this platform, and recorded as a deliberate no rather than an
omission:**

- **Recall is a hard requirement here, not a tunable one.** The golden-query
  harness fails CI on any recall miss (FR-26/FR-30,
  `scripts/evaluate_retrieval.py`). DP noise trades exact top-k for a
  privacy guarantee by construction — that is precisely the trade this
  system's CI gate exists to refuse.
- **The adversary the noise defends against is already excluded by
  architecture.** DP retrieval protects against an *unauthorized* querier
  trying to distinguish corpora via the score channel. That adversary is
  already fully denied here: the FR-26 filter is deterministic, mandatory,
  and fails closed — an unauthorized caller gets zero candidates from the
  restricted document regardless of query, no score gradient to add noise
  to in the first place.
- **The adversary noise would actually need to address — an *authorized*
  caller inferring within their own clearance — is answered more directly
  and at no recall cost** by the score-suppression already done (#127/#128,
  above). Noise on a score the caller never sees adds nothing; noise on rank
  order (the one thing the caller does see) would degrade the answers every
  authorized query returns, for a marginal reduction in an already-narrow
  residual channel (rank-order membership inference, noted in the
  membership-inference section as unmitigated and accepted).

Net: DP retrieval is the right answer for a system whose access boundary is
soft (probabilistic ranking is the only gate). It's the wrong answer for a
system whose access boundary is a hard, deterministic filter and whose
remaining privacy question is what a caller can do *inside* that filter —
which score-suppression already answers without touching recall.

## Open / not yet resolved

| Item | Status |
|---|---|
| Retrieved content persisting in the chat plane beyond purge's reach | Decided ([#286](https://github.com/schuecl/nexus-rag/issues/286)): accepted risk; purge event signals it ([`chat-plane-purge.md`](chat-plane-purge.md)) |
| Qdrant payload cleartext not encrypted at rest | Bounded by collection split/NetworkPolicy/RO-RW keys, not eliminated; depends on deployment storage layer |
| Rank-order residual membership-inference channel | Accepted, unmitigated — see "Considered and rejected" above |
| SIEM-side detection rules for the patterns #426 flags | Not built here — environment-specific SIEM query languages are outside this repo's testable surface; the flagged-event data already reaches a SIEM via #73's export |
| Automated scheduling of the offline audit-reporting jobs (calibration, #426's detector) | Both are "run manually or on a schedule" per their own docs; nothing in this repo schedules either one |

## Related

[`governance.md`](governance.md) (lifecycle/retention, and the
"Query confidentiality and user privacy" section this document supersedes
the "not done" framing of), [`roles-and-permissions.md`](roles-and-permissions.md)
(authorization matrix and its own gap analysis, G1–G7),
[`ARCHITECTURE.md`](../ARCHITECTURE.md) (component/sequence diagrams),
[#127](https://github.com/schuecl/nexus-rag/issues/127) (the issue this
document answers), [#286](https://github.com/schuecl/nexus-rag/issues/286)
(chat-plane persistence — decided, see
[`chat-plane-purge.md`](chat-plane-purge.md)), [#426](https://github.com/schuecl/nexus-rag/issues/426)
(reconnaissance-shaped query detection, closing #127 gap #4 — see section 4 above),
[`docs/observability.md`](observability.md) (the alert rules #426 adds).
