"""NFR-11: durable ingestion processing, moved out of ingestion-api's
BackgroundTasks. This service's only real job is app/processing.py's
consume_forever() loop; the FastAPI app around it exists just to give
Kubernetes/Compose a /health endpoint to probe, matching the other two
custom services' shape (ingestion-api, orchestration-mcp) rather than
introducing a different liveness-check mechanism for this one.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.processing import consume_forever
from common.db import init_db
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # idempotent -- ingestion-api already does this too (common/db.py)
    consumer_task = asyncio.create_task(consume_forever())
    try:
        yield
    finally:
        consumer_task.cancel()


app = FastAPI(title="nexus-rag ingestion-worker", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}
