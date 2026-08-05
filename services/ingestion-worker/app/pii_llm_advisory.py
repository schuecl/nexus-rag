"""Issue #343 (Phase 2 of #342): ask the in-cluster LLM (Ollama -- no
external/cloud service, Section 3 origin constraint) to look for
context-dependent sensitive personal/financial information in a document's
own parsed text that the deterministic regex pass (`common/pii_scan.py`,
#342 Phase 1) can't catch -- a spelled-out or differently-formatted SSN, a
non-US national ID number, or freeform personal/financial detail embedded in
prose. Deliberately deferred out of #342 itself per that issue's own
recommendation ("land the regex pass first ... treat the LLM pass as a
follow-on if regex misses prove common in practice").

Issue #378: the same model also verifies Phase 1's own regex findings
(`verify_pii_findings` below). #342's checksum-validated patterns (Luhn for
credit cards, ABA for bank routing) keep any *individual* random digit run's
false-positive chance low (~10%/~11%), but a numeric-heavy technical
document (a manual full of part numbers, page/section references, revision
codes) offers many candidate digit runs, so at the document level a false
flag becomes common rather than rare -- exactly what #378 reported. Rather
than raise the regex pass's bar (which would just start missing genuine
matches), a curator-facing LLM read of the context *around* each flagged
span -- the same sanitized, value-redacted `context` field already shown in
the UI, never the matched value itself -- adds the contextual judgment a
checksum can't make ("Part No." vs "SSN:"). This stays advisory only, same
as everything else in this family: a likely-false-positive verdict is a
second opinion shown alongside the finding, never a suppression of it -- the
curator still sees and decides on every regex match.

Model provisioning follows app/classification_suggestion.py's
CLASSIFICATION_MODEL convention: PII_LLM_MODEL empty (the default) disables
the feature entirely -- no model pull, no LLM calls, no behavior change. Set
to an Ollama text-generation model to enable. Both `suggest_pii_llm_findings`
and `verify_pii_findings` share this one flag -- an operator opting into the
LLM-assisted PII pass gets both halves (new context-dependent findings, and
a context read on the regex ones) from the one model call budget, not two
separate toggles.

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
only, never as markup (see curate.html's advisoryNode). `verify_pii_findings`
has a narrower blast radius still: its prompt embeds only the already-redacted
`context` excerpts Phase 1 produced (the matched value itself was never in
them to begin with), not the document's raw text.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from common.completion_client import request_completion

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

# Regex findings passed to verify_pii_findings are already bounded by
# pii_scan.py's own _MAX_FINDINGS_PER_KIND (5) across at most 5 finding
# kinds, so no separate truncation is needed on the way in -- this only
# guards the number of verdicts accepted back out, same defensive posture as
# _MAX_LLM_FINDINGS above.
_MAX_LLM_VERDICTS = 25

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

# Issue #378: verify Phase 1's own regex findings using only the sanitized,
# value-redacted context excerpt already shown to the curator -- the matched
# value itself was never in that excerpt (see pii_scan.py's _context), so
# there is nothing further to redact going into this prompt.
VERIFY_PROMPT_TEMPLATE = (
    "You are assisting a document curator in a records-management system. A "
    "deterministic pattern scan flagged the numbered spans below as possible "
    "sensitive data (a US Social Security Number, a credit card number, a "
    "bank routing number, an API key/token, or a private-key block). Many "
    "such matches are false positives: a part number, page/section "
    "reference, revision code, serial number, or other numeric identifier in "
    "a technical document that happens to match the pattern by chance.\n\n"
    "For each span you are shown only the text immediately around the match "
    "-- the matched value itself has already been redacted and is not "
    "available to you. Judge each span using only that surrounding context: "
    "does it read like a genuine instance of the kind of sensitive data "
    "named, or like something else that coincidentally matched the "
    "pattern?\n\n"
    "Respond with strict JSON and nothing else, matching exactly this shape:\n"
    '{{"verdicts": [{{"index": <int, matching the number below>, '
    '"likely_false_positive": <true or false>, "rationale": "<one short '
    'sentence, without repeating the sensitive value>"}}]}}\n\n'
    "Spans:\n{spans}"
)


def _format_span(index: int, finding: Mapping[str, object]) -> str:
    kind = finding.get("kind")
    detail = finding.get("detail")
    context = finding.get("context")
    return f"{index}. kind={kind} ({detail}); context: {context}"


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
            raw = await request_completion(
                client, OLLAMA_URL, PII_LLM_MODEL, prompt, json_format=True
            )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("PII LLM advisory request failed: %s: %s", type(exc).__name__, exc)
        return None

    findings = _parse_response(raw)
    if findings is None:
        logger.warning("PII LLM advisory response was not valid JSON; discarding")
    return findings


@dataclass(frozen=True)
class PiiFindingVerdict:
    """The model's read on one Phase 1 regex finding, from context alone.
    Advisory only -- see module docstring. `index` is this finding's
    position in the list passed to `verify_pii_findings`, not `offset` into
    the document: matching by list position is robust to a model that
    mangles an echoed-back offset, whereas the caller already knows which
    finding sits at which index."""

    index: int
    likely_false_positive: bool
    rationale: str | None

    def to_dict(self) -> dict:
        return {
            "likely_false_positive": self.likely_false_positive,
            "rationale": self.rationale,
        }


def _parse_verdicts(raw: str) -> list[PiiFindingVerdict] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_verdicts = data.get("verdicts")
    if not isinstance(raw_verdicts, list):
        return None

    verdicts: list[PiiFindingVerdict] = []
    seen_indices: set[int] = set()
    for item in raw_verdicts[:_MAX_LLM_VERDICTS]:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index in seen_indices:
            continue
        likely_false_positive = item.get("likely_false_positive")
        if not isinstance(likely_false_positive, bool):
            continue
        rationale = item.get("rationale")
        seen_indices.add(index)
        verdicts.append(
            PiiFindingVerdict(
                index=index,
                likely_false_positive=likely_false_positive,
                rationale=rationale.strip()
                if isinstance(rationale, str) and rationale.strip()
                else None,
            )
        )
    return verdicts


async def verify_pii_findings(
    findings: Sequence[Mapping[str, object]],
) -> list[PiiFindingVerdict] | None:
    """Ask PII_LLM_MODEL to judge each of Phase 1's regex `findings` (dicts
    shaped like `common.pii_scan.PiiFinding.to_dict()`) using only their
    already-redacted `context` excerpts -- see module docstring, issue #378.

    Returns None if the feature is disabled, the model is unreachable, or
    its response can't be parsed -- same "unavailable is not the same as
    clean" distinction as `suggest_pii_llm_findings`. An empty `findings`
    input returns `[]` without a model call: there is nothing to verify.
    Never raises.
    """
    if not pii_llm_enabled():
        return None
    if not findings:
        return []

    spans = "\n".join(_format_span(i, f) for i, f in enumerate(findings))
    prompt = VERIFY_PROMPT_TEMPLATE.format(spans=spans)
    try:
        async with httpx.AsyncClient(timeout=PII_LLM_REQUEST_TIMEOUT_SECONDS) as client:
            raw = await request_completion(
                client, OLLAMA_URL, PII_LLM_MODEL, prompt, json_format=True
            )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("PII LLM verification request failed: %s: %s", type(exc).__name__, exc)
        return None

    verdicts = _parse_verdicts(raw)
    if verdicts is None:
        logger.warning("PII LLM verification response was not valid JSON; discarding")
    return verdicts
