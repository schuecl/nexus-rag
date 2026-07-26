# Security Policy

Nexus RAG is a RAG pipeline for classification/releasability-marked
documents: OIDC-authenticated ingestion and curation, and claims-based
access-controlled retrieval enforced server-side before anything reaches
the vector store. It maintains an append-only audit trail and is often
deployed in regulated or air-gapped DoD environments. We take security
reports seriously.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub
issues, discussions, or pull requests** — that discloses the issue before
a fix is available.

**Preferred — GitHub private vulnerability reporting** (when enabled on
this repository):

1. Go to the **Security** tab of this repository.
2. Click **"Report a vulnerability"** to open a private security advisory.
3. Include as much detail as you can — affected version/commit, a
   description of the issue, reproduction steps or a proof of concept, and
   the potential impact.

**If that option is not available**, contact a repository maintainer
privately (for example, through their GitHub profile) to arrange a secure
channel before sharing any details — please still avoid posting anything
about the vulnerability publicly.

We will acknowledge your report as soon as we reasonably can, keep you
informed of remediation progress, and credit you in the advisory once a
fix is released (unless you prefer to remain anonymous).

Please give us a reasonable opportunity to release a fix before any
public disclosure.

## Scope

Security-relevant areas include, but are not limited to:

- **OIDC authentication and claims parsing** (`services/common/common/claims.py`) — how
  `clearance`, `releasability`, `groups`, `org`, and role claims are verified and turned
  into enforcement decisions, and the browser session/CSRF flow
  (`services/ingestion-api/app/routes/auth.py`, double-submit cookie protection on
  state-changing routes).
- **The mandatory retrieval access filter** (`services/common/common/qdrant_filters.py`) —
  the server-side, non-bypassable Qdrant payload filter (classification ceiling,
  releasability match, access-scope match, approval status) built from verified claims,
  never from anything client-supplied.
- **Curator-authority scoping** (`services/ingestion-api/app/routes/curate.py`) — org-scoped
  curate roles, and capping approval authority by the curator's own clearance and
  releasability holdings, not just role membership.
- **Credential separation** — the Qdrant read/write vs. read-only API key split between
  services, separate non-superuser database roles for the application and Keycloak, and
  object-store credentials kept independent of both.
- **The append-only audit log** — the application's database role holds only
  `SELECT`/`INSERT` on the audit table; row mutation or deletion requires the bootstrap
  superuser, never day-to-day application traffic.
- **The ingestion pipeline's handling of untrusted uploaded documents** — parsing of
  attacker-supplied files (`services/ingestion-worker/app/parsing.py`, including malformed/
  zip-bomb-shaped input) and the prompt-injection delimiting of retrieved document content
  before it reaches a generation model (`services/orchestration-mcp/app/rag_search.py`).

## Supported Versions

This project is pre-1.0 and evolving quickly. Security fixes are applied
to the latest `main`; please verify a report against the most recent
commit before submitting.
