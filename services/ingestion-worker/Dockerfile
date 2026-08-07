# Issue #511: multi-stage build. The `builder` stage does the pip installs
# (needs pip/setuptools/wheel present); the runtime stage below only copies
# in the resulting site-packages + app source, so pip/setuptools/wheel (and
# everything vendored inside them, e.g. pip/_vendor/msgpack) never enter the
# shipped image's layers at all -- strictly stronger than the interim #491
# fix of uninstalling them after the fact, and this stage never runs `pip`
# so there's nothing to uninstall.
FROM python:3.13-slim AS builder

# setuptools/wheel are upgraded (jaraco.context/wheel CVE-2026-23949,
# CVE-2026-24049 -- see #450/#451) since they're a build-time dependency of
# `pip install .` below; neither ships in the runtime stage.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

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
RUN pip install --no-cache-dir --no-deps /srv/common-pkg . \
    # Strip pip/setuptools/wheel out of the builder's own site-packages
    # before it gets copied into the runtime stage below -- if both copies
    # (this one and whatever python:3.13-slim's runtime layer ships
    # natively) land in the same site-packages path, `pip uninstall`
    # against two overlapping dist-info records leaves one of them
    # orphaned rather than actually removing the files.
    && pip uninstall -y pip setuptools wheel

FROM python:3.13-slim

# Issue #241: OCR for scanned PDFs and image uploads (app/ocr.py). Baked into
# the image -- never downloaded at runtime (NFR-1 air-gap). eng traineddata
# only; other languages (OCR_LANG) need their package added here. This is
# the shipped stage, so it's the one that needs the tesseract binary.
#
# Issue #450/#451: `apt-get upgrade` patches OS packages at build time rather
# than trusting the base image's own rebuild cadence -- Debian's security
# archive updates continuously, the upstream python:3.13-slim image does not.
#
# Issue #511: python:3.13-slim ships its own pip/setuptools/wheel baked in
# (separately from anything the builder stage installed) -- those are
# removed here, before the COPY below, so the runtime image never carries
# pip/setuptools/wheel (and everything vendored inside them, e.g.
# pip/_vendor/msgpack) at all. This replaces #491's interim fix of
# installing then uninstalling an app-facing pip/setuptools/wheel upgrade
# every build -- there's no such upgrade cycle left in this stage to keep
# pace with the vendored CVEs resurfacing.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && pip uninstall -y pip setuptools wheel

WORKDIR /srv

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY services/ingestion-worker/app /srv/app

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
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
