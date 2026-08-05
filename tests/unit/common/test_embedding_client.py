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
