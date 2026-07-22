FROM python:3.11-slim

WORKDIR /srv

COPY services/common /srv/common-pkg
RUN pip install --no-cache-dir /srv/common-pkg

COPY services/orchestration-mcp/pyproject.toml /srv/pyproject.toml
COPY services/orchestration-mcp/app /srv/app
RUN pip install --no-cache-dir .

EXPOSE 8002
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8002"]
