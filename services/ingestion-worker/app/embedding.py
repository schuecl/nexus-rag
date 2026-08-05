"""FR-5: generate embeddings for each chunk using the self-hosted,
non-Chinese-origin embedding model served by Ollama (REQUIREMENTS.md Section
7.2) or, since issue #403, an OpenAI-API-compliant hosted model
(`EMBEDDING_API_COMPATIBILITY=openai`) -- the wire protocol itself lives in
`common.embedding_client`, shared with orchestration-mcp's `_embed_query` so
the two can't drift on request/response shape. Sequential calls, not
batched/concurrent -- fine for the small dev corpus this stack is meant for;
worth revisiting for throughput once ingestion runs against a real-sized
corpus.
"""

from __future__ import annotations

import os

import httpx

from common.embedding_client import request_embedding
from common.embedding_prefixes import document_prefix

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


class EmbeddingError(Exception):
    pass


async def embed_texts(texts: list[str]) -> list[list[float]]:
    # Issue #392: nomic-embed-text is asymmetric and requires this prefix on
    # passages, distinct from the query-side prefix in orchestration-mcp's
    # _embed_query -- see common.embedding_prefixes for why it lives there.
    prefix = document_prefix(EMBEDDING_MODEL)
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for text in texts:
            try:
                vector = await request_embedding(client, OLLAMA_URL, EMBEDDING_MODEL, prefix + text)
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding request failed: {exc}") from exc
            vectors.append(vector)
    return vectors
