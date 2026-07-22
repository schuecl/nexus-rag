FROM python:3.11-slim

WORKDIR /srv

COPY services/reranker-service/pyproject.toml /srv/pyproject.toml
COPY services/reranker-service/app /srv/app
RUN pip install --no-cache-dir .

# First run downloads the model from Hugging Face into HF_HOME; mount a
# volume there (see docker-compose.yml) so it persists across restarts and
# so the same cache can be pre-seeded for air-gapped mirroring (NFR-1).
ENV HF_HOME=/srv/.cache/huggingface

EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
