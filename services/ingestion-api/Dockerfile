FROM python:3.11-slim

WORKDIR /srv

COPY services/common /srv/common-pkg
RUN pip install --no-cache-dir /srv/common-pkg

COPY services/ingestion-api/pyproject.toml /srv/pyproject.toml
COPY services/ingestion-api/app /srv/app
RUN pip install --no-cache-dir .

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
