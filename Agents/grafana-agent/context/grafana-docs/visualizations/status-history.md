# Status history


# Status history

A status history visualization displays data in a way that shows periodic states over time. In a status history, each field or series is rendered as a horizontal row, with multiple boxes showing the different statuses. This provides you with a centralized view for the status of a component or service.

For example, if you're monitoring the health status of different services, you can use a status history to visualize the different statuses, such as “True” or "False," over time. Each status is represented by a different color:

*(image/video omitted)*

> **Note:**
A status history is similar to a [state timeline](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/panels-visualizations/visualizations/state-timeline/), but has different [configuration options](#status-history-options). Unlike state timelines, status histories don't merge consecutive values.


Use a status history when you need to:

- Monitor the status of a server, application, or service to know when your infrastructure is experiencing issues over time.
- Identify operational trends over time.
- Spot any recurring issues with the health of your applications.

## Configure a status history

Once you've [created a dashboard](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/dashboards/build-dashboards/create-dashboard/), you can use the following state timeline video as a reference for how to configure a status history:

*(image/video omitted)*



## Supported data formats

The status history visualization works best if you have data capturing the various status of entities over time, formatted as a table. The data must include:

- **Timestamps** - Indicate when each status change occurred. This could also be the start time for the status change. You can also add an optional timestamp to indicate the end time for the status change.
- **Entity name/identifier** - Represents the name of the entity you're trying to monitor.
- **Status value** - Represents the state value of the entity you're monitoring. These can be string, numerical, or boolean states.

### Examples

The following tables are examples of the type of data you need for a status history visualization and how it should be formatted.

#### Single time column with null values

| Timestamps         | Backend_01 | Backend_02 |
| ------------------ | ---------- | ---------- |
| 2024-02-29 8:00:00 | OK         | WARN       |
| 2024-02-29 8:15:00 | WARN       |            |
| 2024-02-29 8:18:00 |            | WARN       |
| 2024-02-29 8:30:00 | BAD        |            |
| 2024-02-29 8:36:00 |            | OK         |
| 2024-02-29 8:45:00 | OK         |            |

The data is converted as follows, with the null and empty values visualized as gaps in the status history:

*(image/video omitted)*

#### Two time columns without null values

| Start time         | End time           | Backend_01 | Backend_02 |
| ------------------ | ------------------ | ---------- | ---------- |
| 2024-02-29 8:00:00 | 2024-02-29 8:15:00 | OK         | OK         |
| 2024-02-29 8:15:00 | 2024-02-29 8:30:00 | OK         | OK         |
| 2024-02-29 8:30:00 | 2024-02-29 8:45:00 | OK         | OK         |
| 2024-02-29 8:45:00 | 2024-02-29 9:00:00 | BAD        | WARN       |
| 2024-02-29 9:00:00 | 2024-02-29 9:15:00 | OK         | WARN       |

The data is converted as follows:

*(image/video omitted)*

## Pan and zoom panel time range

*(shared doc include omitted -- see grafana.com docs)*

## Configuration options

*(shared doc include omitted -- see grafana.com docs)*

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### Status history options

Use these options to refine the visualization.

<!-- prettier-ignore-start -->

| Option | Description                                                                                     |
| ------ | ----------------------------------------------------------------------------------------------- |
| Show values  | Controls whether values are rendered inside the status cells. Choose from **Auto**, **Always**, and **Never**. **Auto** renders values if there is sufficient space. |
| Row height  | Controls the height of boxes. `1` creates the largest boxes, and `0` creates the smallest boxes with more gap. |
| Column width | Controls the width of boxes. `1` creates the widest boxes, and `0` creates the narrowest boxes with more gap. |
| Page size (enable pagination) | The **Page size** option lets you paginate the status history visualization to limit how many series are visible at once. This is useful when you have many series. |
| Line width | Controls line width of status cells. This is a field option and can be set in field defaults or overrides. |
| Fill opacity | Controls the fill opacity of status cells. This is a field option and can be set in field defaults or overrides. |

<!-- prettier-ignore-end -->

### Legend options

*(shared doc include omitted -- see grafana.com docs)*

### Tooltip options

*(shared doc include omitted -- see grafana.com docs)*

### Annotation options

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
