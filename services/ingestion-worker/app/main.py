"""NFR-11: durable ingestion processing, moved out of ingestion-api's
BackgroundTasks. This service's only real job is app/processing.py's
consume_forever() loop; the FastAPI app around it exists just to give
Kubernetes/Compose a /health endpoint to probe, matching the other two
custom services' shape (ingestion-api, orchestration-mcp) rather than
introducing a different liveness-check mechanism for this one.

Issue #109: because the consumer is the whole point of this service and the
HTTP app is only its probe surface, /health reports the consumer's state
rather than the app's. A `{"status": "ok"}` literal made the one failure
mode that matters invisible: the consumer task dying leaves the app serving
200s, so the pod stays Ready, nothing restarts it, and uploads keep being
accepted into a queue nobody drains -- a stall that looks exactly like an
idle system from outside.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Imported as a module, not `from app.processing import STATUS`: STATUS is a
# mutable module-level object, and a from-import would bind this module to
# whichever instance existed at import time. Production only ever mutates it
# in place so either form would work there, but the indirection keeps the two
# modules from disagreeing about which object is current.
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app import processing
from app.processing import consume_forever
from common.db import init_db
from common.logging_setup import setup_logging
from common.siem import enable_siem_export
from common.tracing import setup_tracing

# #73: level-configurable structured logging (LOG_LEVEL/LOG_FORMAT), and NFR-2
# SIEM export of the audit events the pipeline writes (document.embedded,
# document.failed, ...).
setup_logging("ingestion-worker")
enable_siem_export("ingestion-worker")
# #134: the ingest.process span tree (processing.py), joined to
# ingestion-api's ingest.submit via NATS message headers. httpx
# instrumentation adds the Ollama embedding call as a child span. Disabled
# (no-op spans) unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
setup_tracing("ingestion-worker")
HTTPXClientInstrumentor().instrument()


def _log_consumer_exit(task: asyncio.Task) -> None:
    """A task nobody awaits swallows its exception. consume_forever already
    records why it stopped in STATUS (which is what /health reads); this makes
    sure the traceback reaches the logs too, even on a path that bypassed
    consume_forever's own handler."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger("ingestion-worker").error(
            "consumer task exited with an exception", exc_info=exc
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # idempotent -- ingestion-api already does this too (common/db.py)
    consumer_task = asyncio.create_task(consume_forever())
    consumer_task.add_done_callback(_log_consumer_exit)
    try:
        yield
    finally:
        consumer_task.cancel()


app = FastAPI(title="nexus-rag ingestion-worker", lifespan=lifespan)


@app.get("/health")
def health():
    """503 whenever the consumer isn't running, so a k8s liveness probe
    restarts the pod and a readiness probe takes it out of service instead of
    both reporting a worker that stopped working. The body carries the
    consumer's own counters either way -- see processing.ConsumerStatus for
    why `running` is derived from the task having exited rather than from a
    heartbeat freshness check (a long document must not read as dead)."""
    status = processing.STATUS
    payload = {"status": "ok" if status.running else "degraded", "consumer": status.as_dict()}
    if not status.running:
        return JSONResponse(payload, status_code=503)
    return payload
