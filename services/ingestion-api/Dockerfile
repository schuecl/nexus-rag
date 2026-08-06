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

COPY services/common /srv/common-pkg

# Issue #230: third-party deps installed from a hash-pinned lockfile
# (includes services/common's own deps, since it's installed --no-deps
# below) so a compromised/typosquatted PyPI package can't slip in even if
# it satisfies the floor ranges in pyproject.toml. Regenerate with
# `pip-compile --generate-hashes` from requirements.in after editing either
# pyproject.toml.
COPY services/ingestion-api/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt

COPY services/ingestion-api/pyproject.toml /srv/pyproject.toml
COPY services/ingestion-api/app /srv/app
RUN pip install --no-cache-dir --no-deps /srv/common-pkg .

# Issue #491: pip vendors frozen copies of its own build deps
# (pip/_vendor/msgpack, pip/_vendor/setuptools SBOM entry) separate from the
# app-facing setuptools upgraded above -- CVE-2025-47273 (setuptools) and
# GHSA-6v7p-g79w-8964 (msgpack) live only there. Nothing in this service
# invokes pip at runtime, so pip/setuptools/wheel (and everything vendored
# inside them) are removed outright rather than chasing individual vendored
# CVEs. Interim fix -- issue #491 tracks a multi-stage rebuild as the real
# fix for this whole class of finding.
RUN pip uninstall -y pip setuptools wheel

ENV PYTHONDONTWRITEBYTECODE=1

# Fixed non-root UID/GID (matches helm/nexus-rag's securityContext.runAsUser)
# rather than a runtime-injected one, so the object-store mount point below
# already has the right owner baked into the image before a fresh named
# volume / PVC gets mounted over it -- Docker/Kubernetes initialize an empty
# volume by copying the mount point's existing ownership from the image, not
# the other way around.
#
# NFR-12: /srv/object-store (common.object_store.FilesystemObjectStore's
# default OBJECT_STORE_PATH) has to exist in the image *before* chown, or
# the chown has nothing to affect -- confirmed live: without this, Docker
# auto-creates the docker-compose.yml object-store-data volume's mount point
# owned by root, and this container's first write ("PermissionError: [Errno
# 13] Permission denied: '/srv/object-store/documents'") fails outright.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /srv/object-store \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
