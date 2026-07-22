FROM python:3.11-slim

WORKDIR /srv
RUN pip install --no-cache-dir httpx>=0.27

COPY scripts/seed_sample_data.py /srv/seed_sample_data.py

CMD ["python3", "seed_sample_data.py"]
