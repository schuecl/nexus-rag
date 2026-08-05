"""Coverage for issue #403: the shared Ollama-native/OpenAI-compatible
embedding request client -- ingestion-worker's embed_texts and
orchestration-mcp's _embed_query both delegate to this module (see their own
test_embedding.py suites for the prefix-application behavior that still
lives at each call site).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from common import embedding_client
from common.embedding_client import request_embedding

BASE_URL = "http://embedding-service:11434"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_ollama_default_posts_prompt_field(client):
    route = respx.post(f"{BASE_URL}/api/embeddings").mock(
        return_value=httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})
    )

    vector = await request_embedding(client, BASE_URL, "nomic-embed-text", "hello")

    assert vector == [0.1, 0.2, 0.3]
    sent = route.calls.last.request
    assert sent.url == f"{BASE_URL}/api/embeddings"
    assert json.loads(sent.content) == {"model": "nomic-embed-text", "prompt": "hello"}
    # Ollama's native endpoint takes no credential -- no Authorization header.
    assert "authorization" not in {k.lower() for k in sent.headers}


@respx.mock
async def test_ollama_propagates_http_status_error(client):
    respx.post(f"{BASE_URL}/api/embeddings").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await request_embedding(client, BASE_URL, "nomic-embed-text", "hello")


@respx.mock
async def test_openai_compatibility_posts_input_field_to_v1_embeddings(monkeypatch, client):
    monkeypatch.setattr(embedding_client, "API_COMPATIBILITY", "openai")
    route = respx.post(f"{BASE_URL}/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"embedding": [0.4, 0.5], "index": 0}], "model": "text-embed"}
        )
    )

    vector = await request_embedding(client, BASE_URL, "text-embed", "hello")

    assert vector == [0.4, 0.5]
    assert json.loads(route.calls.last.request.content) == {"model": "text-embed", "input": "hello"}


@respx.mock
async def test_openai_compatibility_sends_bearer_token_when_configured(monkeypatch, client):
    monkeypatch.setattr(embedding_client, "API_COMPATIBILITY", "openai")
    monkeypatch.setattr(embedding_client, "API_KEY", "s3cr3t")
    route = respx.post(f"{BASE_URL}/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    )

    await request_embedding(client, BASE_URL, "text-embed", "hello")

    assert route.calls.last.request.headers["authorization"] == "Bearer s3cr3t"


@respx.mock
async def test_openai_compatibility_omits_auth_header_when_key_unset(monkeypatch, client):
    monkeypatch.setattr(embedding_client, "API_COMPATIBILITY", "openai")
    monkeypatch.setattr(embedding_client, "API_KEY", "")
    route = respx.post(f"{BASE_URL}/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    )

    await request_embedding(client, BASE_URL, "text-embed", "hello")

    sent = route.calls.last.request
    assert "authorization" not in {k.lower() for k in sent.headers}


@respx.mock
async def test_openai_compatibility_propagates_http_status_error(monkeypatch, client):
    monkeypatch.setattr(embedding_client, "API_COMPATIBILITY", "openai")
    respx.post(f"{BASE_URL}/v1/embeddings").mock(return_value=httpx.Response(401))

    with pytest.raises(httpx.HTTPStatusError):
        await request_embedding(client, BASE_URL, "text-embed", "hello")


@respx.mock
async def test_batch_ollama_posts_input_list_to_api_embed(client):
    route = respx.post(f"{BASE_URL}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1], [0.2]]})
    )

    vectors = await embedding_client.request_embeddings(
        client, BASE_URL, "nomic-embed-text", ["one", "two"]
    )

    assert vectors == [[0.1], [0.2]]
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "nomic-embed-text", "input": ["one", "two"], "truncate": False}


@respx.mock
async def test_batch_openai_sorts_by_index_not_arrival_order(monkeypatch, client):
    """The OpenAI shape carries an explicit index per item; a compliant-but-
    unordered response must still map vectors[i] to texts[i]."""
    monkeypatch.setattr(embedding_client, "API_COMPATIBILITY", "openai")
    respx.post(f"{BASE_URL}/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.2], "index": 1},
                    {"embedding": [0.1], "index": 0},
                ]
            },
        )
    )

    vectors = await embedding_client.request_embeddings(
        client, BASE_URL, "text-embed", ["one", "two"]
    )

    assert vectors == [[0.1], [0.2]]


@respx.mock
async def test_batch_count_mismatch_raises_value_error(client):
    respx.post(f"{BASE_URL}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1]]})
    )

    with pytest.raises(ValueError, match="1 vectors for 2 inputs"):
        await embedding_client.request_embeddings(
            client, BASE_URL, "nomic-embed-text", ["one", "two"]
        )


@respx.mock
async def test_batch_404_falls_back_to_legacy_per_text_requests(client):
    """An older pinned Ollama without /api/embed (#396): degrade to the
    pre-batch behavior, preserving order, rather than failing ingestion."""
    embedding_client._legacy_batch_endpoints.discard(BASE_URL)
    respx.post(f"{BASE_URL}/api/embed").mock(return_value=httpx.Response(404))
    legacy = respx.post(f"{BASE_URL}/api/embeddings").mock(
        side_effect=[
            httpx.Response(200, json={"embedding": [0.1]}),
            httpx.Response(200, json={"embedding": [0.2]}),
        ]
    )

    vectors = await embedding_client.request_embeddings(
        client, BASE_URL, "nomic-embed-text", ["one", "two"]
    )

    assert vectors == [[0.1], [0.2]]
    assert legacy.call_count == 2
    prompts = [json.loads(c.request.content)["prompt"] for c in legacy.calls]
    assert prompts == ["one", "two"]


@respx.mock
async def test_batch_500_propagates_not_falls_back(client):
    """Only a 404 means "endpoint absent"; a server error must propagate so
    the caller's retry/permanent-failure logic sees it."""
    respx.post(f"{BASE_URL}/api/embed").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await embedding_client.request_embeddings(client, BASE_URL, "nomic-embed-text", ["one"])


async def test_batch_empty_input_returns_empty_without_a_request(client):
    assert await embedding_client.request_embeddings(client, BASE_URL, "m", []) == []


@respx.mock
async def test_batch_ollama_disables_silent_truncation(client):
    """truncate=false restores the legacy endpoint's loud-failure semantics:
    an over-context input must error (surfacing as the worker's permanent
    failure), never come back "success" as a vector of the chunk's prefix.
    Verified against the pinned ollama 0.32.1: the default silently truncated
    a ~6000-word input to prompt_eval_count=2048."""
    route = respx.post(f"{BASE_URL}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1]]})
    )

    await embedding_client.request_embeddings(client, BASE_URL, "nomic-embed-text", ["one"])

    assert json.loads(route.calls.last.request.content)["truncate"] is False
