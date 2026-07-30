# Text


# Text

Text visualizations let you include text or HTML in your dashboards.
This can be used to add contextual information and descriptions or embed complex HTML.

For example, if you want to display important links on your dashboard, you can use a text visualization to add these links:

*(image/video omitted)*



Use a text visualization when you need to:

- Add important links or useful annotations.
- Provide instructions or guidance on how to interpret different panels, configure settings, or take specific actions based on the displayed data.
- Announce any scheduled maintenance or downtime that might impact your dashboards.

## Configuration options

*(shared doc include omitted -- see grafana.com docs)*

### Panel options

*(shared doc include omitted -- see grafana.com docs)*

### Text options

Use the following options to refine your text visualization.

<!-- prettier-ignore-start -->

| Option | Description |
| ------ | ----------- |
| Mode | Determines how embedded content appears. Choose from:<ul><li>**Markdown** - Formats the content as [markdown](https://en.wikipedia.org/wiki/Markdown).</li><li>**HTML** - Renders the content as [sanitized](https://github.com/grafana/grafana/blob/main/packages/grafana-data/src/text/sanitize.ts) HTML. If you require more direct control over the output, you can set the [disable_sanitize_html](ref:disable-sanitize-html) flag which enables you to directly enter HTML.</li><li>**Code** - Renders content inside a read-only code editor. [Variables](ref:variables) in the content are expanded for display.</li></ul><p>To allow embedding of iframes and other websites, you need set `allow_embedding = true` in your Grafana `config.ini` or environment variables, depending on your deployment.</p> |
| Content | Enter the text to display. The content supports Markdown, HTML, or code, depending on **Mode**. Dashboard variables are interpolated in the content. |
| Language | When you choose **Code** as your text mode, select an appropriate language to apply syntax highlighting to the embedded text. Choose from JSON, YAML, XML, TypeScript, SQL, Go, Markdown, HTML, or Plain text. The default is Plain text. |
| Show line numbers | Displays line numbers in the panel preview when you choose **Code** as your text mode. |
| Show mini map | Displays a small outline of the embedded text in the panel preview when you choose **Code** as your text mode. |

<!-- prettier-ignore-end -->
