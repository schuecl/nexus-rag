// Triage / investigation note (grafana-reports skill) -- the short-form record
// of an INSTRUCTIONS §3.6 investigation. 1-2 pages; for a full incident use
// incident-report.typ. Self-contained; placeholders <LIKE-THIS>.

#let classification = "CUI"
#let note-title = "<Triage note — Why did ingestion stall on 2026-07-30?>"
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

#text(15pt, weight: "bold")[#note-title]\
#text(9pt, style: "italic")[#author · <timestamp UTC>]
#line(length: 100%)

*Question:* <the user's question, verbatim.>

*Answer (one paragraph):* <conclusion up front; symptom vs probable cause;
confidence stated (confirmed / likely / needs admin access to confirm).>

== Hypothesis trail
#table(
  columns: (2.2fr, 3fr, 1fr),
  stroke: 0.4pt,
  table.header([*Hypothesis*], [*Test (verbatim query)*], [*Result*]),
  [<worker crashed>], [`nexus_rag_ingestion_worker_consumer_running`], [<ruled out — 1 throughout>],
  [<hand-off stuck>], [`nexus_rag_ingestion_queue_oldest_unpublished_seconds`], [<supported — rising from 13:02>],
  // one row per hypothesis, including the ruled-out ones -- negative results
  // are evidence too
)

== Key evidence
<2-4 representative items: metric readings with windows, log lines (secrets
redacted as present-at-location), alert evaluations with timestamps.>

== Next step
<Single recommended action, referred to the responsible admin/on-call role.>

#line(length: 100%)
#text(8.5pt, style: "italic")[
  Read-only retrieval via Grafana MCP; queries reproduce all values.
  Datasources: <uids>. Point-in-time snapshot.
]
