# Visualizations


# Visualizations

Grafana offers a variety of visualizations to support different use cases. This section of the documentation highlights the built-in visualizations, their options and typical usage.

*(image/video omitted)*

> **Note:**
If you are unsure which visualization to pick, Grafana can provide visualization suggestions based on the panel query. When you select a visualization, Grafana will show a preview with that visualization applied.


- Graphs & charts
  - [Time series](ref:time-series) is the default and main graph visualization. Alerts are supported in this panel.
  - [State timeline](ref:state-timeline) for state changes over time.
  - [Status history](ref:status-history) for periodic state over time.
  - [Bar chart](ref:bar-chart) shows any categorical data.
  - [Histogram](ref:histogram) calculates and shows value distribution in a bar chart.
  - [Heatmap](ref:heatmap) visualizes data in two dimensions, used typically for the magnitude of a phenomenon.
  - [Pie chart](ref:pie-chart) is typically used where proportionality is important.
  - [Candlestick](ref:candlestick) is typically for financial data where the focus is price/data movement.
  - [Gauge](ref:gauge) is the traditional rounded visual showing how far a single metric is from a threshold.
  - [Trend](ref:trend) for datasets that have a sequential, numeric x that is not time.
  - [XY chart](ref:xy-chart) provides a way to visualize arbitrary x and y values in a graph.
- Stats & numbers
  - [Stat](ref:stat) for big stats and optional sparkline.
  - [Bar gauge](ref:bar-gauge) is a horizontal or vertical bar gauge.
- Misc
  - [Table](ref:table) is the main and only table visualization.
  - [Logs](ref:logs) is the main visualization for logs.
  - [Node graph](ref:node-graph) for directed graphs or networks.
  - [Traces](ref:traces) is the main visualization for traces.
  - [Flame graph](ref:flame-graph) is the main visualization for profiling.
  - [Canvas](ref:canvas) allows you to explicitly place elements within static and dynamic layouts.
  - [Geomap](ref:geomap) helps you visualize geospatial data.
- Widgets
  - [Dashboard list](ref:dashboard-list) can list dashboards.
  - [Alert list](ref:alert-list) can list alerts.
  - [Annotations list](ref:annotations-list) can list available annotations.
  - [Text](ref:text) can show markdown and html.
  - [News](ref:news) can show RSS feeds.

The following video shows you how to create gauge, time series line graph, stats, logs, and node graph visualizations:

*(image/video omitted)*

## Get more

You can add more visualization types by installing [panel plugins](https://grafana.com/grafana/plugins/?type=panel).

## Examples

Below you can find some good examples for how all the visualizations in Grafana can be configured. You can also explore [play.grafana.org](https://play.grafana.org) which has a large set of demo dashboards that showcase all the different visualizations.

### Graphs

For time based line, area and bar charts we recommend the default [time series](ref:time-series) visualization. [This public demo dashboard](https://play.grafana.org/d/000000016/1-time-series-graphs?orgId=1) contains many different examples for how this visualization can be configured and styled.

*(image/video omitted)*

For categorical data use a [bar chart](ref:bar-chart).

*(image/video omitted)*

### Big numbers & stats

A [stat](ref:stat) shows one large stat value with an optional graph sparkline. You can control the background or value color using thresholds or color scales.

*(image/video omitted)*

### Gauge

If you want to present a value as it relates to a min and max value you have two options. First a standard radial [gauge](ref:gauge) shown below.

*(image/video omitted)*

Secondly Grafana also has a horizontal or vertical [bar gauge](ref:bar-gauge) with three different distinct display modes.

*(image/video omitted)*

### Table

To show data in a table layout, use a [table](ref:table).

*(image/video omitted)*

### Pie chart

To display reduced series, or values in a series, from one or more queries, as they relate to each other, use a [pie chart](ref:pie-chart).

*(image/video omitted)*

### Heatmaps

To show value distribution over, time use a [heatmap](ref:heatmap).

*(image/video omitted)*

### State timeline

A state timeline shows discrete state changes over time. When used with time series, the thresholds are used to turn the numerical values into discrete state regions.

*(image/video omitted)*
