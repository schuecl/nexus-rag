// Incident / outage report template (grafana-reports skill).
// Self-contained: no @preview imports, no #image/#include. Placeholders <LIKE-THIS>.

#let classification = "CUI"
#let incident-id = "<INC-2026-07-30-01>"
#let report-title = "<Incident Report — Retrieval latency degradation>"
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
  #text(10pt)[Incident #incident-id · #text(style: "italic")[#author]]
]
#line(length: 100%)

#table(
  columns: (1fr, 2fr, 1fr, 2fr),
  stroke: 0.4pt,
  [*Severity*], [<SEV-2 (degraded, no data loss)>],
  [*Status*], [<resolved / monitoring / ongoing>],
  [*Started*], [<2026-07-30 12:40 UTC (first metric deviation)>],
  [*Detected*], [<12:55 UTC, alert `<rule name>` / user report>],
  [*Mitigated*], [<14:10 UTC>],
  [*Duration*], [<1h 30m>],
  [*Services*], [<orchestration-mcp, reranker-service>],
  [*User impact*], [<slower answers; reranker fallback ordering>],
)

= Summary
<Three sentences max: what broke, what users saw, what ended it. Symptom vs
confirmed/probable cause stated explicitly.>

= Timeline (all times UTC, each row evidence-backed)
#table(
  columns: (0.8fr, 2.6fr, 2.6fr),
  stroke: 0.4pt,
  table.header([*Time*], [*Event*], [*Evidence (verbatim query / alert / log selector)*]),
  [<12:40>], [<p95 span latency steps 3× up>],
  [`histogram_quantile(0.95, sum by (le) (rate(traces_spanmetrics_latency_bucket{service="orchestration-mcp"}[5m])))`],
  [<12:55>], [<alert fires>], [<rule name + evaluation timestamp>],
  [<13:05>], [<reranker timeouts begin>], [`{service="reranker"} |~ "(?i)timeout"`],
  // ...
)

= Impact
<Quantified from queries: affected request share, denied/errored counts, log
error volume. No estimated numbers presented as measured.>

= Cause analysis
<Evidence chain per INSTRUCTIONS §3.6. Label each link confirmed (query shown)
or hypothesis (needs admin follow-up). Distinguish trigger vs root cause.>

= Follow-ups (referrals — this agent performs no actions)
#table(
  columns: (3fr, 1.4fr, 1fr),
  stroke: 0.4pt,
  table.header([*Action*], [*Owner (role)*], [*Priority*]),
  [<e.g. raise reranker timeout / add alert on fallback ratio>], [<platform admin>], [<P2>],
)

#line(length: 100%)
#text(8.5pt, style: "italic")[
  Method: all values retrieved read-only via Grafana MCP during the generating
  conversation; queries reproduce them exactly. Datasources: <uids>.
  Generated: <timestamp UTC>. Point-in-time snapshot; timeline granularity
  limited to metric/log resolution.
]
