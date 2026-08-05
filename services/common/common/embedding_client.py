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

import logging
import os

import httpx

logger = logging.getLogger(__name__)

API_COMPATIBILITY = os.environ.get("EMBEDDING_API_COMPATIBILITY", "ollama")
API_KEY = os.environ.get("EMBEDDING_API_KEY", "")

# Base URLs whose /api/embed 404'd once already -- warn once per endpoint, not
# once per batch (issue #396's legacy fallback, see request_embeddings).
_legacy_batch_endpoints: set[str] = set()


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


async def request_embeddings(
    client: httpx.AsyncClient, base_url: str, model: str, texts: list[str]
) -> list[list[float]]:
    """POST a batch of already-prefixed texts, returning one vector per text
    in input order (issue #396 -- one request per batch instead of one per
    text).

    Ordering is part of this function's contract, not an assumption: callers
    store vectors[i] as the embedding of texts[i]. Ollama's /api/embed
    documents input-order responses; the OpenAI shape carries an explicit
    `index` per item, which is sorted on rather than trusted to arrive
    ordered. Either way a response with the wrong number of vectors raises
    ValueError -- a silently truncated or padded batch must never be written
    to the vector store.

    Sends truncate=false: /api/embed's default would silently tail-truncate
    an input over the model's context length, storing a vector that doesn't
    represent the chunk. The legacy endpoint errors on such input instead,
    and chunking's size bounds were built against that loud ceiling -- see
    the inline comment at the request.

    Against an older pinned Ollama whose image predates /api/embed (the
    endpoint set is knowable per deployment, NFR-16, but air-gapped registries
    can lag), a 404 falls back to one legacy /api/embeddings request per text
    -- the pre-#396 behavior, logged once per endpoint. (No truncate flag
    exists there; oversized input already errors, which is the same loud
    outcome.)

    Raises `httpx.HTTPError` subclasses on transport/status failures,
    unwrapped -- same reasoning as `request_embedding` above.
    """
    if not texts:
        return []
    if API_COMPATIBILITY == "openai":
        headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
        resp = await client.post(
            f"{base_url}/v1/embeddings",
            json={"model": model, "input": texts},
            headers=headers,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda item: item["index"])
        vectors = [list(item["embedding"]) for item in data]
    else:
        # truncate=false, deliberately: /api/embed's default (true) silently
        # tail-truncates any input over the model's effective context length
        # (verified against the pinned ollama 0.32.1 -- a ~6000-word input
        # came back "success" with prompt_eval_count=2048), so a chunk whose
        # tail was cut would be stored as a vector that doesn't represent it.
        # The legacy /api/embeddings endpoint *errored* on the same input --
        # that loud failure is how the oversized-CIS-table chunking bug was
        # found at all (see ingestion-worker app/chunking.py's module
        # docstring) and app/chunking.py bounds chunk sizes against exactly
        # that ceiling. false restores the legacy semantics: the error
        # surfaces as the worker's permanent-failure path instead of a
        # silently unrepresentative embedding.
        resp = await client.post(
            f"{base_url}/api/embed",
            json={"model": model, "input": texts, "truncate": False},
        )
        if resp.status_code == 404:
            if base_url not in _legacy_batch_endpoints:
                _legacy_batch_endpoints.add(base_url)
                logger.warning(
                    "/api/embed not available at %s (HTTP 404) -- this Ollama predates the "
                    "batch endpoint; falling back to one /api/embeddings request per text",
                    base_url,
                )
            return [await request_embedding(client, base_url, model, text) for text in texts]
        resp.raise_for_status()
        vectors = [list(v) for v in resp.json()["embeddings"]]
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedding endpoint returned {len(vectors)} vectors for {len(texts)} inputs -- "
            "refusing to guess an alignment"
        )
    return vectors
