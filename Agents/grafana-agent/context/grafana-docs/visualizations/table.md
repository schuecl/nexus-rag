# Table


# Table

Tables are a highly flexible visualization designed to display data in columns and rows.
The table visualization can take multiple datasets and provide the option to switch between them.
With this versatility, it's the preferred visualization for viewing multiple data types, aiding in your data analysis needs.

![Basic table visualization](/media/docs/grafana/panels-visualizations/screenshot-basic-table-v11.3.png)

You can use a table visualization to show datasets such as:

- Common database queries like logs, traces, metrics
- Financial reports
- Customer lists
- Product catalogs

Any information you might want to put in a spreadsheet can often be best visualized in a table.

Tables also provide different styles to visualize data inside the table cells, such as colored text and cell backgrounds, gauges, sparklines, data links, JSON code, and images.

> **Note:**
Annotations and alerts are not currently supported for tables.


## Configure a table visualization

The following video provides a visual walkthrough of the options you can set in a table visualization.
If you want to see a configuration in action, check out the video:

*(image/video omitted)*



## Supported data formats

The table visualization supports any data that has a column-row structure.

> **Note:**
If you’re using a cell type such as sparkline or JSON, the data requirements may differ in a way that’s specific to that type. For more info refer to [Cell type](#cell-type).


### Example

This example shows a basic dataset in which there's data for every table cell:

```csv
Column1, Column2, Column3
value1 , value2 , value3
value4 , value5 , value6
value7 , value8 , value9
```

If a cell is missing or the table column-row structure is not complete, as in the following example, the table visualization won’t display any of the data:

```csv
Column1, Column2, Column3
value1 , value2 , value3
gap1   , gap2
value4 , value5 , value6
```

If you need to hide columns, you can do so using [data transformations](ref:data-transformation), [field overrides](#field-overrides), or by [building a query](ref:build-query) that returns only the needed columns.

## Column filtering

You can temporarily change how column data is displayed using column filtering.
For example, you can show or hide specific values.

### Turn on column filtering

To turn on column filtering, follow these steps:

1. In Grafana, navigate to the dashboard with the table with the columns that you want to filter.
1. Hover over any part of the panel to which you want to add the link to display the actions menu on the top right corner.
1. Click the menu and select **Edit**.
1. In the panel editor pane, expand the **Table** options section.
1. Toggle on the [**Column filter** switch](#table-options).

A filter icon (funnel) appears next to each column title.

*(image/video omitted)*

### Filter column values

To filter column values, follow these steps:

1. Click the filter icon (funnel) next to a column title.

   Grafana displays the filter options for that column.

   *(image/video omitted)*

1. Click the checkbox next to the values that you want to display or click **Select all**.
1. Enter text in the search field at the top to show those values in the display so that you can select them rather than scroll to find them.
1. Choose from several operators to display column values:
   - **Contains** - Matches a regex pattern (operator by default).
   - **Expression** - Evaluates a boolean expression. The character `$` represents the column value in the expression (for example, "$ >= 10 && $ <= 12").
   - The typical comparison operators: `=`, `!=`, `<`, `<=`, `>`, `>=`.

1. Click the checkbox above the **Ok** and **Cancel** buttons to add or remove all displayed values to and from the filter.

### Clear column filters

Columns with filters applied have a blue filter displayed next to the title.

*(image/video omitted)*

To remove the filter, click the blue filter icon and then click **Clear filter**.

<!-- vale Grafana.WordList = NO -->
<!-- vale Grafana.Spelling = NO -->

### Apply filters from the table

In tables, you can apply filters directly from the visualization with one click.

To display the filter icons, hover your cursor over the cell that has the value for which you want to filter:

*(image/video omitted)*

For more information about applying filters this way, refer to [Dashboard drilldown with filters](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/visualizations/dashboards/build-dashboards/filter-group-by/#dashboard-drilldown-with-filters).

<!-- vale Grafana.Spelling = YES -->
<!-- vale Grafana.WordList = YES -->

## Sort columns

Click a column title to change the sort order from default to descending to ascending.
Each time you click, the sort order changes to the next option in the cycle.
You can sort multiple columns by holding the `Cmd` or `Ctrl` key
and clicking the column name.

*(image/video omitted)*

## Dataset selector

If the data queried contains multiple datasets, a table displays a drop-down list at the bottom, so you can select the dataset you want to visualize.
This option is only available when you're editing the panel.

*(image/video omitted)*

## Nested tables

> **Note:**
The new transformation editor for nested tables and the nested table overrides feature are currently in public preview. Grafana Labs offers limited support, and breaking changes might occur prior to the feature being made generally available.

To use these features, enable the `groupToNestedTableV2` and `nestedFramesFieldOverrides` feature toggles in your Grafana configuration file or contact Support.


A table can display sub-tables inside expandable rows. You can add these nested tables using the [Group to nested tables transformation](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/panels-visualizations/query-transform-data/transform-data/#group-to-nested-tables), which groups rows by one or more fields, and can summarize nested row data by applying calculations.

*(image/video omitted)*

Click the expand icon on a row to toggle the visibility of its nested table:

*(image/video omitted)*

To sort nested and top-level rows in nested tables, click a column title to change the sort order from default to descending to ascending.
Each time you click, the sort order changes to the next option in the cycle.
You can sort multiple columns by holding the `Cmd` or `Ctrl` key and clicking the column name.

*(image/video omitted)*

To control the display of fields inside a nested table&mdash;for example, to apply thresholds, units, or a different cell type&mdash;use [field overrides](#field-overrides) with the **Target fields** option set to **Nested**.
For more information, refer to [Apply overrides to nested table fields](#apply-overrides-to-nested-table-fields).

## Configuration options

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### Table options

<!-- prettier-ignore-start -->
| Option               | Description                                               |
| -------------------- | --------------------------------------------------------- |
| Show table header    | Show or hide column names imported from your data source. |
| Frozen columns       | Freeze columns starting from the left side of the table. Enter a value to set how many columns are frozen. |
| Cell height          | Set the height of the cell. Choose from **Small**, **Medium**, or **Large**. |
| Max row height       | Define the maximum height for a row in the table. This can be useful when **Wrap text** is enabled for one or more columns. |
| Enable pagination    | Toggle the switch to control how many table rows are visible at once. When switched on, the page size automatically adjusts to the height of the table. This option doesn't affect queries. |
| Minimum column width | Define the lower limit of the column width, in pixels. By default, the minimum width of the table column is 150 pixels. For small-screen devices, such as mobile phones or tablets, reduce the value to `50` to allow table-based panels to render correctly in dashboards. |
| Column width         | Define a column width, in pixels, rather than allowing the width to be set automatically. By default, Grafana calculates the column width based on the table size and the minimum column width. |
| Column alignment     | Set how Grafana should align cell contents. Choose from: **Auto** (default), **Left**, **Center**, or **Right**.  |
| Column filter        | Temporarily change how column data is displayed. For example, show or hide specific values. For more information, refer to [Column filtering](#column-filtering). |
| Wrap text            | Enables text wrapping for cell content. |
| Wrap header text     | Enables text wrapping for column headers. |
<!-- prettier-ignore-end -->

### Table footer options

The table footer displays the results of calculations (and reducer functions) on fields.
The footer is only displayed after you select an option in the **Calculation** drop-down list:

*(image/video omitted)*

There are several calculations you can choose from including minimum, maximum, first, last, and total.
For the full list of options, refer to [Calculations](ref:calculations).

In the table footer:

- You can apply multiple calculations at once.
- The calculations and reducer functions apply to all fields in the table, by default. To control which fields have a calculation or function applied, add the table footer in an override instead.
- If you enable a mathematical function for a non-numeric field, nothing for that function is displayed for that field.

In the following image, multiple calculations&mdash;**Mean**, **Max**, and **Last**&mdash;have been applied:

*(image/video omitted)*

You can also see in the previous image that the mathematical functions, **Mean** and **Max**, haven't been applied to the text field in the table.
Only the **Last** function has been applied to that field.

> **Note:**
Calculations applied to cell types like **Markdown + HTML** might have unexpected results.


### Cell options

Cell options allow you to control how data is displayed in a table.
The options are differ based on the cell type that you select and are outlined within the descriptions of each cell type.
The following table provides short descriptions for each cell type and links to a longer description and the cell type options.

#### Cell type

By default, Grafana automatically chooses display settings.
You can override these settings by choosing one of the following cell types to control the default display for all fields.
Additional configuration is available for some cell types.

If you want to apply a cell type to only some fields instead of all fields, you can do so using the **Cell options > Cell type** field override.

<!-- prettier-ignore-start -->
| Cell type                                 | Description                                                                                                                |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| [Auto](#auto)                             | A basic text and number cell. |
| [Colored text](#colored-text)             | If thresholds, value mappings, or color schemes are set, then the cell text is displayed in the appropriate color. |
| [Colored background](#colored-background) | If thresholds, value mappings, or color schemes are set, then the cell background is displayed in the appropriate color. |
| [Data links](#data-links)                 | The cell text reflects the titles of the configured data links.|
| [Gauge](#gauge)                           | Values are displayed as a horizontal bar gauge. You can set the [Gauge display mode](#gauge-display-mode) and the [Value display](#value-display) options. |
| [Sparkline](#sparkline)                   | Shows values rendered as a sparkline. |
| [JSON View](#json-view)                   | Shows values formatted as code. |
| [Pill](#pill)                             | Displays each item in a comma-separated string in a colored block. |
| [Markdown + HTML](#markdown--html)        | Displays rich markdown or HTML content. |
| [Image](#image)                           | Displays an image when the value is a URL or a base64 encoded image. |
| [Actions](#actions)                       | The cell displays a button that triggers a basic, unauthenticated API call when clicked. |
<!-- prettier-ignore-end -->

#### Auto

This is a basic text and number cell.

It has the following cell options:

*(shared doc include omitted -- see grafana.com docs)*

#### Colored text

If thresholds, value mappings, or color schemes are set, the cell text is displayed in the appropriate color.

![Table with colored text cell type](/media/docs/grafana/panels-visualizations/screenshot-table-colored-text-v11.3-2.png)

The colored text cell type has the following options:

*(shared doc include omitted -- see grafana.com docs)*

#### Colored background

If thresholds, value mappings, or color schemes are set, the cell background is displayed in the appropriate color.

![Table with colored background cell type](/media/docs/grafana/panels-visualizations/screenshot-table-colored-bkgrnd-v11.3-2.png)

You can also set background cell color by row:

![Table with background cell color applied to row](/media/docs/grafana/panels-visualizations/screenshot-table-colored-row-v11.3.png)

The colored background cell type has the following options:

<!-- prettier-ignore-start -->
| Option | Description |
| ------ | ----------- |
| Background display mode | Choose between **Basic** and **Gradient**. |
| Apply to entire row | Toggle the switch on to apply the background color that's configured for the cell to the whole row. |
| Cell value inspect | <p>Enables value inspection from table cells. When the switch is toggled on, clicking the inspect icon in a cell opens the **Inspect value** drawer which contains two tabs: **Plain text** and **Code editor**.</p><p>Grafana attempts to automatically detect the type of data in the cell and opens the drawer with the associated tab showing. However, you can switch back and forth between tabs.</p> |
| Tooltip from field | Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip. For more information, refer to the [Tooltip from field](#tooltip-from-field). |
| Styling from field | Toggle on the **Styling from field** switch to apply the styling from another field (or column). The referenced field must contain CSS properties formatted in JSON object syntax (for example, `{"name":"John"}`). For more information, refer to the [Styling from field](#styling-from-field). |
<!-- prettier-ignore-end -->

<!-- The cell value inspect, tooltip from field, and styling from field descriptions above should be copied from docs/sources/shared/visualizations/cell-options.md -->

#### Data links

If you've configured data links, when the cell type is **Auto**, the cell text becomes clickable.
If you change the cell type to **Data links**, the cell text reflects the titles of the configured data links. To control the application of data link text more granularly, use a **Cell option > Cell type > Data links** field override.

#### Gauge

With this cell type, cells can be displayed as a graphical gauge, with several different presentation types.

The gauge cell type has the following options:

<!-- prettier-ignore-start -->
| Option             | Description                                                                                                    |
| ------------------ | -------------------------------------------------------------------------------------------------------------- |
| Gauge display mode | Controls the type of gauge used. For more information, refer to the [Gauge display mode](#gauge-display-mode). |
| Value display      | Controls how the value is displayed. For more information, refer to the [Value display](#value-display). |
| Tooltip from field | Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip. For more information, refer to [Tooltip from field](#tooltip-from-field). |
| Styling from field | Toggle on the **Styling from field** switch to apply the styling from another field (or column). The referenced field must contain CSS properties formatted in JSON object syntax (for example, `{"name":"John"}`). For more information, refer to the [Styling from field](#styling-from-field). |
<!-- prettier-ignore-end -->

> **Note:**
The maximum and minimum values of the gauges are configured automatically from the smallest and largest values in your whole dataset.
If you don't want the max/min values to be pulled from the whole dataset, you can configure them for each column using [field overrides](#field-overrides).


##### Gauge display mode

You can set three gauge display modes.

<!-- prettier-ignore-start -->
| Option | Description |
| ------ | ----------- |
| Basic | Shows a simple gauge with the threshold levels defining the color of gauge. *(image/video omitted)* |
| Gradient | The threshold levels define a gradient. *(image/video omitted)* |
| Retro LCD | The gauge is split up in small cells that are lit or unlit. *(image/video omitted)* |
<!-- prettier-ignore-end -->

##### Value display

Labels displayed alongside of the gauges can be set to be colored by value, match the theme text color, or be hidden.

<!-- prettier-ignore-start -->
| Option | Description |
| ------ | ----------- |
| Value color | Labels are colored by value. *(image/video omitted)* |
| Text color | Labels match the theme text color. *(image/video omitted)* |
| Hidden | Labels are hidden. *(image/video omitted)* |
<!-- prettier-ignore-end -->

#### Sparkline

This cell type shows values rendered as a sparkline.
To show sparklines on data with multiple time series, use the [Time series to table transformation](ref:time-series-to-table-transformation) to process it into a format the table can show.

You can color sparklines using thresholds by setting the field's color scheme to **From thresholds (by value)** in the [standard color scheme options](ref:color-scheme), using an override. When thresholds are defined, the sparkline automatically uses the **Scheme** gradient mode to reflect threshold levels along the line.

![Table using sparkline cell type](/media/docs/grafana/panels-visualizations/screenshot-table-as-sparkline-v11.3.png)

The sparkline cell type options are described in the following table.
For more detailed information about all of the sparkline styling options (except **Hide value**), refer to the [time series graph styles documentation](ref:graph-styles).

<!-- prettier-ignore-start -->
| Option              | Description                                                                |
| ------------------- | --------------------------------------------------------------------------------------------- |
| Hide value          | Toggle the switch on or off to display or hide the cell value on the sparkline. |
| Style               | Choose whether to display your time-series data as **Lines**, **Bars**, or **Points**. You can use overrides to combine multiple styles in the same graph. |
| Line interpolation  | How the graph interpolates the series line. Choose from:<ul><li>**Linear** - Points are joined by straight lines.</li><li>**Smooth** - Points are joined by curved lines that smooths transitions between points.</li><li>**Step before** - The line is displayed as steps between points. Points are rendered at the end of the step.</li><li>**Step after** - The line is displayed as steps between points. Points are rendered at the beginning of the step.</li></ul> |
| Line width          | The thickness of the series lines or the outline for bars using the **Line width** slider. |
| Fill opacity        | The series area fill color using the **Fill opacity** slider. |
| Gradient mode       | Gradient mode controls the gradient fill, which is based on the series color. Gradient appearance is influenced by the **Fill opacity** setting. To change the color, use the standard color scheme field option. For more information, refer to [Color scheme](ref:color-scheme). Choose from:<ul><li>**None** - No gradient fill. This is the default setting.</li><li>**Opacity** - An opacity gradient where the opacity of the fill increases as y-axis values increase.</li><li>**Hue** - A subtle gradient that's based on the hue of the series color.</li><li>**Scheme** - The sparkline line color reflects the configured threshold levels. Automatically applied when the color scheme is set to **From thresholds (by value)** and thresholds are defined.</li></ul> |
| Line style          | Choose from:<ul><li>**Solid**</li><li>**Dash** - Select the length and gap for the line dashes. Default dash spacing is 10, 10.</li><li>**Dots** - Select the gap for the dot spacing. Default dot spacing is 0, 10.</li></ul> |
| Connect null values | How null values, which are gaps in the data, appear on the graph. Null values can be connected to form a continuous line or set to a threshold above which gaps in the data are no longer connected. Choose from:<ul><li>**Never** - Time series data points with gaps in the data are never connected.</li><li>**Always** - Time series data points with gaps in the data are always connected.</li><li>**Threshold** - Specify a threshold above which gaps in the data are no longer connected. This can be useful when the connected gaps in the data are of a known size or within a known range, and gaps outside this range should no longer be connected.</li></ul> |
| Show points         | Whether to show data points to lines or bars. Choose from: <ul><li>**Auto** - Grafana determines a point's visibility based on the density of the data. If the density is low, then points appear.</li><li>**Always** - Show the points regardless of how dense the dataset is.</li><li>**Never** - Don't show points.</li></ul> |
| Point size          | Set the size of the points, from 1 to 40 pixels in diameter. |
| Bar alignment       | Set the position of the bar relative to a data point. |
| Tooltip from field | Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip. For more information, refer to [Tooltip from field](#tooltip-from-field). |
| Styling from field | Toggle on the **Styling from field** switch to apply the styling from another field (or column). The referenced field must contain CSS properties formatted in JSON object syntax (for example, `{"name":"John"}`). For more information, refer to the [Styling from field](#styling-from-field). |
<!-- prettier-ignore-end -->

#### JSON View

This cell type shows values formatted as code.
If a value is an object, the JSON object will appear on hover.

*(image/video omitted)*

It has the following cell options:

*(shared doc include omitted -- see grafana.com docs)*

#### Pill

The **Pill** cell type displays each item in a comma-separated string in a colored block.

*(image/video omitted)*

The colors applied to each piece of text are maintained throughout the table.
For example, if the word "test" is first displayed in a red pill, it will always be displayed in a red pill.

The following data formats are supported for the pill cell type:

- Comma-separated values (`cows,chickens,goats`)
- JSON arrays of uniform (`(["cows","chickens","goats"])`) or mixed (`[1,2,3,"foo",42,"bar"]`) types

Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip.
For more information, refer to [Tooltip from field](#tooltip-from-field).

#### Markdown + HTML

The **Markdown + HTML** cell type displays rich Markdown or HTML content, rendered using the
[GitHub-Flavored Markdown](https://github.github.com/gfm/) spec. This is useful if you need to display
customized, pre-formatted information alongside tabular data, such as formatted strings,
lists of links, or other dynamic cases.

For this cell type, you can toggle the **Dynamic height** switch, which allows the cell to resize
dynamically based on the cell content. If you use dynamic height, we strongly recommend that you
also toggle on **Pagination** to avoid performance issues in larger tables, since enabling
Dynamic height disables table virtualization.

By default, the HTML rendered is sanitized, and un-sanitized HTML can only be rendered
in these cells if the [`disable_sanitize_html`](https://grafana.com/docs/grafana/<GRAFANA_VERSION>/setup-grafana/configure-grafana/#disable_sanitize_html) option is set to true for your Grafana instance.

Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip.
For more information, refer to [Tooltip from field](#tooltip-from-field).

*(image/video omitted)*

#### Image

If you have a field value that is an image URL or a base64 encoded image, this cell type displays it as an image.

![Table with image cell type](/media/docs/grafana/panels-visualizations/screenshot-table-cell-image-v11.3.png)

It has the following options:

<!-- prettier-ignore-start -->
| Option             | Description                                                                                                                   |
| ------------------ |  ---------------------------------------------------------------------------------------------------------------------------- |
| Alt text           | Set the alternative text of an image. The text will be available for screen readers and in cases when images can't be loaded. |
| Title text         | Set the text that's displayed when the image is hovered over with a cursor. |
| Tooltip from field | Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip. For more information, refer to [Tooltip from field](#tooltip-from-field). |
<!-- prettier-ignore-end -->

#### Actions

Actions add a button to a cell that triggers a basic, unauthenticated API call when clicked. Configure actions from **Data links and actions** or with field overrides.

<!-- prettier-ignore-start -->
| Option             | Description  |
| ------------------ | ------------ |
| Endpoint           | Enter the endpoint URL. |
| Method             | Choose from **GET**, **POST**, and **PUT**. |
| Content-Type       | Select an option in the drop-down list. Choose from: JSON, Text, JavaScript, HTML, XML, and x-www-form-urlencoded. |
| Query parameters   | Enter as many **Key**, **Value** pairs as you need. |
| Header parameters  | Enter as many **Key**, **Value** pairs as you need. |
| Payload            | Enter the body of the API call. |
| Tooltip from field | Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip. For more information, refer to [Tooltip from field](#tooltip-from-field). |
<!-- prettier-ignore-end -->

#### Tooltip from field

Toggle on the **Tooltip from field** switch to use the values from another field (or column) in a tooltip.

When you toggle the switch on, you can select from a drop-down list any of the fields in the table to be used as the source of the tooltip content.
All table fields are included in the drop-down list, whether visible or hidden.

When a tooltip from a field has been added to a cell, a chip is displayed in the top-right or top-left corner of the cell:

*(image/video omitted)*

Hover your mouse over the chip to display the tooltip.

When you toggle on the switch, the **Tooltip placement** option, which controls where the tooltip box opens upon hover, is also displayed.
Select one of the following options: **Auto**, **Top**, **Right**, **Bottom**, and **Left**.

The content of the tooltip is determined by the values of the source field and can't be directly edited.
However, you can affect the display of the value using overrides like value mappings, as shown in the [Example: Tooltip from field with value mappings](#example-tooltip-from-field-with-value-mappings) section.

While you can turn on this option under **Cell options** and have it applied to all cells in the table, it's typically used as an override on a sub-set of cells instead.
This is demonstrated in the example in the following section.

##### Example: Tooltip from field using overrides

The following table has five visible fields (columns) as well as a hidden field called "Info":

*(image/video omitted)*

- The "Info" field is hidden using the **Table > Hide in table** override property.
- The following overrides have been applied to the "Short text" field:
  - The values from the "Info" field are used as tooltip text for the "Short text" cells using the **Cell options > Tooltip from field** override property.
  - The **Cell options > Tooltip placement** override property is set to control the placement of the tooltip.

*(image/video omitted)*

Now, when you hover the cursor over the chip in the "Short text" column, the corresponding values from the "Info" column appear in the tooltip:

*(image/video omitted)*

##### Example: Tooltip from field with value mappings

While the content of the tooltip is determined by the values of the source field and can't be directly edited, you can use field overrides on the source field to manipulate the display of that value.

For example, if the "Info" column is being used as the source field for the tooltip values, you could set up a value mapping.
In this case, the value "up" is mapped to the word "Good":

*(image/video omitted)*

Now, when you hover the cursor over the chip in the "Short text" cell, the mapped value appears in the tooltip:

*(image/video omitted)*

You can use all field overrides to affect the display of the tooltip.
For example, the **Table > Column width** or **Cell options > Cell type** overrides can change the cell width or visual display of the data.

#### Styling from field

Toggle on the **Styling from field** switch to apply the styling from another field (or column).
The referenced field must contain [CSS properties](https://developer.mozilla.org/en-US/docs/Web/API/CSSStyleProperties) formatted in JSON object syntax. For example:

```JSON
{"marginLeft":12, "text-decoration": "underline"}
```

While you can turn on this option under **Cell options** and have it applied to all cells in the table, it's typically used as an override on a sub-set of cells instead.
This is demonstrated in the following example.

The following table has six visible fields (columns) as well as a hidden field called "Style":

*(image/video omitted)*

- The "Style" field has JSON objects with CSS properties. (Note that they are formatted for use in CSV format in this example.)
- The "Style" field is hidden using the **Table > Hide in table** override property.
- The "Info" field is using the **Cell options > Styling from field** override property with the "Style" field as the source.

The following image shows the "Info" field with the styling from the "Style" field applied:

*(image/video omitted)*

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

#### Apply overrides to nested table fields

> **Note:**
The new transformation editor for nested tables and the nested table overrides feature are currently in public preview. Grafana Labs offers limited support, and breaking changes might occur prior to the feature being made generally available.

To use these features, enable the `groupToNestedTableV2` and `nestedFramesFieldOverrides` feature toggles in your Grafana configuration file or contact Support.


By default, field overrides apply only to columns in the parent table.
To target columns inside a nested table, set the **Target fields** option on the override to **Nested**:

*(image/video omitted)*

All standard override properties&mdash;including thresholds, value mappings, units, data links, and cell type&mdash;apply the same way to nested fields.

*(image/video omitted)*
