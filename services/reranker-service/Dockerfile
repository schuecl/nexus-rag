# Issue #511/#553: multi-stage build, same pattern as ingestion-api/
# ingestion-worker/orchestration-mcp (#511/#554). The `builder` stage does
# the pip installs (needs pip/setuptools/wheel present); the runtime stage
# below only copies in the resulting site-packages + app source, so
# pip/setuptools/wheel (and everything vendored inside them, e.g.
# pip/_vendor/msgpack) never enter the shipped image's layers at all --
# strictly stronger than the interim #491 fix of uninstalling them after the
# fact, and this stage never runs `pip` so there's nothing to uninstall.
#
# Split off from #511 into its own issue (#553) because this image's
# TORCH_INDEX_URL build arg (CPU vs CUDA torch wheel) has to survive the
# stage split, and a CUDA wheel needs its runtime shared libraries --
# whatever lands in site-packages via the `nvidia-*` pip packages -- carried
# into the runtime stage too, not just the app's own site-packages tree.
# Validated here against the CPU default only (no GPU in the dev stack or
# CI); the CUDA path is implemented but unvalidated, consistent with this
# repo's honest-confidence-labeling convention (see docs/dev-setup.md's
# "What's stubbed vs working").
FROM python:3.13-slim AS builder

# setuptools/wheel are upgraded (jaraco.context/wheel CVE-2026-23949,
# CVE-2026-24049 -- see #450/#451) since they're a build-time dependency of
# `pip install .` below; neither ships in the runtime stage.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /srv

COPY services/reranker-service/pyproject.toml /srv/pyproject.toml
COPY services/reranker-service/app /srv/app
# Issue #230: every third-party dep except torch installs from a
# hash-pinned lockfile. torch is installed first and separately, still
# unpinned by hash, from a configurable index, so sentence-transformers'
# dependency is already satisfied when the lockfile installs (its own
# requirements.in explains why torch can't be in the hashed lock: a
# runtime-selectable index can't be pinned to one variant's hashes). The
# default is the CPU wheel: the bare PyPI torch is the CUDA build (~6 GB of
# NVIDIA libraries), and this service runs the cross-encoder on CPU in the
# dev stack -- shipping dead CUDA libs also inflates the NFR-1 air-gapped
# mirror. A GPU deployment overrides the arg with its CUDA index (e.g.
# https://download.pytorch.org/whl/cu124), and an air-gapped build points
# it at the internal mirror of whichever variant it runs. A CUDA wheel's
# runtime shared libraries land inside site-packages too -- either bundled
# directly in the torch wheel or as separate `nvidia-*` pip packages torch
# depends on -- so the plain site-packages copy into the runtime stage below
# carries them along with no extra COPY needed.
COPY services/reranker-service/requirements.txt /srv/requirements.txt
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch --index-url "${TORCH_INDEX_URL}" \
    && pip install --no-cache-dir --require-hashes -r /srv/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    # Strip pip/setuptools/wheel out of the builder's own site-packages
    # before it gets copied into the runtime stage below -- if both copies
    # (this one and whatever python:3.13-slim's runtime layer ships
    # natively) land in the same site-packages path, `pip uninstall`
    # against two overlapping dist-info records leaves one of them
    # orphaned rather than actually removing the files (see #511's
    # ingestion-api Dockerfile, found the hard way there).
    && pip uninstall -y pip setuptools wheel

FROM python:3.13-slim

# Issue #450/#451: OS packages patched at build time rather than trusting the
# base image's own rebuild cadence -- Debian's security archive updates
# continuously, the upstream python:3.13-slim image does not. This is the
# only stage that ships, so it's the only one that needs the OS patch.
#
# Issue #511/#553: python:3.13-slim ships its own pip/setuptools/wheel baked
# in (separately from anything the builder stage installed) -- those are
# removed here, before the COPY below, so the runtime image never carries
# pip/setuptools/wheel (and everything vendored inside them, e.g.
# pip/_vendor/msgpack) at all.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip uninstall -y pip setuptools wheel

WORKDIR /srv

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY services/reranker-service/app /srv/app

# First run downloads the model from Hugging Face into HF_HOME; mount a
# volume there (see docker-compose.yml) so it persists across restarts and
# so the same cache can be pre-seeded for air-gapped mirroring (NFR-1).
ENV HF_HOME=/srv/.cache/huggingface
ENV PYTHONDONTWRITEBYTECODE=1

# Fixed non-root UID/GID (matches helm/nexus-rag's securityContext.runAsUser)
# rather than a runtime-injected one, so the cache dir HF_HOME points at
# already has the right owner baked into the image before a fresh named
# volume / PVC gets mounted over it -- Docker/Kubernetes initialize an empty
# volume by copying the mount point's existing ownership from the image, not
# the other way around.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p "$HF_HOME" \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8003
# `python -m uvicorn`, not the bare `uvicorn` console-script entry point --
# that script lives in /usr/local/bin, which the runtime stage's COPY
# (site-packages only, see #511's ingestion-api Dockerfile for the same
# fix) never brings over.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
