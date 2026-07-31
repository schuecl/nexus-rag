// Capacity & trend report (grafana-reports skill). Now-vs-baseline deltas,
// saturation signals, and projections -- with projections ALWAYS labeled as
// estimates, never presented as measured. Self-contained; placeholders <LIKE-THIS>.

#let classification = "CUI"
#let report-title = "<Nexus RAG — Capacity & Growth Report>"
#let period = "<2026-07-01 – 2026-07-30 (baseline: June)>"
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
  #text(11pt)[Period: #period] · #text(9pt, style: "italic")[#author]
]
#line(length: 100%)

= Summary
<One paragraph: is growth on trend, is anything approaching a limit, what needs
a decision and by roughly when (estimate, per §4's labeling rule).>

= Growth (measured)
#table(
  columns: (2.2fr, 1fr, 1fr, 0.9fr, 3fr),
  stroke: 0.4pt,
  table.header([*Metric*], [*Baseline*], [*Now*], [*Δ*], [*Query (verbatim)*]),
  [<Corpus size (approved docs)>], [<310>], [<367>], [<+18%>], [<query>],
  [<Query volume / day (avg)>], [<820>], [<1,204>], [<+47%>], [`sum(increase(nexus_rag_queries_total[24h]))` <averaged over period>],
  [<Vector points stored>], [<72k>], [<91k>], [<+26%>], [<query>],
  // ...
)

= Saturation signals (measured)
#table(
  columns: (2.2fr, 1.2fr, 1.2fr, 3fr),
  stroke: 0.4pt,
  table.header([*Resource*], [*Peak this period*], [*Limit*], [*Query (verbatim)*]),
  [<Postgres connections>], [<41>], [<100 (`pg_settings_max_connections`)>], [`max_over_time(sum(pg_stat_activity_count)[30d:1h])`],
  [<Ingestion queue age>], [<38 s>], [<none set — watch item>], [`max_over_time(nexus_rag_ingestion_queue_oldest_unpublished_seconds[30d:1h])`],
  // ...
)

= Projections (ESTIMATES — linear extrapolation of §2, not measurements)
- <"At the current +18%/month, corpus reaches ~X docs by <month>. Assumes
  trend holds; re-check monthly."> Each projection names its assumption.
- <Nearest limit and its estimated horizon.>

= Recommendations (referrals)
<Capacity actions -- raise limits, add storage, re-baseline alerts -- named to
the responsible admin role with a suggested decision date. This agent performs
none of them.>

#line(length: 100%)
#text(8.5pt, style: "italic")[
  Method: measured sections retrieved read-only via Grafana MCP; queries
  reproduce them. §Projections are arithmetic estimates from §Growth, labeled
  as such. Datasources: <uids>. Generated: <timestamp UTC>.
]
