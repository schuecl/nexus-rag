// Observability health report template (grafana-reports skill).
// Self-contained on purpose: NO @preview package imports, NO #image/#include
// of external paths -- must compile air-gapped with a bare `typst compile`.
// Placeholders to fill are marked <LIKE-THIS>.

#let classification = "CUI"   // deployment's marking -- confirm before generating
#let report-title = "<Nexus RAG — Operations Health Report>"
#let period = "<2026-07-30 00:00 – 23:59 UTC>"
#let author = "<Grafana Assistant (read-only agent), for: username>"

#set page(
  paper: "us-letter",
  margin: (top: 1.4cm, bottom: 1.4cm, x: 1.6cm),
  header: align(center, text(weight: "bold", fill: rgb("#7a2518"), classification)),
  footer: align(center)[
    #text(weight: "bold", fill: rgb("#7a2518"), classification)
    #h(1fr) #counter(page).display("1 of 1", both: true)
  ],
)
#set text(font: "Libertinus Serif", size: 10.5pt)
#set heading(numbering: "1.")

#align(center)[
  #text(17pt, weight: "bold")[#report-title]\
  #text(11pt)[Reporting period: #period]\
  #text(9pt, style: "italic")[#author]
]
#line(length: 100%)

= Summary
<One paragraph: overall state, anything requiring action, symptom vs probable
cause stated explicitly. No claim without a matching row in §2.>

= Findings (every claim tied to its query)
#table(
  columns: (2.6fr, 1fr, 3fr),
  stroke: 0.4pt,
  table.header([*Claim*], [*Value*], [*Query (verbatim)*]),
  [<RAG queries served, 24h>], [<1,204>],
  [`sum(increase(nexus_rag_queries_total[24h]))`],
  [<Denied by access control>], [<12>],
  [`sum(increase(nexus_rag_queries_total{outcome="denied"}[24h])) or vector(0)`],
  // ...one row per claim; add rows, never unreferenced prose
)

= Alert state
<Firing first with evaluation timestamps, then pending, then "N rules normal".
Firing ≠ outage: pair each with its measured value vs threshold.>

= Investigation notes  // omit section if this is a routine health report
<Triage narrative per INSTRUCTIONS §3.6: alerts → metrics (pinned window) →
logs (implicated services only). Log excerpts follow the secret-redaction rule.>

= Recommended next steps
<Referrals only — actions (restart, silence, config change) name the
responsible admin/on-call role; this agent neither performs nor scripts them.>

#line(length: 100%)
#text(8.5pt, style: "italic")[
  Method: all values retrieved read-only via the Grafana MCP tools during the
  generating conversation; queries reproduce the numbers exactly. Datasource:
  <prometheus-uid / loki-uid>. Generated: <timestamp UTC>. This report is a
  point-in-time snapshot.
]
