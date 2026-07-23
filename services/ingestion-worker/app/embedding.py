"""FR-5: generate embeddings for each chunk using the self-hosted,
non-Chinese-origin embedding model served by Ollama (REQUIREMENTS.md Section
7.2). Sequential calls, not batched/concurrent -- fine for the small dev
corpus this stack is meant for; worth revisiting for throughput once ingestion
runs against a real-sized corpus.
"""

from __future__ import annotations

import os

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


class EmbeddingError(Exception):
    pass


async def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for text in texts:
            try:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/embeddings",
                    json={"model": EMBEDDING_MODEL, "prompt": text},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding request failed: {exc}") from exc
            vectors.append(resp.json()["embedding"])
    return vectors
