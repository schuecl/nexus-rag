FROM python:3.11-slim

WORKDIR /srv

# Issue #230: hash-pinned lockfile instead of a floating floor.
COPY scripts/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt

COPY scripts/*.py scripts/*.json /srv/

# Issues #459/#464: image had no USER, so every one-shot built from this
# Dockerfile (seed-sample-data, eval-retrieval, calibrate-tagging-advisory,
# detect-query-anomalies, ingest-classification-corpus, verify-corpus-access)
# ran as root. None of them write anywhere but /srv's own __pycache__ at
# runtime, so the fixed UID/GID convention the four service Dockerfiles use
# (matches helm/nexus-rag's securityContext.runAsUser) applies here too.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /srv
USER appuser

CMD ["python3", "seed_sample_data.py"]
