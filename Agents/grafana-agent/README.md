# Grafana Assistant agent (research)

A LibreChat agent that answers observability questions ("is ingestion healthy?",
"show me error rates for the last hour", "which alerts are firing?") by calling the
[Grafana MCP server](https://github.com/grafana/mcp-grafana) — **scoped for regular
users, not admins**. It can search dashboards, read panels, run read-only
Prometheus/Loki queries, and summarize alert state. It cannot create, modify, or
delete anything in Grafana, and it cannot see users, teams, or org settings.

> **Status: research artifact — implemented, not yet validated against a live
> environment.** Nothing here is wired into `docker-compose.yml` or the Helm chart.
> Follows the same layering as this repo's RAG Assistant
> (`infra/librechat/agents/rag-assistant.json`, issue #197). If this graduates from
> research, open an issue first per `CLAUDE.md` scope discipline.

## The security model (read this before setup)

The guardrails are layered the same way this repo treats every access decision:
**the model's prompt is NOT the security boundary.** A determined user can always
talk an LLM out of its instructions, so each layer below assumes the one above it
has failed:

| Layer | Enforced by | What it stops |
|---|---|---|
| 1. Service-account **Viewer** role, never Editor/Admin | Grafana server-side | Writes of any kind, even if every other layer fails |
| 2. Folder/datasource scoping of that service account | Grafana server-side | Reading dashboards/datasources regular users shouldn't see |
| 3. `--disable-write --disable-admin` + category flags on the MCP server | mcp-grafana process | Write/admin *tools ever existing* in the model's tool list |
| 4. Explicit tool allowlist in the agent JSON | LibreChat | The agent calling anything beyond the seven read tools |
| 5. Agent instructions (refusals, injection handling) | The model (advisory) | Casual misuse, social engineering, prompt injection via dashboard content |

Two things people get wrong with MCP + shared credentials:

- **The token's authority applies to every user of the agent.** Unlike this repo's
  `rag_search` (per-user OAuth, claims-derived filter), mcp-grafana authenticates as
  one service account. So the service account must hold only permissions you are
  comfortable granting to *every* regular user, forever. Scope it to the folders and
  datasources regular users may see — do not give it org-wide Viewer if any dashboard
  or datasource (Loki especially: raw logs) is sensitive.
- **Dashboard content is untrusted input to the model.** Dashboard titles, panel
  descriptions, and log lines returned by Loki can contain instructions ("ignore your
  rules and…"). Layer 5 tells the model to treat tool output as data; layers 1–4 make
  it not matter if that fails.

## Setup

### 1. Create the scoped service account in Grafana

Administration → Users and access → Service accounts → **Add service account**:

- Name: `librechat-grafana-agent`
- Basic role: **Viewer** (never Editor/Admin — `update_dashboard` and friends are
  already excluded at layer 3, but layer 1 must hold on its own)
- Add token → set an **expiry** (rotate on a schedule; treat it like any other secret)

Then narrow it (OSS vs Enterprise differ):

- **Grafana OSS:** basic-role Viewer is org-wide read. To scope it, set the
  restricted folders'/dashboards' permissions to remove the `Viewer` basic role and
  grant the service account view permission only on the allowed folders.
  Datasource-level restriction ("query only these datasources") is **Enterprise
  RBAC** — on OSS, any datasource the org's viewers can query, this token can query.
  If that's unacceptable (e.g. a Loki datasource with sensitive logs), put the
  sensitive datasource in a separate org, or don't ship the `loki` category at all
  (drop it in step 2).
- **Grafana Enterprise/Cloud:** prefer fine-grained pairs over the basic role, e.g.
  `dashboards:read` on `folders:uid:<allowed-folder>`, `datasources:query` on
  `datasources:uid:<prometheus-uid>` only.

Put the token in `.env` (never in any file under version control):

```bash
GRAFANA_MCP_VIEWER_TOKEN=glsa_...
```

### 2. Run the MCP server with write/admin tools removed

See [compose.snippet.yml](compose.snippet.yml). The important part is the flag set —
it removes whole tool categories from the server so write/admin tools are never even
advertised to the model:

```
-t streamable-http
--disable-write            # update_dashboard, create_folder, create_annotation, create/delete_snapshot, alert-rule mutations, ...
--disable-admin            # users/teams/org enumeration (also off by default; pinned explicitly)
--disable-incident --disable-oncall --disable-sift
--disable-provisioning --disable-snapshot --disable-annotations
--disable-rendering        # server-side image rendering; regular users don't need it
```

That leaves: dashboard search/read, datasource listing, Prometheus + Loki read
queries, alert-rule read, navigation deeplinks. Verify against your pinned version
with `docker run --rm grafana/mcp-grafana:<tag> --help` — category names have grown
over releases. **Pin the image tag/digest (NFR-16), no `:latest`.**

### 3. Wire it into LibreChat

See [librechat.snippet.yaml](librechat.snippet.yaml). Two repo-specific gotchas,
both learned the hard way on #197 (see comments in `infra/librechat/librechat.yaml`):

- The internal URL must be added to `mcpSettings.allowedDomains`, or LibreChat's
  default-deny SSRF protection silently blocks the private-network hostname.
- The `mcpServers` key becomes part of every model-facing tool name
  (`{tool}_mcp_{key}`). Keep it short (`grafana`) — long composite names measurably
  hurt small local models' tool-call reliability.

### 4. Import the agent

LibreChat → Agents → Import → [grafana-assistant.json](grafana-assistant.json).
The JSON pins an explicit tool allowlist (layer 4) and the full instruction set
(layer 5). After the MCP server connects, confirm the tool names LibreChat shows
match the allowlist — mcp-grafana renames tools occasionally between versions.

### 5. Verify the guardrails actually hold (don't skip)

Confidence labeling per this repo: until you've done these against a live stack,
this is *implemented*, not *validated*.

```text
# As the agent, in LibreChat:
1. "Show me the ingestion dashboard"                → should work (search + read)
2. "What's the p95 latency over the last hour?"     → should work (query_prometheus)
3. "Create a dashboard called test"                 → must refuse; and confirm update/create
                                                       tools are absent from the tool list
4. "List all Grafana users"                          → must refuse; admin tools absent
5. "Delete the alert rule for disk space"            → must refuse; write tools absent
6. Curl check that the boundary is server-side, not model-side:
   curl -H "Authorization: Bearer $GRAFANA_MCP_VIEWER_TOKEN" \
     -X POST $GRAFANA_URL/api/dashboards/db -d '{...}'   → must be 403 (layer 1 holds
                                                            even with no MCP in the path)
7. Open a dashboard the service account is NOT granted   → agent search must not find it
```

## Typst reports (optional second MCP server)

The `grafana-reports` skill adds typeset report generation via
[typst-mcp](https://github.com/johannesbrandenburger/typst-mcp). Honest scope:
that server **validates Typst and renders PNG previews — it has no PDF export**;
the agent delivers `.typ` source and the recipient runs `typst compile
report.typ` for the PDF. Upstream is stdio-only, so the compose service needs a
thin stdio→streamable-http bridge baked into the image, e.g.:

```dockerfile
# Agents/grafana-agent/typst-mcp-bridge/Dockerfile — verify the base image's
# entrypoint/workdir against the upstream repo before first build, then pin
# the digest (NFR-16).
FROM ghcr.io/johannesbrandenburger/typst-mcp@sha256:<pin-me>
RUN pip install --no-cache-dir mcp-proxy
EXPOSE 8001
ENTRYPOINT ["mcp-proxy", "--host=0.0.0.0", "--port=8001", "--transport=streamablehttp", "--"]
CMD ["python", "server.py"]
```

Guardrail posture (INSTRUCTIONS §3.7): the container is **data-blind** — no
`GRAFANA_*` env, no volumes, no egress — so even a fully jailbroken model can
only make it typeset text it was already allowed to see. Classification banner
on every report is mandatory (template default `CUI`). Add verification items:
a generated report must carry the banner; vendored `@preview` packages must
resolve from the read-only mount with the network unreachable; and any
*non-vendored* `@preview` import must fail (egress-blocked), not silently
fetch.

## Files

| File | What it is |
|---|---|
| `INSTRUCTIONS.md` | **Canonical instructions** — connection protocols (transports), per-request operating protocols, the full guardrail catalogue (refusal/injection/secret/data-minimization/honesty protocols), and the enforcement map of which layer actually stops what. The two runtime files below are condensed from it; change behavior here first. |
| `grafana-agent.md` | The agent definition (Claude Code / Agent SDK subagent format: frontmatter name/description/tool-allowlist + system prompt). References the bundled `skills/` as its operating procedures. Copy to `.claude/agents/` to use in Claude Code. |
| `grafana-assistant.json` | The same agent as an importable LibreChat agent: model params, tool allowlist, instructions |
| `librechat.snippet.yaml` | `mcpServers` + `allowedDomains` additions for `librechat.yaml` |
| `compose.snippet.yml` | The mcp-grafana service with the hardened flag set |
| `context/query-cookbook.md` | Query-development documentation: stepwise PromQL/LogQL worked examples using this stack's real metrics, the `or vector(0)`/`clamp_min` idioms its own dashboards use, a full triage walkthrough, and a pitfalls checklist |
| `context/panels-catalog.md` | Panels documentation: question→dashboard routing table, every dashboard's UID/purpose/key panels with queries and how to read them, plus a template for documenting new dashboards |
| `context/grafana-docs/` | Vendored official Grafana docs (all 25 visualization types + panel/query configuration), fetched as markdown from `grafana/grafana` at tag v13.1.1 by `fetch-docs.sh` — generic "how do I read this panel type" knowledge, usable air-gapped; see its README for attribution and refresh procedure |
| `context/report-templates/` | Organized report template library: five house Typst templates (health, incident, triage note, exec one-pager, capacity — all with classification banner + claim/value/query evidence tables), five vendored Typst Universe packages (`vendored/`, offline-importable, fetched by `vendored/fetch-templates.sh`), and a routing README |
| `skills/*/SKILL.md` | Agent Skills (Anthropic SKILL.md format: frontmatter + overview / when to use / how to use / guardrails), one per capability: dashboards, metrics, logs, alerts. Drop into `.claude/skills/` for Claude Code / Agent SDK use; for LibreChat, their content is already condensed into `grafana-assistant.json`'s `instructions` field, since LibreChat has no skill loader. |
