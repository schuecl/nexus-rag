"""Issue #343 (Phase 2 of #342): the LLM-assisted PII/sensitive-info client --
degrade-on-failure contract (suggest_pii_llm_findings never raises) and
parsing of the model's findings list.

No live Ollama anywhere: the model endpoint is mocked with respx, same
approach as tests/test_classification_suggestion.py.
"""

from __future__ import annotations

import json

import httpx
import respx

from app import pii_llm_advisory
from app.pii_llm_advisory import suggest_pii_llm_findings

OLLAMA = "http://ollama:11434"


def _enable(monkeypatch, model: str = "test-pii-model") -> None:
    monkeypatch.setattr(pii_llm_advisory, "PII_LLM_MODEL", model)
    monkeypatch.setattr(pii_llm_advisory, "OLLAMA_URL", OLLAMA)


def _mock_generate(body: dict) -> respx.Route:
    return respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps(body)})
    )


async def test_disabled_by_default_makes_no_request():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{OLLAMA}/api/generate")
        result = await suggest_pii_llm_findings("some text")
        assert result is None
        assert not route.called


@respx.mock
async def test_successful_findings_are_parsed(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "findings": [
                {
                    "kind": "spelled-out SSN",
                    "rationale": "a Social Security Number is written out in prose",
                }
            ]
        }
    )
    result = await suggest_pii_llm_findings("some text")
    assert result is not None
    assert len(result) == 1
    assert result[0].kind == "spelled-out SSN"
    assert "written out in prose" in result[0].rationale


@respx.mock
async def test_empty_findings_list_is_a_valid_clean_result(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"findings": []})
    result = await suggest_pii_llm_findings("ordinary prose")
    assert result == []


@respx.mock
async def test_findings_are_capped(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"findings": [{"kind": f"kind-{i}"} for i in range(10)]})
    result = await suggest_pii_llm_findings("text")
    assert result is not None
    assert len(result) == 5


@respx.mock
async def test_findings_missing_kind_are_dropped(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"findings": [{"rationale": "no kind here"}, {"kind": "valid"}]})
    result = await suggest_pii_llm_findings("text")
    assert result is not None
    assert [f.kind for f in result] == ["valid"]


@respx.mock
async def test_missing_rationale_is_none(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"findings": [{"kind": "foreign national ID"}]})
    result = await suggest_pii_llm_findings("text")
    assert result is not None
    assert result[0].rationale is None


@respx.mock
async def test_request_carries_model_and_text(monkeypatch):
    _enable(monkeypatch)
    route = _mock_generate({"findings": []})
    await suggest_pii_llm_findings("some document text")
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "test-pii-model"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert "some document text" in body["prompt"]


@respx.mock
async def test_text_is_truncated_to_scan_limit(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(pii_llm_advisory, "PII_LLM_SCAN_LIMIT", 10)
    route = _mock_generate({"findings": []})
    await suggest_pii_llm_findings("0123456789overflow")
    body = json.loads(route.calls.last.request.content)
    assert "overflow" not in body["prompt"]
    assert "0123456789" in body["prompt"]


@respx.mock
async def test_malformed_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "not json at all"})
    )
    result = await suggest_pii_llm_findings("text")
    assert result is None


@respx.mock
async def test_non_object_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps(["not", "a", "dict"])})
    )
    result = await suggest_pii_llm_findings("text")
    assert result is None


@respx.mock
async def test_findings_not_a_list_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"findings": "not a list"})
    result = await suggest_pii_llm_findings("text")
    assert result is None


@respx.mock
async def test_model_unreachable_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(side_effect=httpx.ConnectError("refused"))
    result = await suggest_pii_llm_findings("text")
    assert result is None


@respx.mock
async def test_server_error_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(return_value=httpx.Response(500, text="boom"))
    result = await suggest_pii_llm_findings("text")
    assert result is None
