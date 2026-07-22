FROM python:3.11-slim

WORKDIR /srv
RUN pip install --no-cache-dir httpx>=0.27

COPY scripts/*.py scripts/*.json /srv/

CMD ["python3", "seed_sample_data.py"]
