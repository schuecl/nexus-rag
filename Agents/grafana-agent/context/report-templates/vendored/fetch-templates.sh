#!/usr/bin/env bash
# Vendors a curated set of Typst Universe packages/templates from the official
# typst/packages repo into vendored/preview/<name>/<version>/ -- the layout
# Typst's resolver expects, so `#import "@preview/<name>:<version>"` works
# fully offline with --package-path / TYPST_PACKAGE_PATH pointed here.
#
# Typst packages have NO dependency manifest -- imports live in the source --
# so after fetching the requested set this script scans every vendored .typ
# for `@preview/<name>:<ver>` imports and fetches the transitive closure.
#
# "latest" resolves to the highest version present in the repo AT FETCH TIME
# and is then a pin (recorded on disk); re-run deliberately to upgrade.
# Needs internet once; commit the output for air-gapped use.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# name=version ("latest" allowed). Curated for the grafana-agent use case:
REQUESTED=(
  # report/document templates
  "ilm=latest" "basic-report=latest" "modern-technique-report=latest"
  "charged-ieee=latest" "letter-pro=latest"
  # figures, plots, diagrams, timelines -- charts of query data in reports
  "cetz=latest" "cetz-plot=latest" "lilaq=latest" "fletcher=latest" "timeliney=latest"
  # writing aids
  "unify=latest" "glossarium=latest"
  # presentations (ops reviews)
  "touying=latest" "diatypst=latest"
  # academic papers (the research writeup itself)
  "arkheion=latest" "starter-journal-article=latest"
)

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/typst/packages.git "$TMP/packages" 2>/dev/null
REPO="$TMP/packages"

latest_version() {  # highest semver dir under packages/preview/$1
  git -C "$REPO" ls-tree --name-only HEAD "packages/preview/$1/" 2>/dev/null \
    | awk -F/ 'NF>=4 {print $4}' | sort -V | tail -1
}

fetch_one() {  # $1=name $2=version -> 0 if vendored (or already present)
  local name="$1" ver="$2"
  [ -d "$HERE/preview/$name/$ver" ] && return 0
  git -C "$REPO" sparse-checkout add "packages/preview/$name/$ver" 2>/dev/null || true
  local src="$REPO/packages/preview/$name/$ver"
  [ -d "$src" ] || { echo "MISS  $name:$ver" >&2; return 1; }
  mkdir -p "$HERE/preview/$name/$ver"
  cp -r "$src/." "$HERE/preview/$name/$ver/"
  echo "ok    $name:$ver"
}

# Pass 1: requested set
for req in "${REQUESTED[@]}"; do
  name="${req%%=*}"; ver="${req##*=}"
  [ "$ver" = "latest" ] && ver="$(latest_version "$name")"
  [ -n "$ver" ] && fetch_one "$name" "$ver" || echo "MISS  $name (no versions)" >&2
done

# Pass 2..N: transitive closure of @preview imports found in vendored sources
while :; do
  mapfile -t deps < <(
    grep -rhoE '@preview/[a-z0-9-]+:[0-9]+\.[0-9]+\.[0-9]+' "$HERE/preview" \
      --include='*.typ' 2>/dev/null | sed 's|@preview/||' | sort -u
  )
  added=0
  for dep in "${deps[@]:-}"; do
    [ -z "$dep" ] && continue
    name="${dep%%:*}"; ver="${dep##*:}"
    [ -d "$HERE/preview/$name/$ver" ] && continue
    fetch_one "$name" "$ver" && added=1 || true
  done
  [ "$added" = "0" ] && break
done

echo "done: $(find "$HERE/preview" -name typst.toml | wc -l) packages vendored, $(du -sh "$HERE" | cut -f1) total"
echo "licenses:"; find "$HERE/preview" -name typst.toml -exec grep -H '^license' {} \; | sed "s|$HERE/preview/||;s|/typst.toml||"
