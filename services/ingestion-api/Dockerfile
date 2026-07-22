FROM python:3.11-slim

WORKDIR /srv

COPY services/common /srv/common-pkg
RUN pip install --no-cache-dir /srv/common-pkg

COPY services/ingestion-api/pyproject.toml /srv/pyproject.toml
COPY services/ingestion-api/app /srv/app
RUN pip install --no-cache-dir .

# Caches the BM25 sparse model (common.sparse_embedding); mount a volume here
# (see docker-compose.yml) so it isn't re-downloaded on every restart.
ENV HF_HOME=/srv/.cache/huggingface

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
