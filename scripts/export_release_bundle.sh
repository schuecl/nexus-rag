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
#   requires jq (verifies each saved image tar is self-contained -- see
#   verify_image_tar below)
set -euo pipefail

VERSION="${1:?usage: export_release_bundle.sh X.Y.Z [source-registry]}"
REGISTRY="${2:-ghcr.io/schuecl/nexus-rag}"
SERVICES=(ingestion-api ingestion-worker orchestration-mcp reranker-service)

# `docker save` on a daemon backed by the containerd image-store snapshotter
# has been observed here to report success while writing a tar that contains
# only its own top-level manifest blob -- every layer and the config blob
# the manifest references are silently absent, and `docker load` doesn't
# catch it because it resolves those blobs from the daemon's local content
# store instead of the tar. So verify the tar itself: for every blob its OCI
# manifest references, confirm that blob is actually present in the archive
# at the expected size.
verify_image_tar() {
  local tar_path="$1" svc="$2"
  local manifest_digest oci_manifest listing
  manifest_digest=$(tar xf "$tar_path" -O index.json | jq -r '.manifests[0].digest' | sed 's/^sha256://')
  oci_manifest=$(tar xf "$tar_path" -O "blobs/sha256/${manifest_digest}")
  listing=$(tar tvf "$tar_path")
  while IFS=$'\t' read -r digest size; do
    local path="blobs/sha256/${digest#sha256:}"
    local actual
    actual=$(awk -v p="$path" '$NF==p {print $3}' <<<"$listing")
    if [ -z "$actual" ]; then
      echo "export verification failed for ${svc}: ${tar_path} is missing ${path}" \
           "(referenced by the image manifest, expected ${size} bytes) -- check whether" \
           "this daemon's containerd image-store snapshotter is dropping blobs from" \
           "docker save (docker info | grep snapshotter)" >&2
      return 1
    fi
    if [ "$actual" -ne "$size" ]; then
      echo "export verification failed for ${svc}: ${tar_path}'s ${path} is ${actual} bytes," \
           "manifest expects ${size} -- tar is truncated" >&2
      return 1
    fi
  done < <(jq -r '[.config, .layers[]] | .[] | "\(.digest)\t\(.size)"' <<<"$oci_manifest")
}

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
  docker inspect --format '{{index .RepoDigests 0}}' "$ref" \
    >> "$workdir/image-digests.txt"

  docker save -o "$workdir/images/${svc}-${VERSION}.tar" "$ref"
  verify_image_tar "$workdir/images/${svc}-${VERSION}.tar" "$svc"
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
