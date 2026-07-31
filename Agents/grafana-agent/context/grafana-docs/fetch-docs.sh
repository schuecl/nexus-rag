#!/usr/bin/env bash
# Fetches Grafana's official panels/visualizations documentation as markdown
# from the grafana/grafana repo, pinned to the tag matching the deployed
# Grafana (docker-compose.yml's grafana image), lightly cleans Hugo shortcodes,
# and organizes it under this directory.
#
# Re-run after a Grafana upgrade: ./fetch-docs.sh v<new-version>
# Needs internet (run from a connected enclave; commit the output for air-gap).
set -euo pipefail

TAG="${1:-v13.1.0}"   # keep in sync with grafana/grafana image tag in docker-compose.yml
BASE="https://raw.githubusercontent.com/grafana/grafana/${TAG}/docs/sources/visualizations/panels-visualizations"
HERE="$(cd "$(dirname "$0")" && pwd)"

VISUALIZATIONS=(
  alert-list annotations bar-chart bar-gauge candlestick canvas dashboard-list
  flame-graph gauge geomap heatmap histogram logs news node-graph pie-chart
  stat state-timeline status-history table text time-series traces trend xy-chart
)
CONFIGURATION=(
  panel-overview panel-editor-overview panel-inspector
  configure-panel-options configure-standard-options configure-overrides
  configure-thresholds configure-value-mappings configure-legend
  configure-data-links configure-tooltips
)
# query-transform-data is a directory of subpages at 13.x, fetched separately below
QUERY_DOCS=(calculation-types expression-queries transform-data troubleshoot-queries)

mkdir -p "$HERE/visualizations" "$HERE/configuration"

clean() {  # strip/downgrade Hugo shortcodes so the files read as plain markdown
  python3 - "$1" <<'EOF'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
# front matter: keep the title as an H1, drop the rest
m = re.match(r"^---\n(.*?)\n---\n", t, re.S)
if m:
    title = re.search(r"^title:\s*(.+)$", m.group(1), re.M)
    t = (f"# {title.group(1).strip()}\n\n" if title else "") + t[m.end():]
t = re.sub(r"\{\{<\s*(figure|video-embed|youtube)\b.*?>\}\}", "*(image/video omitted)*", t, flags=re.S)
t = re.sub(r"\{\{<\s*admonition[^>]*>\}\}", "> **Note:**", t)
t = re.sub(r"\{\{<\s*/admonition\s*>\}\}", "", t)
t = re.sub(r"\{\{<\s*relref\s+\"([^\"]+)\"\s*>\}\}", r"\1", t)
t = re.sub(r"\{\{<\s*docs/shared\b.*?>\}\}", "*(shared doc include omitted -- see grafana.com docs)*", t, flags=re.S)
t = re.sub(r"\{\{[<%].*?[%>]\}\}", "", t, flags=re.S)  # anything else
open(p, "w", encoding="utf-8").write(t)
EOF
}

fetch() {  # $1 = source subpath, $2 = destination file
  local url="$BASE/$1/index.md" out="$2"
  if curl -sSf "$url" -o "$out"; then clean "$out"; echo "ok    $out"
  else echo "MISS  $url" >&2; rm -f "$out"; fi
}

for v in "${VISUALIZATIONS[@]}"; do fetch "visualizations/$v" "$HERE/visualizations/$v.md"; done
for c in "${CONFIGURATION[@]}"; do fetch "$c" "$HERE/configuration/$c.md"; done
for q in "${QUERY_DOCS[@]}"; do fetch "query-transform-data/$q" "$HERE/configuration/query-$q.md"; done
curl -sSf "$BASE/query-transform-data/_index.md" -o "$HERE/configuration/query-overview.md" \
  && clean "$HERE/configuration/query-overview.md" && echo "ok    query-overview.md" || true
curl -sSf "$BASE/visualizations/_index.md" -o "$HERE/visualizations/_overview.md" \
  && clean "$HERE/visualizations/_overview.md" && echo "ok    _overview.md" || true

echo "done: $(find "$HERE" -name '*.md' ! -name 'README.md' | wc -l) files at tag $TAG"
