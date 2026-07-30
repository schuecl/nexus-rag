# Grafana official docs — vendored context

Official Grafana panels/visualizations documentation, vendored as markdown for
the agent's on-demand use (and for air-gapped deployments where grafana.com is
unreachable).

- **Source:** [`grafana/grafana`](https://github.com/grafana/grafana)
  `docs/sources/visualizations/panels-visualizations/`, tag **v13.1.1**
  (deployed Grafana is 13.1.0 — same docs line; re-fetch on upgrade).
- **Fetched:** 2026-07-30 via [`fetch-docs.sh`](fetch-docs.sh) — re-run it with a
  new tag to refresh: `./fetch-docs.sh v13.2.0`.
- **License/attribution:** Grafana and its documentation are © Grafana Labs,
  licensed AGPL-3.0. These files are lightly processed copies (Hugo shortcodes
  stripped, figures/videos omitted); the authoritative version is
  https://grafana.com/docs/grafana/latest/panels-visualizations/.
- **Known gaps:** images/videos and Hugo `docs/shared` includes are omitted
  (marked inline). The three community plugins our dashboards use
  (`andrewbmchugh-flow-panel`, `grafana-graphviz-panel`,
  `jdbranham-diagram-panel`) have no official Grafana docs — see each plugin's
  own repo/README.

## Layout

| Directory | Contents |
|---|---|
| `visualizations/` | One file per visualization type — **all 25** official types (alert-list, annotations, bar-chart, bar-gauge, candlestick, canvas, dashboard-list, flame-graph, gauge, geomap, heatmap, histogram, logs, news, node-graph, pie-chart, stat, state-timeline, status-history, table, text, time-series, traces, trend, xy-chart) plus `_overview.md` (choosing a visualization) |
| `configuration/` | Panel mechanics: panel/editor/inspector overviews, standard options, overrides, thresholds, value mappings, legend, data links, tooltips — and the query docs (`query-overview`, `query-transform-data`, `query-expression-queries`, `query-calculation-types`, `query-troubleshoot-queries`) |

## How the agent uses this

- "What is a state timeline / how do I read this heatmap?" →
  `visualizations/<type>.md`.
- "Why does this panel show a different number than my query?" →
  `configuration/query-calculation-types.md` (panel *calculation* — last, mean,
  max — applied on top of the query) and
  `configuration/query-troubleshoot-queries.md`.
- "What do the thresholds/colors on this panel mean?" →
  `configuration/configure-thresholds.md` / `configure-value-mappings.md`.
- Deployment-specific questions (what OUR dashboards show and how to read them)
  stay with [`../panels-catalog.md`](../panels-catalog.md); this directory is
  generic Grafana knowledge only. Both are read-only reference — nothing here
  changes the guardrails in `../../INSTRUCTIONS.md`.
