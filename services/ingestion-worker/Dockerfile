FROM python:3.11-slim

# Issue #241: OCR for scanned PDFs and image uploads (app/ocr.py). Baked into
# the image -- never downloaded at runtime (NFR-1 air-gap). eng traineddata
# only; other languages (OCR_LANG) need their package added here.
#
# Issue #450/#451: `apt-get upgrade` patches OS packages at build time rather
# than trusting the base image's own rebuild cadence -- Debian's security
# archive updates continuously, the upstream python:3.11-slim image does not.
# pip/setuptools/wheel are upgraded too: setuptools vendors its own copies of
# jaraco.context and wheel (CVE-2026-23949, CVE-2026-24049), which sit
# underneath the hash-pinned application lockfile below and can't be patched
# through it.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
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
COPY services/ingestion-worker/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt

COPY services/ingestion-worker/pyproject.toml /srv/pyproject.toml
COPY services/ingestion-worker/app /srv/app
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

# Caches the BM25 sparse model (common.sparse_embedding); mount a volume here
# (see docker-compose.yml) so it isn't re-downloaded on every restart.
ENV HF_HOME=/srv/.cache/huggingface
ENV PYTHONDONTWRITEBYTECODE=1

# Fixed non-root UID/GID (matches helm/nexus-rag's securityContext.runAsUser)
# rather than a runtime-injected one -- see ingestion-api/Dockerfile's
# identical comment for why. Same reasoning applies to /srv/object-store
# here as it does there (NFR-12) -- this service reads from it
# (get_object_store().get(...) in app/processing.py), which needs the mount
# point to already be owned by appuser for the same "volume inherits the
# image's ownership on first creation" reason.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p "$HF_HOME" /srv/object-store \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
