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
from app.pii_llm_advisory import suggest_pii_llm_findings, verify_pii_findings

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


# Issue #378: verify_pii_findings -- a context-only second opinion on Phase
# 1's own regex findings, not the additive context-dependent-findings pass
# above. Same HTTP client/degrade contract, so the same respx-mocked-Ollama
# technique applies.

_SAMPLE_FINDING = {
    "kind": "credit_card",
    "detail": "Credit card number pattern (Luhn-valid)",
    "context": "Part No. [REDACTED] Rev 2",
    "offset": 42,
}


async def test_verify_disabled_by_default_makes_no_request():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{OLLAMA}/api/generate")
        result = await verify_pii_findings([_SAMPLE_FINDING])
        assert result is None
        assert not route.called


async def test_verify_empty_findings_returns_empty_without_request(monkeypatch):
    _enable(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{OLLAMA}/api/generate")
        result = await verify_pii_findings([])
        assert result == []
        assert not route.called


@respx.mock
async def test_verify_successful_verdicts_are_parsed(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "verdicts": [
                {
                    "index": 0,
                    "likely_false_positive": True,
                    "rationale": "reads like a part number, not a card number",
                }
            ]
        }
    )
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is not None
    assert len(result) == 1
    assert result[0].index == 0
    assert result[0].likely_false_positive is True
    assert "part number" in result[0].rationale


@respx.mock
async def test_verify_empty_verdicts_list_is_a_valid_result(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"verdicts": []})
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result == []


@respx.mock
async def test_verify_verdicts_are_capped(monkeypatch):
    _enable(monkeypatch)
    findings = [dict(_SAMPLE_FINDING, offset=i) for i in range(30)]
    _mock_generate({"verdicts": [{"index": i, "likely_false_positive": False} for i in range(30)]})
    result = await verify_pii_findings(findings)
    assert result is not None
    assert len(result) == 25


@respx.mock
async def test_verify_verdict_missing_likely_false_positive_is_dropped(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "verdicts": [
                {"index": 0, "rationale": "no verdict field here"},
                {"index": 1, "likely_false_positive": False},
            ]
        }
    )
    result = await verify_pii_findings([_SAMPLE_FINDING, _SAMPLE_FINDING])
    assert result is not None
    assert [v.index for v in result] == [1]


@respx.mock
async def test_verify_duplicate_index_keeps_first_only(monkeypatch):
    _enable(monkeypatch)
    _mock_generate(
        {
            "verdicts": [
                {"index": 0, "likely_false_positive": True, "rationale": "first"},
                {"index": 0, "likely_false_positive": False, "rationale": "second"},
            ]
        }
    )
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is not None
    assert len(result) == 1
    assert result[0].rationale == "first"


@respx.mock
async def test_verify_missing_rationale_is_none(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"verdicts": [{"index": 0, "likely_false_positive": False}]})
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is not None
    assert result[0].rationale is None


@respx.mock
async def test_verify_request_carries_context_not_offset_or_kind_alone(monkeypatch):
    _enable(monkeypatch)
    route = _mock_generate({"verdicts": []})
    await verify_pii_findings([_SAMPLE_FINDING])
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "test-pii-model"
    assert body["format"] == "json"
    assert "Part No. [REDACTED] Rev 2" in body["prompt"]
    assert "credit_card" in body["prompt"]


@respx.mock
async def test_verify_malformed_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "not json at all"})
    )
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is None


@respx.mock
async def test_verify_non_object_json_response_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": json.dumps(["not", "a", "dict"])})
    )
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is None


@respx.mock
async def test_verify_verdicts_not_a_list_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    _mock_generate({"verdicts": "not a list"})
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is None


@respx.mock
async def test_verify_model_unreachable_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(side_effect=httpx.ConnectError("refused"))
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is None


@respx.mock
async def test_verify_server_error_degrades_to_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{OLLAMA}/api/generate").mock(return_value=httpx.Response(500, text="boom"))
    result = await verify_pii_findings([_SAMPLE_FINDING])
    assert result is None
