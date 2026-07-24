FROM python:3.11-slim

WORKDIR /srv

COPY services/common /srv/common-pkg
RUN pip install --no-cache-dir /srv/common-pkg

COPY services/ingestion-worker/pyproject.toml /srv/pyproject.toml
COPY services/ingestion-worker/app /srv/app
RUN pip install --no-cache-dir .

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
