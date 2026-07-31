# Flame graph


# Flame graph

Flame graphs let you visualize [profiling](https://grafana.com/docs/pyroscope/latest/introduction/what-is-profiling/) data. Using this visualization, a [profile](https://grafana.com/docs/pyroscope/latest/view-and-analyze-profile-data/profiling-types/) can be represented as a [flame graph](#flame-graph-mode), [top table](#top-table-mode), or both.

For example, if you want to understand which parts of a program consume the most resources, such as CPU time, memory, or I/O operations, you can use a flame graph to visualize and analyze where potential performance issues are:

*(image/video omitted)*

You can use a flame graph visualization if you need to:

- Identify any performance hotspots to find where code optimizations may be needed.
- Diagnose the root cause of any performance degradation.
- Analyze the behavior of complex systems, including distributed systems or microservices architectures.

To learn more about how Grafana Pyroscope visualizes flame graphs, refer to [Flame graphs: Visualizing performance data](https://grafana.com/docs/pyroscope/latest/view-and-analyze-profile-data/flamegraphs/).

## Configure a flame graph visualization

Once you’ve created a [dashboard](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/dashboards/build-dashboards/create-dashboard/), the following video shows you how to configure a flame graph visualization:

*(image/video omitted)*



## Supported data formats

To render a flame graph, you must format the data frame data using a _nested set model_.

A nested set model ensures each item of a flame graph is encoded by its nesting level as an integer value, its metadata, and by its order in the data frame. This means that the order of items is significant and needs to be correct. The ordering is a depth-first traversal of the items in the flame graph which recreates the graph without needing variable-length values in the data frame like in a children's array.

Required fields:

| Field name | Type           | Description                                                                                                                 |
| ---------- | -------------- | --------------------------------------------------------------------------------------------------------------------------- |
| level      | number         | The nesting level of the item, which represents how many items are between this item and the top item of the flame graph.   |
| value      | number         | The absolute or cumulative value of the item. This translates to the width of the item in the graph.                        |
| label      | string or enum | Label to be shown for the particular item.                                                                                  |
| self       | number         | Self value, which is usually the cumulative value of the item minus the sum of cumulative values of its immediate children. |

Diff profiles can also include optional `valueRight` and `selfRight` fields. When present, the tooltip and top table show baseline, comparison, and diff values.

### Example

The following table is an example of the type of data you need for a flame graph visualization and how it should be formatted:

| level | value    | self   | label                                     |
| ----- | -------- | ------ | ----------------------------------------- |
| 0     | 16.5 Bil | 16.5 K | total                                     |
| 1     | 4.10 Bil | 4.10 k | test/pkg/agent.(\*Target).start.func1     |
| 2     | 4.10 Bil | 4.10 K | test/pkg/agent.(\*Target).start.func1     |
| 3     | 3.67 Bil | 3.67 K | test/pkg/distributor.(\*Distributor).Push |
| 4     | 1.13 Bil | 1.13 K | compress/gzip.(\*Writer).Write            |
| 5     | 1.06 Bil | 1.06 K | compress/flat.(\*compressor).write        |

## Flame graph mode

A flame graph takes advantage of the hierarchical nature of profiling data. It condenses data into a format that allows you to easily see which code paths are consuming the most system resources, such as CPU time, allocated objects, or space when measuring memory. Each block in the flame graph represents a function call in a stack and its width represents its value.

Grayed-out sections are a set of functions that represent a relatively small value and they are collapsed together into one section for performance reasons.

*(image/video omitted)*

You can hover over a specific function to view a tooltip that shows you additional data about that function, like the function's value, percentage of total value, and the number of samples with that function.

*(image/video omitted)*

### Menu actions

You can click a function to show a drop-down menu with additional actions:

- [Focus block](#focus-block)
- [Copy function name](#copy-function-name)
- [Sandwich view](#sandwich-view)
- [Grouping](#grouping)

*(image/video omitted)*

#### Focus block

When you click **Focus block**, the block, or function, is set to 100% of the flame graph's width and all its child functions are shown with their widths updated relative to the width of the parent function. This makes it easier to drill down into smaller parts of the flame graph.

*(image/video omitted)*

#### Copy function name

When you click **Copy function name**, the full name of the function that the block represents is copied.

#### Sandwich view

The sandwich view allows you to show the context of the clicked function. It shows all the function's callers on the top and all the callees at the bottom. This shows the aggregated context of the function so if the function exists in multiple places in the flame graph, all the contexts are shown and aggregated in the sandwich view.

*(image/video omitted)*

#### Grouping

Under the **Grouping** section of the menu, the following options let you expand and collapse groups of functions:

- **Expand group** - Expands the grouped function you've clicked. Displayed if you click a function that's been automatically grouped in the flame graph.
- **Expand all groups** - Expands all grouped functions in the flame graph. Always displayed when you click the graph.
- **Collapse group** - Collapses the expanded function you've clicked. Displayed if you click a function in the flame graph that's been manually expanded.
- **Collapse all groups** - Collapses all expanded functions in the flame graph. Displayed if there are any expanded functions when you click the graph.

### Status bar

The status bar shows metadata about the flame graph and currently applied modifications, like what part of the graph is in focus or what function is shown in sandwich view. Click the **X** in the status bar pill to remove that modification.

*(image/video omitted)*

## Top table mode

The top table shows the functions from the profile in table format. The table has three columns: **Symbol**, **Self**, and **Total**. The table is sorted by self time by default, but can be reordered by total time or symbol name by clicking the column headers. Each row represents aggregated values for the given function if the function appears in multiple places in the profile.

*(image/video omitted)*

There are also action buttons on the left-most side of each row. The first button searches for the function name while second button shows the sandwich view of the function.

## Toolbar

The following table lists the features of the toolbar:

<!-- prettier-ignore-start -->

| Option | Description |
| ------ | ----------- |
| [Search](#search) | Use the search field to find functions with a particular name. All the functions in the flame graph that match the search will remain colored while the rest of the functions appear in gray. |
| Reset | Reset the flame graph back to its original state from a focus block or sandwich view. The reset icon is only displayed when the flame graph is in one of those two states. |
| [Change color scheme](#change-color-scheme) | Switch between **By value** and **By package name** to visually tie functions from the same package together. |
| Grouping | Expand or collapse all groups to show all instances of a function or show the function grouped. |
| Text align | Align text either to the left or to the right to show more important parts of the function name when it does not fit into the block. |
| Visualization picker | Choose to show only the flame graph, only table, or both at the same time. |

<!-- prettier-ignore-end -->

### Search

You can use the search field to find functions with a particular name. All the functions in the flame graph that match the search will remain colored while the rest of the functions are grayed-out.

*(image/video omitted)*

### Change color scheme

You can switch between **By value** and **By package name** to visually tie functions from the same package together.

*(image/video omitted)*

## Configuration options

*(shared doc include omitted -- see grafana.com docs)*

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### Standard options

**Standard options** in the panel editor pane let you change how field data is displayed in your visualizations.
When you set a standard option, the change is applied to all fields or series.
For more granular control over the display of fields, refer to [Configure field overrides](ref:configure-field-overrides).

You can customize the following standard options:

<!-- prettier-ignore-start -->

| Option | Description |
| ------ | ----------- |
| Unit | This option lets you choose which unit a field should use. For more information on unit options as well as creating custom units, refer to the [unit configuration documentation](ref:units). |
| Decimals | Specify the number of decimals Grafana includes in the rendered value. |

<!-- prettier-ignore-end -->

### Field overrides

*(shared doc include omitted -- see grafana.com docs)*
