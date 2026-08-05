"""Issue #418: the chat/generation wire protocol spoken against OLLAMA_URL,
shared by ingestion-worker's `app/captioning.py`, `app/classification_suggestion.py`,
and `app/pii_llm_advisory.py` -- all three already point at the same Ollama
instance as `common/embedding_client.py` (see `helm/nexus-rag/values.yaml`'s
`embeddingService` comment) and speak the identical `/api/generate` request/
response shape, so they share one client here rather than three copies --
same reasoning as `embedding_client.py` itself.

`COMPLETION_API_COMPATIBILITY` selects which wire format the configured
endpoint speaks:

- `"ollama"` (default): today's behavior, unchanged. `POST {model, prompt,
  stream: false}` (plus `images`/`format` when the caller passes them) to
  `/api/generate`, unauthenticated, response `{"response": "..."}`.
- `"openai"`: any OpenAI-API-compliant hosted model (vLLM, TGI, a cloud
  chat-completion endpoint, or Ollama's own `/v1/chat/completions`, which
  speaks this shape too). `POST {model, messages: [...]}` to
  `/v1/chat/completions`, `Authorization: Bearer COMPLETION_API_KEY` when
  that's set, response `{"choices": [{"message": {"content": "..."}}]}`.
  A `json_format` request adds `response_format: {"type": "json_object"}`,
  the OpenAI convention for the same "force valid JSON" contract as Ollama's
  `format: "json"`. Images (vision prompts) are carried as
  `image_url`/data-URI content parts alongside the text -- the multimodal
  content-array shape OpenAI-compatible vision models expect.

This module deliberately does not wrap httpx's exceptions or a malformed
response into anything of its own beyond `ValueError` for response-shape
mismatches (missing `choices`/`message`/`content`) -- same reasoning as
`embedding_client.py`: every caller already treats "model unreachable or
returned garbage" as one degrade-to-None outcome via its own
`except (httpx.HTTPError, ValueError, KeyError)`, so a bare `ValueError` here
is exactly what that catch already expects.
"""

from __future__ import annotations

import os

import httpx

API_COMPATIBILITY = os.environ.get("COMPLETION_API_COMPATIBILITY", "ollama")
API_KEY = os.environ.get("COMPLETION_API_KEY", "")


def _openai_content(prompt: str, images: list[str] | None) -> str | list[dict[str, object]]:
    if not images:
        return prompt
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    for image in images:
        # Mime type is best-effort/generic: OpenAI-compatible vision servers
        # decode the base64 payload itself rather than trusting the declared
        # type, and the caller (captioning.py) doesn't know the source
        # format after extraction either.
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}
        )
    return content


def _parse_openai_response(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError(
            f"malformed OpenAI-compatible completion response: expected a JSON object, "
            f"got {type(payload).__name__}"
        )
    try:
        return str(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"malformed OpenAI-compatible completion response: {exc}") from exc


async def request_completion(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    *,
    images: list[str] | None = None,
    json_format: bool = False,
) -> str:
    """POST a single completion request and return the model's raw text
    response. `images` is a list of already-base64-encoded image payloads
    (vision prompts); `json_format` requests the endpoint constrain its
    output to valid JSON. Raises `httpx.HTTPError` (or a subclass) on any
    transport/status failure, and `ValueError` if an OpenAI-compatible
    response doesn't contain the expected `choices[0].message.content` shape
    -- both unwrapped, see module docstring."""
    if API_COMPATIBILITY == "openai":
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": _openai_content(prompt, images)}],
        }
        if json_format:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
        resp = await client.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        return _parse_openai_response(resp.json())

    ollama_payload: dict[str, object] = {"model": model, "prompt": prompt, "stream": False}
    if images:
        ollama_payload["images"] = images
    if json_format:
        ollama_payload["format"] = "json"
    resp = await client.post(f"{base_url}/api/generate", json=ollama_payload)
    resp.raise_for_status()
    return str(resp.json().get("response", ""))
