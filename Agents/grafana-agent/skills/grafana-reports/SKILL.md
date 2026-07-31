---
name: grafana-reports
description: Generate typeset observability reports (health summaries, incident timelines, triage write-ups) by combining Grafana MCP evidence with the Typst MCP server (validate + PNG preview; PDF via one typst compile of the delivered source). Use when the user asks for a report, summary document, PDF, or shareable write-up of dashboards/metrics/alerts.
---

# Observability reports via Typst (read-only evidence → typeset document)

## Overview

Turns evidence gathered through the Grafana tools into a typeset report:
author Typst source from the bundled template, validate it, render a PNG
preview, deliver the `.typ` source. The Typst server is an authoring aid —
it renders previews, it does not export PDF; the final PDF is one
`typst compile report.typ` by the recipient. Rendering is data-blind: the
Typst container holds no Grafana credentials and cannot fetch anything.

## When to use

- "Give me a PDF/report/summary I can share" about system health, an
  incident, or a triage investigation.
- Recurring health summaries (daily/weekly ops report).
- NOT for quick answers — a chat reply with numbers beats a document unless
  the user asked for an artifact.

## How to use

1. **Evidence first, entirely via Grafana tools** (`grafana-metrics`,
   `grafana-alerts`, `grafana-logs` skills). Every number in the report must
   come from a query run in THIS conversation, with its query string kept.
2. **Select the template** from `../../context/report-templates/README.md`'s
   routing table (health, incident, triage note, exec one-pager, capacity —
   or a vendored Typst Universe package: report skins, `touying`/`diatypst`
   slides for ops reviews, `charged-ieee`/`arkheion` papers) and fill it:
   title, period, classification banner, findings table (claim + value +
   exact query), methodology footer. Ambiguous ask → offer the shapes in one
   line each.
   **Charts:** metric trends can be plotted with `lilaq` (or `cetz-plot`) —
   plot data MUST be inline literals transcribed from this conversation's
   query results; incident timelines can use `timeliney`; architecture
   sketches `fletcher`.
3. **Validate** with `check_if_snippet_is_valid_typst_syntax`; on errors,
   consult the Typst docs tools (`list_docs_chapters` / `get_docs_chapter`)
   rather than guessing syntax.
4. **Preview** with `typst_to_image` and check the layout renders (banner
   visible, table not overflowing).
5. **Deliver** the full `.typ` source in a code block, the preview, and the
   one-liner to produce the PDF: `typst compile report.typ`.

## Guardrails

- **Markings are mandatory.** Every report carries the deployment's
  classification/handling banner (template header+footer, default `CUI`) —
  a report aggregating observability data inherits the sensitivity of its
  most sensitive content. If unsure of the right marking, ask before
  generating.
- **Evidence-only content:** nothing enters a report that didn't come from
  this conversation's tool results. No speculation, no user-pasted content
  presented as measured data (quote it as user-provided if included).
- **Offline-resolvable imports only:** `@preview/...` imports are allowed
  solely for packages present in the vendored set
  (`../../context/report-templates/vendored/preview/` — resolved from the
  read-only mount, never the network; see the library README for the curated
  list). Any other package import, and any `#image(...)`/`#include` of
  filesystem paths, is forbidden — a network fetch fails air-gapped and is an
  exfiltration channel. When using a vendored template, graft the
  classification banner + evidence conventions in; they are not optional.
- **Same refusals as everywhere:** a report is a read-only artifact. Requests
  to embed admin data (user lists, tokens, datasource configs) are refused
  per INSTRUCTIONS §4.1; log excerpts in reports follow the secret-redaction
  rule (§4.4).
