"""FR-5: generate embeddings for each chunk using the self-hosted,
non-Chinese-origin embedding model served by Ollama (REQUIREMENTS.md Section
7.2) or, since issue #403, an OpenAI-API-compliant hosted model
(`EMBEDDING_API_COMPATIBILITY=openai`) -- the wire protocol itself lives in
`common.embedding_client`, shared with orchestration-mcp's `_embed_query` so
the two can't drift on request/response shape.

Issue #396: batched, not one request per chunk. A document that chunks into
100 pieces was 100 sequential round trips, and that latency sat inside both
the per-document processing timeout (#208) and NFR-11's redelivery loop --
the slowest documents were the likeliest to time out and repeat the work.
Batches are bounded (EMBEDDING_BATCH_SIZE, default 32): an unbounded batch
just moves the memory problem into the embedding server.
"""

from __future__ import annotations

import logging
import os

import httpx

from common.embedding_client import request_embeddings
from common.embedding_prefixes import document_prefix

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DEFAULT_BATCH_SIZE = 32


def _batch_size() -> int:
    """Read EMBEDDING_BATCH_SIZE, degrading loudly on a bad value.

    Read at import of a module every worker boot imports, so a bad value must
    not raise (same failure mode and same reasoning as issue #389's
    DB_POOL_RECYCLE_SECONDS parse): a typo would crash the worker at startup,
    far from its cause. A rejected value is always logged so it can't be
    silently believed to have taken effect.
    """
    raw = os.environ.get("EMBEDDING_BATCH_SIZE")
    if raw is None or not raw.strip():
        return DEFAULT_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "EMBEDDING_BATCH_SIZE=%r is not an integer; using the default %d",
            raw,
            DEFAULT_BATCH_SIZE,
        )
        return DEFAULT_BATCH_SIZE
    if value < 1:
        logger.warning(
            "EMBEDDING_BATCH_SIZE=%d is not a usable batch size; using the default %d",
            value,
            DEFAULT_BATCH_SIZE,
        )
        return DEFAULT_BATCH_SIZE
    return value


EMBEDDING_BATCH_SIZE = _batch_size()


class EmbeddingError(Exception):
    pass


async def embed_texts(texts: list[str]) -> list[list[float]]:
    # Issue #392: nomic-embed-text is asymmetric and requires this prefix on
    # passages, distinct from the query-side prefix in orchestration-mcp's
    # _embed_query -- see common.embedding_prefixes for why it lives there.
    prefix = document_prefix(EMBEDDING_MODEL)
    prefixed = [prefix + text for text in texts]
    vectors: list[list[float]] = []
    # 300s read budget instead of the old per-request 60s: one request now
    # carries up to EMBEDDING_BATCH_SIZE texts, and a CPU-only Ollama (the
    # documented dev default) earns the whole batch's compute in one response.
    # The #208 per-document timeout still bounds the stage overall.
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        for start in range(0, len(prefixed), EMBEDDING_BATCH_SIZE):
            batch = prefixed[start : start + EMBEDDING_BATCH_SIZE]
            try:
                vectors.extend(await request_embeddings(client, OLLAMA_URL, EMBEDDING_MODEL, batch))
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding request failed: {exc}") from exc
            except ValueError as exc:
                # request_embeddings' count-mismatch guard: a misaligned batch
                # is a permanent failure, not something to retry into Qdrant.
                raise EmbeddingError(str(exc)) from exc
    return vectors
