"""Issue #403: the embedding wire protocol spoken against OLLAMA_URL, shared
by ingestion-worker's `embed_texts` and orchestration-mcp's `_embed_query` so
the two can't drift on request/response shape -- same reasoning as
`common/embedding_prefixes.py` for the search_document:/search_query:
prefixes those two callers also share.

`EMBEDDING_API_COMPATIBILITY` selects which wire format the configured
endpoint speaks:

- `"ollama"` (default): today's behavior, unchanged. `POST {model, prompt}`
  to `/api/embeddings`, unauthenticated, response `{"embedding": [...]}`.
  What the self-deployed `embeddingService` (`ollama/ollama`) and any other
  Ollama-*compatible* `embeddingService.external` endpoint (issue #401)
  speak.
- `"openai"`: any OpenAI-API-compliant hosted model (vLLM, TGI, a cloud
  embedding endpoint) -- the other half of #401's original ask, not covered
  by #401 itself since that PR was chart-only. `POST {model, input}` to
  `/v1/embeddings`, `Authorization: Bearer EMBEDDING_API_KEY` when that's
  set (Ollama's native path has no credential; this is new), response
  `{"data": [{"embedding": [...]}], ...}`.

This module deliberately does not wrap httpx's exceptions into anything of
its own: the two callers already disagree on how an embedding failure should
be treated (ingestion-worker's `embed_texts` wraps it into its own
permanent-failure `EmbeddingError`; orchestration-mcp's `_embed_query` lets
`httpx.HTTPError` propagate to its own existing broad `except` clause) and
that choice belongs to each caller, not here.
"""

from __future__ import annotations

import os

import httpx

API_COMPATIBILITY = os.environ.get("EMBEDDING_API_COMPATIBILITY", "ollama")
API_KEY = os.environ.get("EMBEDDING_API_KEY", "")


async def request_embedding(
    client: httpx.AsyncClient, base_url: str, model: str, text: str
) -> list[float]:
    """POST a single already-prefixed text for embedding and return its
    vector. Raises `httpx.HTTPError` (or a subclass, e.g.
    `httpx.HTTPStatusError`) on any transport/status failure -- unwrapped,
    see module docstring."""
    if API_COMPATIBILITY == "openai":
        headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
        resp = await client.post(
            f"{base_url}/v1/embeddings",
            json={"model": model, "input": text},
            headers=headers,
        )
        resp.raise_for_status()
        return list(resp.json()["data"][0]["embedding"])

    resp = await client.post(
        f"{base_url}/api/embeddings",
        json={"model": model, "prompt": text},
    )
    resp.raise_for_status()
    return list(resp.json()["embedding"])
