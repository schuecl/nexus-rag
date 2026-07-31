"""Issue #308 (Phase 3 of #138): ask the in-cluster LLM (Ollama -- no
external/cloud service, Section 3 origin constraint) to zero-shot-classify a
document against the admin-configured Classification vocabulary, plus a
free-text doc_type/program_community guess, with a confidence score and a
short rationale.

Scope note: only Classification (`ClassificationLevel`) is an actual
admin-configurable, DB-backed controlled list (see `common/models.py`).
`doc_type`/`program_community` (`common/metadata.py`) are plain free-text
fields with no equivalent controlled table -- REQUIREMENTS.md Section 6.3
itself only ever calls program_community "free-form OR controlled," never
mandates a controlled list exist. So the model is asked to *match* the
Classification value against the real configured list (and a value outside
it is dropped, never invented), but is only asked to *guess* doc_type/
program_community in its own words -- still useful decision support, just
not vocabulary-constrained the way Classification is.

Model provisioning follows app/captioning.py's VISION_MODEL convention:
CLASSIFICATION_MODEL empty (the default) disables the feature entirely -- no
model pull, no LLM calls, no behavior change. Set to an Ollama text
generation model to enable.

Degrade contract, same as caption_images(): this module never raises. Any
failure -- model unreachable, malformed/non-JSON response, a classification
value that doesn't match the configured list -- degrades to `None` (no
suggestion), logged by the caller. FR-11's spillage control stays the human
curator's job; this is advisory-only decision support that must never block
or delay ingestion.

Prompt-injection note: the prompt embeds the document's own text, which is
attacker-controlled input. A crafted document could try to steer the model's
`rationale`/`doc_type` output. The blast radius is bounded by construction --
this module never writes doc.classification/doc_type, only ever a curator-
facing advisory hint a human reviews before deciding -- but callers must
still render every field (rationale especially) as text content only, never
as markup (see curate.html's advisoryNode, same posture as marking-detection
evidence excerpts).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("ingestion-worker")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
# Empty string = suggestion disabled (the default). No hardcoded fallback,
# same reasoning as VISION_MODEL: pulling/serving an extra generation model
# is an explicit deployment decision, not a side effect of upgrading the
# worker.
CLASSIFICATION_MODEL = os.environ.get("CLASSIFICATION_MODEL", "")

CLASSIFICATION_REQUEST_TIMEOUT_SECONDS = float(
    os.environ.get("CLASSIFICATION_REQUEST_TIMEOUT_SECONDS", "30")
)

# Document text is scanned for markings in full (see
# common/marking_detection.py's _ADVISORY_SCAN_LIMIT), but an LLM prompt
# costs context-window budget and latency per character -- bounded much
# tighter here since a document's classification/type is usually evident
# from its opening content.
CLASSIFICATION_SCAN_LIMIT = int(os.environ.get("CLASSIFICATION_SCAN_LIMIT", "6000"))

PROMPT_TEMPLATE = (
    "You are assisting a document curator in a records-management system. Based "
    "solely on the document text below, suggest tags for it.\n\n"
    "Respond with strict JSON and nothing else, matching exactly this shape:\n"
    '{{"classification": "<one value from {classifications}>", '
    '"doc_type": "<a short document type, e.g. policy memo, SOP, briefing slide, '
    'data sheet, report>", "program_community": "<a short program/mission-area tag, '
    'or null if none is evident>", "confidence": <number from 0.0 to 1.0>, '
    '"rationale": "<one short sentence explaining the suggestion>"}}\n\n'
    "Document text:\n{text}"
)


@dataclass
class ClassificationSuggestion:
    """The model's raw suggestion, already validated against the configured
    Classification list. `classification` is None if the model's answer
    didn't match any configured value -- callers must treat that as "no
    classification suggestion", not fall back to a guess outside the
    configured vocabulary."""

    classification: str | None
    doc_type: str | None
    program_community: str | None
    confidence: float
    rationale: str | None


def suggestion_enabled() -> bool:
    return bool(CLASSIFICATION_MODEL)


def _match_classification(value: object, classifications: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    for candidate in classifications:
        if candidate.strip().casefold() == value.strip().casefold():
            return candidate
    return None


def _parse_response(raw: str, classifications: list[str]) -> ClassificationSuggestion | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_confidence = data.get("confidence")
    confidence: float
    try:
        confidence = max(0.0, min(1.0, float(raw_confidence)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0.0
    doc_type = data.get("doc_type")
    program_community = data.get("program_community")
    rationale = data.get("rationale")
    return ClassificationSuggestion(
        classification=_match_classification(data.get("classification"), classifications),
        doc_type=doc_type.strip() if isinstance(doc_type, str) and doc_type.strip() else None,
        program_community=(
            program_community.strip()
            if isinstance(program_community, str) and program_community.strip()
            else None
        ),
        confidence=confidence,
        rationale=rationale.strip() if isinstance(rationale, str) and rationale.strip() else None,
    )


async def suggest_classification(
    text: str, *, classifications: list[str]
) -> ClassificationSuggestion | None:
    """Ask CLASSIFICATION_MODEL to zero-shot classify `text`. Returns None if
    the feature is disabled, the model is unreachable, or its response can't
    be parsed into a usable suggestion. Never raises."""
    if not suggestion_enabled():
        return None
    if not classifications:
        # Nothing to match against -- e.g. no active ClassificationLevel rows.
        return None

    prompt = PROMPT_TEMPLATE.format(
        classifications=", ".join(classifications), text=text[:CLASSIFICATION_SCAN_LIMIT]
    )
    try:
        async with httpx.AsyncClient(timeout=CLASSIFICATION_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": CLASSIFICATION_MODEL,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                },
            )
            resp.raise_for_status()
            raw = str(resp.json().get("response", ""))
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("classification suggestion request failed: %s: %s", type(exc).__name__, exc)
        return None

    suggestion = _parse_response(raw, classifications)
    if suggestion is None:
        logger.warning("classification suggestion response was not valid JSON; discarding")
    return suggestion
