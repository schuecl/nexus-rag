# Alert list


# Alert list

Alert lists allow you to display a list of important alerts that you want to track. You can configure the alert list to show the current state of your alert, such as firing, pending, or normal. Learn more about alerts in [Grafana Alerting overview](ref:grafana-alerting-overview).

![An alert list visualization](/media/docs/grafana/panels-visualizations/screenshot-alert-list-v11.3.png)

On each dashboard load, this visualization queries the alert list, always providing the most up-to-date results.



## Configure an alert list

Once you’ve [created a dashboard](ref:create-dashboard), the following video shows you how to configure an alert list visualization:

*(image/video omitted)*

## Configuration options

*(shared doc include omitted -- see grafana.com docs)*

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### Options

Use the following options to refine your alert list visualization.

<!-- prettier-ignore-start -->

| Option     | Description                                                                                               |
| ---------- | --------------------------------------------------------------------------------------------------------- |
| View mode  | Choose between **List** to display alerts in a detailed list format with comprehensive information, or **Stat** to show alerts as a summarized single-value statistic.  |
| Group mode | Choose between **Default grouping** to show alert instances grouped by their alert rule, or **Custom grouping** to show alert instances grouped by a custom set of labels. |
| Group by | When **Custom grouping** is selected, choose label keys to group alert instances. |
| Max items | Sets the maximum number of alerts to list when **Group mode** is **Default grouping**. This option is hidden for **Custom grouping**. By default, Grafana sets this value to 20. |
| [Sort order](#sort-order) | Select how to order the alerts displayed. |
| Alerts linked to this dashboard | Toggle the switch on to only show alerts from the dashboard the alert list is in. |

<!-- prettier-ignore-end -->

#### Sort order

Select how to order the alerts displayed. Choose from:

- **Alphabetical (asc)** - Alphabetical order.
- **Alphabetical (desc)** - Reverse alphabetical order.
- **Importance** - By importance according to the following values, with 1 being the highest:
  - alerting: 1
  - firing: 1
  - no_data: 2
  - pending: 3
  - ok: 4
  - paused: 5
  - inactive: 5
- **Time (asc)** - Oldest active alert instances first.
- **Time (desc)** - Newest active alert instances first.

### Filter options

These options allow you to limit alerts shown to only those that match the query, folder, or tags you choose.

<!-- prettier-ignore-start -->

| Option     | Description                                                                                               |
| ---------- | --------------------------------------------------------------------------------------------------------- |
| Alert name | Filter alerts by name. |
| Alert instance label | Filter alert instances using [label](ref:alert-label) querying. For example,`{severity="critical", instance=~"cluster-us-.+"}`. |
| Datasource | Filter alerts from the selected data source. |
| Folder | Filter Grafana-managed alert rules by alert rule folder. This option is available only when **Datasource** is empty or set to **Grafana**. It doesn't filter by dashboard folder. |
| Show alerts with 0 instances | Filter for alert rules with no instances. Alert rules with 0 (zero) instances are hidden by default. You can choose to show them by toggling this switch. Because these rules have no instances, they remain hidden if the **Alert instance label** filter is configured. |

### Alert state filter options

Choose which alert states to display in this visualization.

<!-- prettier-ignore-start -->

| Option     | Description                                                                                               |
| ---------- | --------------------------------------------------------------------------------------------------------- |
| Alerting / Firing | Shows alerts that are currently active and triggering an alert condition. |
| Pending | Shows alerts that are in a transitional state, waiting for conditions to be met before triggering. |
| No Data | Shows alerts where the data source is not returning any data, which could indicate an issue with data collection. |
| Recovering | Shows alerts in a recovering state after the alert condition is resolved. |
| Normal | Shows alerts that are in a normal or resolved state, where no alert condition is currently met. |
| Error | Shows alerts where an error has occurred, typically related to an issue in the alerting process. |

<!-- prettier-ignore-end -->
