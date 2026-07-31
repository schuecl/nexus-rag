FROM python:3.11-slim

WORKDIR /srv

# Issue #230: hash-pinned lockfile instead of a floating floor.
COPY scripts/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt

COPY scripts/*.py scripts/*.json /srv/

CMD ["python3", "seed_sample_data.py"]
