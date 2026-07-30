// Executive one-pager (grafana-reports skill). HARD one-page cap: if it
// doesn't fit, cut detail -- never shrink below 9.5pt or spill to page 2.
// Plain language; jargon only inside the parenthetical evidence notes.
// Self-contained; placeholders <LIKE-THIS>.

#let classification = "CUI"
#let report-title = "<Nexus RAG — Weekly Status>"
#let period = "<Week of 2026-07-27>"

#set page(
  paper: "us-letter",
  margin: (top: 1.3cm, bottom: 1.3cm, x: 1.6cm),
  header: align(center, text(weight: "bold", fill: rgb("#7a2518"), classification)),
  footer: align(center, text(weight: "bold", fill: rgb("#7a2518"), classification)),
)
#set text(font: "Libertinus Serif", size: 10pt)

#align(center)[
  #text(16pt, weight: "bold")[#report-title]\
  #text(10pt)[#period · prepared read-only from live monitoring]
]
#line(length: 100%)

// Stoplight: green = healthy, amber = degraded/watch, red = action needed.
#let g = table.cell(fill: rgb("#e8f5e9"))[GREEN]
#let a = table.cell(fill: rgb("#fff8e1"))[AMBER]
#let r = table.cell(fill: rgb("#ffebee"))[RED]

= At a glance
#table(
  columns: (1.6fr, 0.7fr, 3.4fr),
  stroke: 0.4pt,
  table.header([*Area*], [*State*], [*Basis (measured this week)*]),
  [Document ingestion], g, [<N docs processed, 0 failures; queue age < 5 s throughout>],
  [Search & retrieval], a, [<N queries; reranker fallback 4% Tue 13:00-14:30, else 0>],
  [Access control], g, [<N denials, all consistent with user clearances -- controls working>],
  [Platform (DB, queue, identity)], g, [<all services up; Postgres connections peak 40% of max>],
)

= Three numbers that matter
#grid(columns: (1fr, 1fr, 1fr), gutter: 8pt,
  align(center)[#text(20pt, weight: "bold")[<1,204>]\ #text(9pt)[questions answered]],
  align(center)[#text(20pt, weight: "bold")[<99.6%>]\ #text(9pt)[queries without error]],
  align(center)[#text(20pt, weight: "bold")[<57>]\ #text(9pt)[documents added to corpus]],
)

= Watch items & risks
- <One line each, max three, each traceable to a §At-a-glance row. What could
  become a problem, and who owns watching it (role, not name).>

= Decisions needed  // omit section if none
- <Anything requiring leadership action, stated as a question with options.>

#line(length: 100%)
#text(8pt, style: "italic")[
  Source: live Grafana monitoring, read-only, retrieved <timestamp UTC>.
  Detailed queries available in the companion technical report on request.
]
