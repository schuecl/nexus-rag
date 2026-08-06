FROM python:3.13-slim

# Issue #450/#451: OS packages patched at build time rather than trusting the
# base image's own rebuild cadence -- Debian's security archive updates
# continuously, the upstream python:3.13-slim image does not. pip/setuptools/
# wheel are upgraded too: setuptools vendors its own copies of jaraco.context
# and wheel (CVE-2026-23949, CVE-2026-24049), which sit underneath the
# hash-pinned application lockfile below and can't be patched through it.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

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
# it at the internal mirror of whichever variant it runs.
COPY services/reranker-service/requirements.txt /srv/requirements.txt
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch --index-url "${TORCH_INDEX_URL}" \
    && pip install --no-cache-dir --require-hashes -r /srv/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    # Issue #491: pip vendors frozen copies of its own build deps
    # (pip/_vendor/msgpack, pip/_vendor/setuptools SBOM entry) separate from
    # the app-facing setuptools upgraded above -- CVE-2025-47273 (setuptools)
    # and GHSA-6v7p-g79w-8964 (msgpack) live only there. Nothing in this
    # service invokes pip at runtime, so pip/setuptools/wheel (and
    # everything vendored inside them) are removed outright rather than
    # chasing individual vendored CVEs. Interim fix -- issue #491 tracks a
    # multi-stage rebuild as the real fix for this whole class of finding.
    && pip uninstall -y pip setuptools wheel

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
