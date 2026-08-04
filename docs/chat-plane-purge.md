# Chat-plane purge runbook (issue #286)

When a document is purged here, every copy **this system** holds is destroyed
(`common/purge.py`, #123): Qdrant chunks, the object-store original, the
Postgres row (tombstoned). But every chat conversation that ever retrieved the
document still holds its text verbatim in stores this repo cannot reach:

- **LibreChat** persists `rag_search` tool results and the model's grounded
  answers in its own Mongo store, keyed by LibreChat's user/session model,
  with no classification tagging and no retention policy this repo controls.
- **LiteLLM** may log or spend-track the generation request — whether prompt
  text (which contains chunk text) lands in its DB depends on its own config.

This runbook is the chat-plane half of a spillage remediation. It is executed
by whoever operates LibreChat/LiteLLM (MPNexus platform operations), not by
this system — this repo's part ends at emitting the trigger event and the
scoping data below.

**Status honesty:** the trigger event and the audit queries in step 2 are
*tested against mocks* (`test_purge.py::TestChatPlaneSignal`, the FR-31 audit
schema). The LibreChat/LiteLLM commands in steps 3–4 are *illustrative and
unvalidated* — schema names vary by version and deployment; verify them
against the deployed instances before relying on them. That caveat is the
reason this is a runbook and not automation.

## The decision this document records

Issue #286 laid out four options. Adopted: **1 + 2 + 3 together** —

1. **Accepted risk, stated plainly** (`docs/governance.md` "Retention and
   destruction", `docs/roles-and-permissions.md` G7): chat-plane copies are
   beyond purge's reach; a spillage response is incomplete until the chat
   plane is swept by that platform's own means.
2. **This runbook** — the manual procedure.
3. **Purge-event signal** — the `document.purged` audit entry now carries
   `chat_plane_action_required` and `retrievable_since`, and the NFR-2 SIEM
   export (#73, `common/siem.py`) forwards it off-box like every other audit
   event. Chat-plane operators alert on it; no new plumbing was built.

Rejected: **option 4, retrieval-side minimization** (truncating chunk text /
returning citations-plus-excerpts). It shrinks but does not close the
exposure — any retrievable text can persist in a conversation — at a direct
cost to answer quality, and it would dilute the honest statement that the
boundary exists. Recorded as considered.

## 1. Trigger

A purge emits a `document.purged` audit entry (forwarded to the SIEM as
RFC 5424 syslog, MSGID `document.purged`) whose `detail` includes:

| Field | Meaning |
|---|---|
| `chat_plane_action_required` | `true` if the document's chunks were ever retrievable. Computed from **two** signals: the pre-purge status (`approved`/`superseded`, or a conservative `purging` retry) OR a non-null `first_approved_at` — the second is what catches a document that was approved, then demoted back to `pending_review` (or on to `rejected`) by an out-of-authority tag edit before being purged. `false` means no query can have returned it — **stop here**, no sweep needed. |
| `retrievable_since` | The **first** approval timestamp (`first_approved_at`, which survives demotion and re-approval — the earliest exposure), falling back to `reviewed_at` for rows approved before that column existed. `null` only when neither exists; fall back to the tombstoned row's `created_at`. |
| (entry `created_at`) | End of the window: nothing was retrievable after the purge. |

**Known residual:** a document approved *before* `first_approved_at` existed
and then demoted before purge has a null column and a non-retrievable
status — the flag reads `false` for it. This narrows over time (every
approval since the column ships sets it) and is stated here rather than
silently absorbed.

The sweep window is `[retrievable_since, purge time]`.

## 2. Enumerate the exposure (this system's side)

Contrary to #286's original assumption, contaminated queries **are**
enumerable: every successful `query` audit row records
`result_document_ids` (G6 as corrected by #364). Using the
`nexus_rag_audit_reporting` role (read-only on `audit_log`, #309):

```sql
-- Every query that returned the purged document: who, and when.
SELECT actor_sub, actor_username, created_at
FROM audit_log
WHERE action = 'query'
  AND detail->'result_document_ids' @> to_jsonb(:purged_document_id::text)
ORDER BY created_at;
```

This yields the exact set of OIDC identities and timestamps whose
conversations may hold the text. If the audit rows for the window have been
expired (retention), fall back to sweeping all conversations in the window.

## 3. LibreChat sweep

For each `actor_sub`/timestamp pair from step 2, in LibreChat's Mongo
(*illustrative — verify collection/field names against the deployed
version*):

```javascript
// Find the LibreChat user for the OIDC subject, then their conversations
// updated in the window.
const user = db.users.findOne({ openidId: "<actor_sub>" });
db.conversations.find({
  user: user._id,
  updatedAt: { $gte: ISODate("<retrievable_since>"),
               $lte: ISODate("<purge_time>") } });
```

Per affected conversation, the deployment's policy decides: delete the
conversation (`db.conversations.deleteOne` + matching `db.messages` rows), or
excise the affected messages. Deleting only messages that quote the document
requires reading them — treat the conversation as spilled and act at
conversation granularity unless policy says otherwise.

## 4. LiteLLM sweep

Check the deployment's LiteLLM config first: if prompt logging is off
(`store_prompts_in_spend_logs: false`, the default for spend logs), there is
no chunk text in LiteLLM's DB and this step is a recorded no-op. If any
logging callback or `store_model_in_db`-style prompt persistence is on, purge
rows in the window from the relevant tables (`LiteLLM_SpendLogs` and any
configured logging backend) by date range.

## 5. Record completion

The chat-plane sweep happens outside this system's audit trail by
definition. Record it in the platform's own change/incident log, referencing
the `document.purged` audit entry id (the tombstoned document id is in its
`target_id`) so the two halves of the remediation are joinable later. Do not
copy the document's name or content into that record — the same
audit-content-minimization rule `governance.md` applies here.
