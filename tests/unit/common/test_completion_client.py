"""Coverage for issue #418: the shared Ollama-native/OpenAI-compatible chat
completion client -- ingestion-worker's captioning.py, classification_
suggestion.py, and pii_llm_advisory.py all delegate to this module (see
their own test suites for the prompt-construction/response-parsing behavior
that still lives at each call site).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from common import completion_client
from common.completion_client import request_completion

BASE_URL = "http://ollama:11434"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_ollama_default_posts_prompt_field(client):
    route = respx.post(f"{BASE_URL}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "a caption"})
    )

    text = await request_completion(client, BASE_URL, "llava", "describe this")

    assert text == "a caption"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"model": "llava", "prompt": "describe this", "stream": False}
    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}


@respx.mock
async def test_ollama_sends_images_and_format_when_provided(client):
    route = respx.post(f"{BASE_URL}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "{}"})
    )

    await request_completion(
        client, BASE_URL, "llava", "describe", images=["YWJj"], json_format=True
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["images"] == ["YWJj"]
    assert sent["format"] == "json"


@respx.mock
async def test_ollama_omits_images_and_format_when_not_requested(client):
    route = respx.post(f"{BASE_URL}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )

    await request_completion(client, BASE_URL, "m", "p")

    sent = json.loads(route.calls.last.request.content)
    assert "images" not in sent
    assert "format" not in sent


@respx.mock
async def test_ollama_missing_response_field_defaults_to_empty_string(client):
    respx.post(f"{BASE_URL}/api/generate").mock(return_value=httpx.Response(200, json={}))

    text = await request_completion(client, BASE_URL, "m", "p")

    assert text == ""


@respx.mock
async def test_ollama_propagates_http_status_error(client):
    respx.post(f"{BASE_URL}/api/generate").mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await request_completion(client, BASE_URL, "m", "p")


@respx.mock
async def test_openai_compatibility_posts_messages_to_chat_completions(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "a caption"}}]})
    )

    text = await request_completion(client, BASE_URL, "gpt-vision", "describe this")

    assert text == "a caption"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "model": "gpt-vision",
        "messages": [{"role": "user", "content": "describe this"}],
    }


@respx.mock
async def test_openai_compatibility_json_format_sets_response_format(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )

    await request_completion(client, BASE_URL, "m", "p", json_format=True)

    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"] == {"type": "json_object"}


@respx.mock
async def test_openai_compatibility_images_become_content_parts(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "caption"}}]})
    )

    await request_completion(client, BASE_URL, "m", "describe", images=["YWJj"])

    sent = json.loads(route.calls.last.request.content)
    content = sent["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


@respx.mock
async def test_openai_compatibility_sends_bearer_token_when_configured(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    monkeypatch.setattr(completion_client, "API_KEY", "s3cr3t")
    route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    )

    await request_completion(client, BASE_URL, "m", "p")

    assert route.calls.last.request.headers["authorization"] == "Bearer s3cr3t"


@respx.mock
async def test_openai_compatibility_omits_auth_header_when_key_unset(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    monkeypatch.setattr(completion_client, "API_KEY", "")
    route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    )

    await request_completion(client, BASE_URL, "m", "p")

    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}


@respx.mock
async def test_openai_compatibility_propagates_http_status_error(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(401))

    with pytest.raises(httpx.HTTPStatusError):
        await request_completion(client, BASE_URL, "m", "p")


@respx.mock
async def test_openai_compatibility_malformed_response_raises_value_error(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(ValueError, match="malformed OpenAI-compatible completion response"):
        await request_completion(client, BASE_URL, "m", "p")


@respx.mock
async def test_openai_compatibility_empty_choices_raises_value_error(monkeypatch, client):
    monkeypatch.setattr(completion_client, "API_COMPATIBILITY", "openai")
    respx.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    with pytest.raises(ValueError, match="malformed OpenAI-compatible completion response"):
        await request_completion(client, BASE_URL, "m", "p")
