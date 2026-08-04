# Querying the corpus: three ways, and the lessons behind them

There are three ways to ask questions against the approved documents, in
increasing order of moving parts (and of things that can go wrong). Retrieval
and access control are identical in all three — the differences are entirely in
the generation/chat layer in front of them.

| Way | What it exercises | Use it when |
|-----|-------------------|-------------|
| **1. MCP tool directly** (`/debug/rag_search`) | Retrieval + per-user access filter + reranking | Verifying the security-critical path; no chat model needed |
| **2. LibreChat Agent** (`RAG Assistant`) | The full chat UX with tool execution | The intended end-user experience |
| **3. Plain model chat** | A model with the tool toggled on | Debugging the generation hop; not recommended for the demo |

The retrieval plane (1) is solid and is what matters for the access-control
invariant. Ways 2 and 3 add a small local model whose tool-calling is the
fragile part — most of the lessons below are about making that work.

---

## 1. The MCP tool directly (`/debug/rag_search`)

`orchestration-mcp` exposes the exact retrieval logic as a plain REST endpoint
for curl testing, with no MCP client or chat model involved. It enforces the
same claims-based filter as the MCP tool.

```bash
# Get a token for the querying user (rag-app client, direct grant):
KC=http://localhost:8080
TOKEN=$(curl -s -X POST "$KC/realms/nexus-rag/protocol/openid-connect/token" \
  -d client_id=rag-app -d client_secret=dev-rag-app-secret \
  -d username=bob-query -d password=devpass123 -d grant_type=password -d scope=openid \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Query (top_k optional):
curl -s -X POST "http://localhost:8002/debug/rag_search?query=password%20policy&top_k=3" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

The response includes `applied_filter` (the access filter built from the user's
claims), a `security_notice`, and `results[].payload` with the **tags on every
chunk**: `classification`, `releasability`, `access_scope`, `status`, plus
`filename`, `doc_type`, `heading`, `text` (wrapped in
`<untrusted_document_content>`). This is the ground truth: change the `username`
above and the same query returns different documents.

## 2. The LibreChat Agent (`RAG Assistant`) — the intended UX

An **Agent** is required for reliable MCP tool execution in the chat UI (a plain
custom-endpoint chat advertises the tool but doesn't run LibreChat's tool loop).
The agent definition is checked in at
[`infra/librechat/agents/rag-assistant.json`](../infra/librechat/agents/rag-assistant.json);
create it once per LibreChat instance:

```bash
# The target user must have logged in via Keycloak at least once first.
scripts/create_librechat_agent.sh dave-admin
```

Then in LibreChat: switch the endpoint selector to **Agents**, pick **RAG
Assistant**, and the **first time** click **Connect** on the `rag` tool to do the
per-user Keycloak login. Ask e.g. *"What is our password policy?"* — it calls
`rag_search`, retrieves the approved passage, and answers with a
`[password-policy.md, CUI]` citation.

Two things the agent definition encodes, both of which turned out to be
load-bearing:

- **`instructions`** that force tool use ("you MUST call rag_search ... never
  answer from your own knowledge"). Small models under-call the tool on bare
  `tool_choice=auto` and will otherwise answer from parametric (hallucinated)
  knowledge.
- **`model_parameters.disableStreaming: true`** — see lesson C below.

## 3. Plain model chat (not recommended)

Selecting the LiteLLM endpoint and a model directly, with the `rag` tool toggled
on, *can* work but is the least reliable: it depends on the endpoint-level
`disableStreaming` (in `librechat.yaml`) and small models are inconsistent about
emitting a parseable tool call. Prefer the Agent.

---

## Lessons learned (in the order they bite)

### A. `LITELLM_MASTER_KEY` must reach the *librechat* container (#193)
`librechat.yaml` sets the LiteLLM endpoint's `apiKey: "${LITELLM_MASTER_KEY}"`,
and LibreChat only substitutes that from its **own** container environment. If
the `librechat` service doesn't pass the var, the key is empty and chat fails
with **"Missing API Key for LiteLLM"** — which reads like, but has nothing to do
with, the user's Keycloak login. Fixed in the compose `librechat` service.

### B. Model size vs. tool-calling (#195)
Measured live against `rag_search`'s real schema:

| model | behaviour |
|-------|-----------|
| llama3.2:1b, qwen2.5:0.5b/1.5b | hallucinate the tool **name** (e.g. `get_password_policy`) instead of calling `rag_search` |
| **qwen2.5:3b-instruct** | smallest that calls `rag_search` correctly (reliable when nudged by the agent instructions) |
| qwen2.5:7b-instruct | most reliable on bare `auto`, but ~5 GB and slow on CPU |

Dev defaults to 3b; the 7b block is commented in `infra/litellm/config.yaml`.
Symptom of a too-small model: raw JSON like `{"name": "get_password_policy", ...}`
printed as the answer.

### C. Ollama 0.32.1 does not emit structured tool calls when **streaming**
This is the subtle one. Non-streaming requests return a proper
`tool_calls` object; **streaming** requests stream the raw `{"name":...}` as
plain `content` (confirmed at both Ollama's `/v1` and native `/api/chat`). Since
LibreChat streams by default, the tool call arrives as text and never executes —
you see the JSON printed in chat even though the model did the right thing.

Fix: disable streaming so the structured tool call survives. **Where** you set it
matters:
- **Plain chat** honours `disableStreaming: true` on the custom endpoint in
  `librechat.yaml`.
- **Agents** ignore the endpoint flag and read it from the *agent's*
  `model_parameters.disableStreaming` (see
  `api/server/controllers/agents/openai.js`) — which is why the checked-in agent
  definition sets it there.

Trade-off: replies appear when complete instead of token-by-token. Acceptable for
this dev path, and it is what makes the tool actually run. (The real long-term
fix is an Ollama/LiteLLM version that streams tool calls; out of scope here.)

### D. Per-user OAuth for the `rag` tool, and token expiry
The `rag` MCP server is configured for **per-user OAuth** (`requiresOAuth: true`),
not a shared key: each user clicks **Connect** once and logs into Keycloak, so
every `rag_search` runs under *their* claims. The stored access token is
short-lived (realm `accessTokenLifespan`, 900 s in dev). When it lapses you'll
see:

> `{"error": "invalid token: Signature has expired"}`

from `orchestration-mcp` (its `parse_claims` correctly rejects an expired JWT).
Fix: **disconnect and reconnect the `rag` tool** to mint a fresh token. For a
smoother dev session you can raise `accessTokenLifespan` in the realm export, at
the usual convenience-vs-security trade-off — left at the secure default here.

### E. Creating agents via the API
Agents are per-author and created through LibreChat's authenticated REST API
(no file import in v0.8.7). `scripts/create_librechat_agent.sh` mints a session
JWT (signed with the container's `JWT_SECRET`) for an existing user and POSTs
[`rag-assistant.json`](../infra/librechat/agents/rag-assistant.json). Gotcha:
LibreChat's `uaParser` middleware rejects any request without a browser
`User-Agent` with **"Illegal request"** — the script sends one. Second gotcha,
found live (issue #97): the script looked up the author's Mongo `_id` with a
bare `print(...)`, and on this mongosh version that prints `ObjectId('...')`
rather than the hex string alone — the whole wrapped string then got minted
straight into the JWT's `id` claim, and LibreChat's user lookup failed with an
opaque `Cast to ObjectId` 500. Fixed by calling `.toString()` explicitly on
the `_id` rather than relying on `print`'s default formatting.

### F. Scripting the full round-trip without a browser (issue #97)
Everything in section 2 above — including the Keycloak login and the `rag`
tool's per-user OAuth "Connect" — is scriptable with `httpx`, not just
`create_librechat_agent.sh`'s session-JWT trick. Confirmed live (2026-07-28):
`scripts/adversarial_injection_probe.py` does a real Keycloak password-grant
login following the same redirect chain a browser takes, reuses the resulting
Keycloak SSO cookie to complete the `rag` tool's OAuth authorization redirect
non-interactively, then drives LibreChat's resumable chat API
(`POST /api/agents/chat` → poll `GET /api/agents/chat/status/:streamId` →
`GET /api/messages/:conversationId` once inactive) to send a real message and
read the model's real answer. Useful beyond issue #97 for the still-open
"regression test that the generation model actually invokes `rag_search`"
item (`REQUIREMENTS.md` Section 11 P1 list).

Two timing gotchas this ran into, both from things with a TTL shorter than a
CPU-bound `qwen2.5:7b-instruct` generation can take:
- A minted session JWT is only valid 10 minutes (`_JWT_TTL_SECONDS`) — mint a
  fresh one before each query in a multi-query run, not once up front.
- The `rag` tool's Keycloak-issued access token is the 900s one from lesson D
  above. LibreChat does **not** auto-detect "loaded but expired" the way it
  detects "missing" — it loads the stale token, sends it, `orchestration-mcp`
  rejects it, and the agent retries the *same* failing tool call until
  LangGraph's recursion limit (50) aborts the whole message, all without ever
  showing an OAuth prompt. The script works around this by deleting the
  stored token from LibreChat's Mongo `tokens` collection before every
  connect attempt, forcing a real "missing token" state rather than trying to
  infer whether a reconnect is needed.

Not wired into CI — real generation on CPU takes minutes per query, same
reason the golden-query e2e job (`e2e.yml`) points `GENERATION_MODEL` at the
embedding model instead of a real one. Manual/nightly-only, like the
mutation-testing job (though that job now enforces its own ≥80% kill-rate
gate as of issue #78 -- this one has no pass/fail gate at all).
