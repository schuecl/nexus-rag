"""Issue #343 (Phase 2 of #342): ask the in-cluster LLM (Ollama -- no
external/cloud service, Section 3 origin constraint) to look for
context-dependent sensitive personal/financial information in a document's
own parsed text that the deterministic regex pass (`common/pii_scan.py`,
#342 Phase 1) can't catch -- a spelled-out or differently-formatted SSN, a
non-US national ID number, or freeform personal/financial detail embedded in
prose. Deliberately deferred out of #342 itself per that issue's own
recommendation ("land the regex pass first ... treat the LLM pass as a
follow-on if regex misses prove common in practice").

Model provisioning follows app/classification_suggestion.py's
CLASSIFICATION_MODEL convention: PII_LLM_MODEL empty (the default) disables
the feature entirely -- no model pull, no LLM calls, no behavior change. Set
to an Ollama text-generation model to enable.

Degrade contract, same as suggest_classification(): this module never
raises. Any failure -- model unreachable, malformed/non-JSON response --
degrades to `None` (no findings), logged by the caller. This is
advisory-only decision support that must never block or delay ingestion; the
human curator stays the actual spillage-control gate (FR-11).

Prompt-injection note, same as classification_suggestion.py: the prompt
embeds the document's own text, which is attacker-controlled input. A
crafted document could try to steer the model's `kind`/`rationale` output.
The blast radius is bounded by construction -- this module never redacts,
decides, or gates anything, only ever produces a curator-facing advisory hint
a human reviews -- but callers must still render every field as text content
only, never as markup (see curate.html's advisoryNode).

The prompt asks the model not to repeat the sensitive value itself in its
answer, so the curator sees what kind of thing was flagged and why without
that answer becoming a second place the value is stored. That's a prompt
instruction, not a guarantee -- unlike the regex pass, nothing here can
enforce it against a model that ignores the instruction. Callers must treat
`kind`/`rationale` as being exactly as attacker/model-controlled as
`rationale` on the classification suggestion, never redact them a second
time, and never assume the value itself was actually withheld.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("ingestion-worker")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
# Empty string = feature disabled (the default), same reasoning as
# VISION_MODEL/CLASSIFICATION_MODEL: an extra per-document LLM call is an
# explicit deployment decision, not a side effect of upgrading the worker.
PII_LLM_MODEL = os.environ.get("PII_LLM_MODEL", "")

PII_LLM_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("PII_LLM_REQUEST_TIMEOUT_SECONDS", "30"))

# Same reasoning as CLASSIFICATION_SCAN_LIMIT: much tighter than the regex
# scan's _ADVISORY_SCAN_LIMIT, since an LLM prompt costs context-window
# budget and latency per character.
PII_LLM_SCAN_LIMIT = int(os.environ.get("PII_LLM_SCAN_LIMIT", "6000"))

# A document could prompt the model into "finding" many things; the curator
# needs to know the kinds present, not an unbounded list -- same cap
# rationale as pii_scan.py's _MAX_FINDINGS_PER_KIND.
_MAX_LLM_FINDINGS = 5

PROMPT_TEMPLATE = (
    "You are assisting a document curator in a records-management system. A "
    "deterministic pattern scan has already checked this document's text for "
    "common sensitive-data formats: dashed US Social Security Numbers "
    "(###-##-####), valid credit card numbers, valid US bank routing numbers, "
    "well-known API key/token prefixes, and private-key block headers.\n\n"
    "Look ONLY for sensitive personal or financial information that scan "
    "would miss because it depends on context: a Social Security Number "
    "spelled out or formatted differently, a non-US national ID number, or "
    "freeform personal/financial information written out in prose (e.g. a "
    "bank account number, medical detail, or ID number embedded in a "
    "sentence rather than a clearly labeled field).\n\n"
    "Do not repeat the sensitive value itself anywhere in your response -- "
    "describe only the kind of information found and, briefly, why. If "
    "nothing beyond what the pattern scan already covers qualifies, return "
    "an empty findings list.\n\n"
    "Respond with strict JSON and nothing else, matching exactly this shape:\n"
    '{{"findings": [{{"kind": "<short label, e.g. spelled-out SSN, foreign '
    'national ID, freeform financial detail>", "rationale": "<one short '
    "sentence explaining the finding, without repeating the sensitive value "
    'itself>"}}]}}\n\n'
    "Document text:\n{text}"
)


@dataclass(frozen=True)
class PiiLlmFinding:
    """One model-reported finding. Advisory only -- see module docstring.
    `kind`/`rationale` are model output over attacker-controlled document
    text, so treat them as untrusted text content, never markup."""

    kind: str
    rationale: str | None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "rationale": self.rationale}


def pii_llm_enabled() -> bool:
    return bool(PII_LLM_MODEL)


def _parse_response(raw: str) -> list[PiiLlmFinding] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        return None

    findings: list[PiiLlmFinding] = []
    for item in raw_findings[:_MAX_LLM_FINDINGS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            continue
        rationale = item.get("rationale")
        findings.append(
            PiiLlmFinding(
                kind=kind.strip(),
                rationale=rationale.strip()
                if isinstance(rationale, str) and rationale.strip()
                else None,
            )
        )
    return findings


async def suggest_pii_llm_findings(text: str) -> list[PiiLlmFinding] | None:
    """Ask PII_LLM_MODEL to look for context-dependent sensitive information
    in `text`. Returns None if the feature is disabled, the model is
    unreachable, or its response can't be parsed -- an empty list means the
    model ran and reported no findings, which is a meaningfully different
    outcome from "unavailable". Never raises."""
    if not pii_llm_enabled():
        return None

    prompt = PROMPT_TEMPLATE.format(text=text[:PII_LLM_SCAN_LIMIT])
    try:
        async with httpx.AsyncClient(timeout=PII_LLM_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": PII_LLM_MODEL,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                },
            )
            resp.raise_for_status()
            raw = str(resp.json().get("response", ""))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("PII LLM advisory request failed: %s: %s", type(exc).__name__, exc)
        return None

    findings = _parse_response(raw)
    if findings is None:
        logger.warning("PII LLM advisory response was not valid JSON; discarding")
    return findings
