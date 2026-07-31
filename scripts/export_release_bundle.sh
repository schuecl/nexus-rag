#!/usr/bin/env bash
# Issue #295: build the air-gap transfer bundle for a released version.
#
# Run on a connected host after release.yml has published X.Y.Z. Pulls the
# four first-party images and the chart from GHCR, verifies the chart pins
# exactly this version, and produces one tarball whose disconnected-side
# import procedure travels inside it (IMPORT.md). docs/releasing.md is the
# authoritative walkthrough.
#
# Usage: scripts/export_release_bundle.sh X.Y.Z [source-registry]
#   source-registry defaults to ghcr.io/schuecl/nexus-rag
set -euo pipefail

VERSION="${1:?usage: export_release_bundle.sh X.Y.Z [source-registry]}"
REGISTRY="${2:-ghcr.io/schuecl/nexus-rag}"
SERVICES=(ingestion-api ingestion-worker orchestration-mcp reranker-service)

case "$VERSION" in
  v*) echo "pass the bare version (X.Y.Z), not the git tag (${VERSION})" >&2; exit 1 ;;
esac

workdir="dist/nexus-rag-${VERSION}"
rm -rf "$workdir"
mkdir -p "$workdir/images"

echo ">> pulling images from ${REGISTRY}"
: > "$workdir/image-digests.txt"
for svc in "${SERVICES[@]}"; do
  ref="${REGISTRY}/${svc}:${VERSION}"
  docker pull "$ref"
  docker save -o "$workdir/images/${svc}-${VERSION}.tar" "$ref"
  docker inspect --format '{{index .RepoDigests 0}}' "$ref" \
    >> "$workdir/image-digests.txt"
done

echo ">> pulling chart"
helm pull "oci://${REGISTRY}/charts/nexus-rag" --version "$VERSION" -d "$workdir"

# The chart must pin exactly the images in this bundle -- catching a mismatch
# here beats discovering it disconnected, where fixing it means another trip
# across the boundary.
chart_versions=$(tar xzf "$workdir/nexus-rag-${VERSION}.tgz" -O \
  nexus-rag/values.yaml | grep -c "tag: \"${VERSION}\"" || true)
if [ "$chart_versions" -ne "${#SERVICES[@]}" ]; then
  echo "chart values pin ${chart_versions} image(s) at ${VERSION}," \
       "expected ${#SERVICES[@]} -- refusing to bundle a mismatched set" >&2
  exit 1
fi

cat > "$workdir/IMPORT.md" <<EOF
# Importing nexus-rag ${VERSION} (air-gapped side)

1. Verify integrity FIRST: \`sha256sum -c sha256sums.txt\`
2. Load images: \`for img in images/*.tar; do docker load -i "\$img"; done\`
3. Retag into the internal registry and push (bare names, same as the chart):
\`\`\`
for svc in ${SERVICES[*]}; do
  docker tag "${REGISTRY}/\${svc}:${VERSION}" "\${INTERNAL_REGISTRY}/\${svc}:${VERSION}"
  docker push "\${INTERNAL_REGISTRY}/\${svc}:${VERSION}"
done
\`\`\`
4. Deploy: \`helm install nexus-rag ./nexus-rag-${VERSION}.tgz --set global.imageRegistry=\${INTERNAL_REGISTRY}\`

image-digests.txt records the GHCR digests this bundle was built from, for
digest-pinning or post-transfer verification. Full runbook: docs/releasing.md.
EOF

echo ">> checksums"
(cd "$workdir" && sha256sum images/*.tar ./*.tgz image-digests.txt IMPORT.md > sha256sums.txt)

tar czf "dist/nexus-rag-${VERSION}-bundle.tar.gz" -C dist "nexus-rag-${VERSION}"
echo ">> bundle ready: dist/nexus-rag-${VERSION}-bundle.tar.gz"
