FROM python:3.11-slim

WORKDIR /srv

COPY services/reranker-service/pyproject.toml /srv/pyproject.toml
COPY services/reranker-service/app /srv/app
RUN pip install --no-cache-dir .

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
