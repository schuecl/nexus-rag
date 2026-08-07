# Nexus RAG

Air-gapped Retrieval-Augmented Generation for **MPNexus**: document ingestion
with mandatory classification/releasability tagging, human curation as the
gate to retrievability, and claims-filtered hybrid retrieval exposed to
LibreChat as an MCP tool. Every access decision — what a user may tag, what a
curator may approve, what a query may return — derives server-side from
verified OIDC claims, never from client input; nothing is retrievable until a
curator approves it.

The repository's markdown is the source of truth — this site is a rendered,
searchable view of the same files.

<div class="grid cards" markdown>

-   :rocket: **Get Started**

    ---

    One command to a running stack with a seeded, curated corpus — then your
    first claims-filtered query, in about fifteen minutes.

    [:octicons-arrow-right-24: Quickstart](guides/quickstart.md)

-   :books: **Understand it**

    ---

    Three planes, one security idea: a guided tour of the architecture, the
    document lifecycle, and what actually happens on a query.

    [:octicons-arrow-right-24: Architecture tour](guides/understanding-the-architecture.md)

-   :ship: **Deploy it — production**

    ---

    Production deployment three ways: single-host with Compose, Kubernetes
    with Helm, or across the air gap with the verified release bundle.

    [:octicons-arrow-right-24: Compose](guides/deploy-compose.md) ·
    [:octicons-arrow-right-24: Helm](guides/deploy-helm.md) ·
    [:octicons-arrow-right-24: Air-gapped](guides/deploy-airgapped.md)

-   :chart_with_upwards_trend: **Performance & evaluation**

    ---

    Live baselines with charts: retrieval quality at ceiling, the measured
    abstention gap, and per-stage latency with a proposed NFR-4 budget.

    [:octicons-arrow-right-24: Evaluation & performance](evaluation-results.md)

-   :shield: **Security & compliance**

    ---

    Access control verified live per persona, the privacy threat model, and
    the NIST AI RMF alignment evidence — alignment and evidence, deliberately
    not claimed as certification.

    [:octicons-arrow-right-24: NIST AI RMF](nist-ai-rmf/README.md) ·
    [:octicons-arrow-right-24: Threat model](threat-model.md)

</div>

## Where to look first

| You are a… | Start here |
|---|---|
| New to the project | [Quickstart](guides/quickstart.md), then the [architecture tour](guides/understanding-the-architecture.md) |
| Developer working on the stack | [Dev environment setup](dev-setup.md) (the deep reference), [Testing & CI quality gates](testing.md) |
| Deployer / platform engineer | [Compose](guides/deploy-compose.md) · [Helm](guides/deploy-helm.md) · [Air-gapped](guides/deploy-airgapped.md) |
| Reviewer of the security model | [Roles and permissions](roles-and-permissions.md), [Privacy threat model](threat-model.md), [Data governance](governance.md) |
| Operator of a deployment | [Observability](observability.md), [Credential rotation](credential-rotation.md), [SIEM detection runbook](siem-detection-runbook.md) |
| Auditor / compliance | [NIST AI RMF overview](nist-ai-rmf/README.md), [Evidence index](nist-ai-rmf/evidence/evidence-index.md) |

The guide pages are written for reading; every one ends with a **Sources**
section naming the canonical repo documents it was written from — those
remain the single source of truth, rendered unmodified under Core Concepts
and How-to Guides.
