"""Issue #308 (Phase 3 of #138): the LLM zero-shot classification client --
degrade-on-failure contract (suggest_classification never raises) and
validation of the model's answer against the configured Classification list.

No live Ollama anywhere: the model endpoint is mocked with respx, same
approach as tests/test_captioning.py.
"""

from __future__ import annotations

import json

import httpx
import respx

from app import classification_suggestion
from app.classification_suggestion import suggest_classification

OLLAMA = "http://ollama:11434"
CLASSIFICATIONS = ["UNCLASSIFIED", "CUI", "SECRET"]


def _enable(monkeypatch, model: str = "test-classify-model") -> None:
    monkeypatch.setattr(classification_suggestion, "CLASSIFICATION_MODEL", model)
    monkeypatch.setattr(classification_suggestion, "OLLAMA_URL", OLLAMA)


def _mock_generate(body: dict) -> respx.Route:
    return respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps(body)})
    )


async def test_disabled_by_default_makes_no_request():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{OLLAMA}/api/generate")
        result = await suggest_classification("some text", classifications=CLASSIFICATIONS)
        assert result is None
        assert not route.called


@respx.mock
async def test_successful_suggestion_is_parsed(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "classification": "SECRET",
            "doc_type": "briefing slide",
            "program_community": "Signal Corps",
            "confidence": 0.87,
            "rationale": "Mentions troop movements and a classified banner.",
        }
    )
    result = await suggest_classification("some text", classifications=CLASSIFICATIONS)
    assert result is not None
    assert result.classification == "SECRET"
    assert result.doc_type == "briefing slide"
    assert result.program_community == "Signal Corps"
    assert result.confidence == 0.87
    assert "troop movements" in result.rationale


@respx.mock
async def test_classification_matches_case_insensitively_to_configured_casing(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"classification": "secret", "doc_type": "memo", "confidence": 0.5})
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is not None
    assert result.classification == "SECRET"


@respx.mock
async def test_classification_outside_configured_list_is_dropped_not_invented(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "classification": "TOP SECRET",
            "doc_type": "memo",
            "confidence": 0.9,
            "rationale": "looks sensitive",
        }
    )
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is not None
    assert result.classification is None
    # Everything else the model said is still useful decision support.
    assert result.doc_type == "memo"
    assert result.rationale == "looks sensitive"


@respx.mock
async def test_request_carries_model_and_configured_classifications(monkeypatch):
    _enable(monkeypatch)
    route = _mock_generate({"classification": "CUI", "confidence": 0.5})
    await suggest_classification("some document text", classifications=CLASSIFICATIONS)
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "test-classify-model"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert "SECRET" in body["prompt"]
    assert "some document text" in body["prompt"]


@respx.mock
async def test_text_is_truncated_to_scan_limit(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(classification_suggestion, "CLASSIFICATION_SCAN_LIMIT", 10)
    route = _mock_generate({"classification": "CUI", "confidence": 0.5})
    await suggest_classification("0123456789overflow", classifications=CLASSIFICATIONS)
    body = json.loads(route.calls.last.request.content)
    assert "overflow" not in body["prompt"]
    assert "0123456789" in body["prompt"]


@respx.mock
async def test_malformed_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "not json at all"})
    )
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is None


@respx.mock
async def test_non_object_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps(["not", "a", "dict"])})
    )
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is None


@respx.mock
async def test_missing_confidence_defaults_to_zero(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"classification": "CUI", "doc_type": "memo"})
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is not None
    assert result.confidence == 0.0


@respx.mock
async def test_out_of_range_confidence_is_clamped(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"classification": "CUI", "confidence": 4.2})
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is not None
    assert result.confidence == 1.0


@respx.mock
async def test_model_unreachable_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(side_effect=httpx.ConnectError("refused"))
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is None


@respx.mock
async def test_server_error_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(return_value=httpx.Response(500, text="boom"))
    result = await suggest_classification("text", classifications=CLASSIFICATIONS)
    assert result is None


async def test_no_configured_classifications_short_circuits(monkeypatch):
    _enable(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{OLLAMA}/api/generate")
        result = await suggest_classification("text", classifications=[])
        assert result is None
        assert not route.called
