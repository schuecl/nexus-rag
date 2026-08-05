# Releasing

How this stack is versioned, how a release is cut, and how released artifacts
reach an air-gapped MPNexus environment. Introduced by #295; the design
rationale lives in that issue's discussion.

Confidence label, per this repo's convention: **validated live** as of v0.1.0
(2026-07-31, run
[30626111804](https://github.com/schuecl/nexus-rag/actions/runs/30626111804),
8m42s, green) — tag push through image/chart publish through GitHub Release
creation ran end to end exactly as documented below. The air-gapped export
(`scripts/export_release_bundle.sh`) has since been confirmed working too,
pulling and bundling the published v0.1.0 images and chart -- that
confirmation predates #399's finding that `docker save` can silently produce
an incomplete tar on a containerd-snapshotter-backed daemon, and did not
catch it (a same-host `docker load` check, which #399's fix replaced,
false-positives in exactly that case). Anything that turns out to differ in
practice gets corrected here in the same PR that fixes it.

## Versioning scheme

**One SemVer version, lockstep, for the whole stack.** A single `X.Y.Z`
covers:

| Location | Field |
|---|---|
| `helm/nexus-rag/Chart.yaml` | `version` **and** `appVersion` (equal by rule) |
| `services/{common,ingestion-api,ingestion-worker,orchestration-mcp,reranker-service}/pyproject.toml` | `[project].version` |
| `helm/nexus-rag/values.yaml` | the four first-party `image.tag`s |

Why lockstep: the five packages are one deployable unit — everything depends
on `services/common`, compose and the chart deploy them as a set, and the
golden-query gate validates the *set*. Independent per-service versions would
create a compatibility matrix with no consumer. Why chart `version` ==
`appVersion`: they were conflated before this process existed; keeping them
equal by rule removes the "which one do I bump" failure mode. Chart-only fixes
ride the next stack release.

Pre-1.0 semantics: minor releases may break; patch releases are fixes only.
Bump **minor** for anything adding or changing behavior, **patch** for
fix-only releases. (Post-1.0, standard SemVer.)

`scripts/check_version_consistency.py` enforces file-to-file agreement on
every PR (ci.yml `pin-check` job); `release.yml` refuses a tag that disagrees
with the files. Files are the source of truth — the tag must match them,
never the reverse; no version is derived at build time.

## Changelog

`CHANGELOG.md`, Keep a Changelog format, curated by hand (this repo's commit
convention — one squashed descriptive commit per PR — reads well enough that
generated changelogs would be curation with extra steps; Conventional Commits
was considered in #295 and rejected as convention churn).

- A PR that changes running behavior **should** add a line under
  `## [Unreleased]` in the same change. Encouraged, not CI-enforced — the
  release PR is the backstop where gaps get filled.
- The release workflow **hard-fails** if the tagged version has no changelog
  section, so the discipline is enforced exactly once, where it matters.

## Cutting a release

1. **Release PR** (one logical change, like any other):
   - Bump the version everywhere it lives (the table above — 11 fields).
     `grep -rn "0\.1\.0"` against the table beats trusting memory;
     `python scripts/check_version_consistency.py X.Y.Z` confirms.
   - In `CHANGELOG.md`: retitle `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
     sweep in anything PRs forgot, start a fresh empty `[Unreleased]`.
2. Merge it (green CI includes the consistency check).
3. **Tag the merge commit** on `main` in the upstream repo:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" <merge-commit>
   git push upstream vX.Y.Z
   ```
4. `release.yml` does the rest — verifies tag↔files, builds the four images,
   pushes `ghcr.io/schuecl/nexus-rag/<service>:X.Y.Z` (immutable, no
   `:latest`, ever — NFR-16 applied to our own images), generates CycloneDX
   SBOMs, packages the chart and pushes
   `oci://ghcr.io/schuecl/nexus-rag/charts/nexus-rag:X.Y.Z`, and creates the
   GitHub Release with the changelog section as notes plus the chart tarball,
   SBOMs, image digests, and a sha256 manifest attached.
5. **Verify**: the Release page lists all artifacts;
   `helm pull oci://ghcr.io/schuecl/nexus-rag/charts/nexus-rag --version X.Y.Z`
   works; the digests in `image-digests.txt` match what GHCR shows.

Rollback = deploy the previous version's artifacts; nothing is ever
overwritten (immutable tags), so every released version remains deployable.
A botched release that already tagged: fix forward with a patch release —
never delete or re-point a version tag that the workflow ran for.

## Air-gapped deployment (MPNexus)

The connected/disconnected boundary is explicit: CI publishes to GHCR;
a human (or transfer process) carries a bundle across; the cluster only ever
sees the internal registry.

**Host prerequisite for the connected side.** `scripts/export_release_bundle.sh`
requires `jq`, and the Docker daemon it runs against must use the classic
`overlay2` storage driver, not the containerd image-store snapshotter. On a
snapshotter-backed daemon, `docker save` has been observed (#399) to exit 0
while silently omitting every layer and the image config blob from the tar —
the export script now catches this and refuses to bundle (`verify_image_tar`),
but the underlying `docker save` behavior can't be fixed from the script.
Check which driver is active:

```bash
docker info | grep -i "storage driver\|driver-type"
# containerd snapshotter active: "Storage Driver: overlayfs" +
#   "driver-type: io.containerd.snapshotter.v1"
# classic (wanted): "Storage Driver: overlay2", no driver-type line
```

If the snapshotter is active, disable it and restart the daemon (drops any
running containers, so restart `docker compose up` afterward too):

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "features": {
    "containerd-snapshotter": false
  }
}
EOF
sudo systemctl restart docker
```

**Connected side** — build the transfer bundle:

```bash
scripts/export_release_bundle.sh X.Y.Z
# -> dist/nexus-rag-X.Y.Z-bundle.tar.gz
```

The bundle contains the four images as docker archives, the chart tarball,
`image-digests.txt`, a `sha256sums.txt` over everything, and an `IMPORT.md`
with the exact disconnected-side commands.

**Disconnected side** — import and deploy:

```bash
tar xzf nexus-rag-X.Y.Z-bundle.tar.gz && cd nexus-rag-X.Y.Z
sha256sum -c sha256sums.txt                    # verify before anything else
for img in images/*.tar; do docker load -i "$img"; done
# retag into the internal registry and push (same bare names the chart uses)
for svc in ingestion-api ingestion-worker orchestration-mcp reranker-service; do
  docker tag "ghcr.io/schuecl/nexus-rag/${svc}:X.Y.Z" \
    "registry.internal.example.mil/nexus-rag/${svc}:X.Y.Z"
  docker push "registry.internal.example.mil/nexus-rag/${svc}:X.Y.Z"
done
helm install nexus-rag ./nexus-rag-X.Y.Z.tgz \
  --set global.imageRegistry=registry.internal.example.mil/nexus-rag
```

The chart's `global.imageRegistry` + bare `image.repository` names exist for
exactly this flow — no values surgery per release, just the registry prefix.
Third-party images (Postgres, Qdrant, NATS, Keycloak, ...) are pinned in
`values.yaml` and mirrored by the same retag-and-push pattern; they change
rarely and are listed release-to-release by the values.yaml diff.

## Answering "what version is running"

`helm list` shows the chart/app version; every image in the cluster carries
its version in the tag; the GitHub Release for that tag is the changelog,
SBOM, and digest record for what's deployed. That chain — cluster → tag →
release page → changelog — is the point of the whole process.
