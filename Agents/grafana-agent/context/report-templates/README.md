# Report template library

Organized templates for the `grafana-reports` skill. The agent picks by the
routing table below, fills placeholders (`<LIKE-THIS>`), validates, previews,
and delivers `.typ` source; the recipient runs `typst compile <file>.typ` for
the PDF.

## Which template for which request

| The user asks for... | Template | Shape |
|---|---|---|
| "daily/weekly health report", "status report" | `health-report.typ` | summary → findings table → alert state → next steps |
| "incident report", "outage write-up", "post-incident summary" | `incident-report.typ` | metadata block → timeline → impact → cause analysis → follow-ups |
| "write up what we just found", quick investigation record | `triage-note.typ` | question → hypothesis trail → evidence → conclusion (1–2 pages) |
| "something for leadership", "one-pager", "brief for the boss" | `exec-onepager.typ` | stoplight table + 3 headline numbers + risks, hard one-page cap |
| "capacity report", "growth/trend report", "are we going to run out" | `capacity-report.typ` | now-vs-baseline deltas → saturation signals → labeled projections |

Ambiguous request → ask which shape, listing the five in one line each.

## Conventions every template enforces (do not remove when filling)

1. **Classification banner** in header + footer (default `CUI`; confirm before
   generating). A report inherits the sensitivity of its most sensitive content.
2. **Evidence discipline:** every quantitative claim appears with its verbatim
   query (findings/timeline/evidence tables have a query column for exactly
   this reason). No unreferenced numbers.
3. **Offline-resolvable source only:** `#import "@preview/..."` is allowed
   ONLY for the vendored packages below (resolved from `vendored/`, no
   network); any other package import, and any `#image`/`#include` of file
   paths, is forbidden. The five house templates use no imports at all and
   compile with a bare `typst compile` anywhere.
4. **Methodology footer:** read-only retrieval statement, datasource UIDs,
   generation timestamp, point-in-time caveat.
5. **Referrals, not actions:** recommended next steps name the responsible
   admin/on-call role; the agent neither performs nor scripts changes
   (INSTRUCTIONS §4.1).

## LaTeX and other formats

- **LaTeX in:** good existing LaTeX material (a team's report boilerplate, an
  equation, a table) can be converted with the Typst MCP's
  `latex_snippet_to_typst` tool (Pandoc-backed) and grafted into a template.
  Treat converted output like any other draft: validate, preview, keep the
  conventions above.
- **LaTeX out: no.** One output format (Typst) keeps validation, preview, and
  the air-gap story simple; anyone needing `.tex` can convert downstream.
- **PDF:** produced by the recipient (`typst compile`) — the Typst MCP server
  previews PNG only and has no PDF export; say so rather than promising a PDF
  attachment.

## Vendored Typst Universe packages (`vendored/`)

Imported from the official [typst/packages](https://github.com/typst/packages)
repo by [`vendored/fetch-templates.sh`](vendored/fetch-templates.sh) into the
exact layout Typst's resolver expects (`preview/<name>/<version>/`), so
`#import "@preview/<name>:<version>"` works **fully offline**:

```bash
# CLI (recipient rendering a PDF):
typst compile --package-path context/report-templates/vendored report.typ
# Container (typst-mcp service): both env vars point at the read-only mount
TYPST_PACKAGE_PATH=/vendored  TYPST_PACKAGE_CACHE_PATH=/vendored
```

Curated set (62 packages total on disk, ~53M — the requested set below plus
the transitive closure of their imports, resolved automatically by the script
since Typst packages carry no dependency manifest):

| Category | Package (version) | Use for |
|---|---|---|
| Report/document templates | `ilm` 2.1.1 · `basic-report` 0.5.0 · `modern-technique-report` 0.1.0 · `letter-pro` 3.0.0 | polished reports, simple tech reports, memos |
| **Figures & data plots** | `lilaq` 0.6.0 · `cetz` 0.5.2 · `cetz-plot` 0.1.4 | **charting query results inside reports** (data inlined as literals) |
| Diagrams & timelines | `fletcher` 0.5.8 · `timeliney` 0.4.0 | flow/architecture diagrams; Gantt-style incident timelines |
| Writing aids | `unify` 0.8.1 · `glossarium` 0.5.10 | SI units/quantities; acronym glossaries |
| Presentations | `touying` 0.7.4 · `diatypst` 0.9.3 | ops-review slide decks (diatypst = simple, touying = powerful) |
| Academic papers | `charged-ieee` 0.1.4 · `arkheion` 0.1.2 · `starter-journal-article` 0.5.1 | the research writeup itself (IEEE / arXiv / journal styles) |

Rules and notes:

- Versions are pins — bump them in `fetch-templates.sh` deliberately; the
  closure pass re-resolves dependencies.
- LICENSE files ship with each package and stay. Almost everything is
  MIT/MIT-0/Apache-2.0; **one transitive pull (`xarrow`) is GPL-3.0-only** —
  fine to use (document *output* is not a derivative work), but flag it if
  this directory is ever redistributed.
- The closure intentionally over-approximates (it also follows imports found
  in packages' bundled docs/examples), trading disk for the guarantee that
  everything present compiles offline.
- **When a vendored template is used, the classification banner and
  evidence-table conventions still apply** (graft them in — the house
  templates show how). For charts, plot data must be inline literals taken
  from this conversation's query results — never file reads.

## Adding a template

Copy the closest existing one; keep conventions 1–5; add a routing-table row
here in the same change (the agent selects from this README — an unlisted
template is invisible to it).
