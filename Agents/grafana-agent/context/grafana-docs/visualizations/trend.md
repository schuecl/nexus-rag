# Trend


# Trend

Trend visualizations should be used for datasets that have a sequential, numeric x-field that is not time. Some examples are function graphs, rpm/torque curves, supply/demand relationships, and elevation or heart rate plots along a race course (with x as distance or duration from start).

For example, you could represent engine power and torque versus speed where speed is plotted on the x-axis and power and torque are plotted on the y-axes:

*(image/video omitted)*

Trend visualizations support all visual styles and options available in the [time series visualization](ref:time-series-visualization) with these exceptions:

- No annotations or time regions
- No shared cursor/crosshair
- No multi-timezone x-axis
- No ability to change the dashboard time range using drag-selection

Trend visualizations require at least two numeric fields. The x-field must use ascending numeric values. If the values aren't ascending, Grafana shows an error. When multiple frames or queries exist, you should use a [join transformation](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/visualizations/panels-visualizations/query-transform-data/transform-data/) on the x-fields to produce a single frame.

## Configuration options

*(shared doc include omitted -- see grafana.com docs)*

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### X axis options

In the **X field** option, select a numeric field that contains ascending values.

### Tooltip options

*(shared doc include omitted -- see grafana.com docs)*

### Legend options

*(shared doc include omitted -- see grafana.com docs)*

### Graph styles options

The options under the **Graph styles** section let you control the general appearance of the graph, excluding [color](#standard-options). These options apply to numeric-x series.

*(shared doc include omitted -- see grafana.com docs)*

### Axis options

*(shared doc include omitted -- see grafana.com docs)*

### Standard options

*(shared doc include omitted -- see grafana.com docs)*

### Data links and actions

*(shared doc include omitted -- see grafana.com docs)*

### Value mappings

*(shared doc include omitted -- see grafana.com docs)*

### Thresholds

*(shared doc include omitted -- see grafana.com docs)*

### Field overrides

*(shared doc include omitted -- see grafana.com docs)*
