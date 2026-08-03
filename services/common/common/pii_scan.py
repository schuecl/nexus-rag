"""Issue #342, Phase 1: an advisory, non-blocking regex scan for common
sensitive-data patterns (US SSN, credit card numbers, bank routing numbers,
API keys/tokens, private-key blocks) in a parsed document's own text -- a
curator-facing signal that a document may carry sensitive content the
uploader didn't intend to include, independent of and in addition to
Classification/Releasability tagging. Same posture as
common/marking_detection.py (issue #138) and common/content_advisory.py
(issue #284 item 2): pure, dependency-free, and advisory only --
`detect_pii_risks` returns findings for a curator to look at; nothing here
decides, blocks, corrects, or redacts anything. See docs/governance.md's
Non-goals section for why this is scoped as a flag-only spillage-adjacent
signal, not PII redaction or GDPR-style data-subject-rights tooling.

Two detection layers were proposed in the issue -- a deterministic regex
pass (this module) and a follow-on LLM-assisted pass for context-dependent
PII a regex can't catch (spelled-out SSNs, foreign ID formats, freeform PII
in prose). Only the regex pass is implemented here, per the issue's own
recommendation to land it first: self-contained, deterministic, testable
without a model, air-gap-friendly (no model call at all).

Detection is deliberately narrow to keep false positives low, the same
tradeoff marking_detection.py and content_advisory.py both make:

- SSN: only the dashed `###-##-####` form, with the same invalid-range
  exclusions the SSA itself defines (area 000/666/900-999, group 00,
  serial 0000) -- an unformatted 9-digit run is not scanned at all, since
  nothing distinguishes it from an arbitrary 9-digit number in a technical
  document (a part number, a case ID) without far more context than a
  regex has.
- Credit card: a digit run of 13-19 digits (optionally space/dash grouped)
  that passes the Luhn checksum -- the same validation a real payment form
  runs client-side, so a random 16-digit number in a table has only a
  ~10% chance of a false positive rather than matching on length alone.
- Bank routing number: a standalone 9-digit run that passes the ABA
  routing-number checksum -- again cutting an otherwise-noisy 9-digit scan
  down to roughly the same ~10% false-positive rate as an unvalidated
  length match.
- API keys / access tokens: a fixed set of well-known vendor key prefixes
  (AWS, GitHub, Slack) matched literally, plus a generic
  `key/token/secret = "..."`-shaped assignment for anything else. A PEM
  private-key block header (`-----BEGIN ... PRIVATE KEY-----`) is matched
  literally.
- Phone numbers and email addresses are deliberately not scanned here --
  the issue itself calls these lower priority, since they are often
  legitimately present in organizational documents (a POC line, a contact
  block) and would swamp a curator with noise for little spillage-relevant
  signal.
- Passport/driver's-license numbers are out of scope for this phase --
  format varies by issuing state/country with no single reliable pattern,
  called out in the issue as a stretch goal, not landed here.

Evidence shown to the curator never echoes the sensitive value itself, even
in part: `detail` names only the kind of pattern matched (never a value
fragment), and `context` shows the surrounding text with the matched span
itself replaced by a fixed-width redaction marker -- the same "never
re-hide the very thing this exists to reveal" posture content_advisory.py
uses for invisible-Unicode findings, applied here to the sensitive value
itself rather than to a control character.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# How much sanitized context to show on either side of a flagged span.
_CONTEXT_RADIUS = 20

# Marker used in place of the matched sensitive value inside a context
# excerpt -- never the value itself, see module docstring.
_REDACTED = "[REDACTED]"

# How many findings of each kind to keep -- a document could repeat one
# pattern many times (e.g. a table of card numbers); the curator needs to
# know the pattern is present, not see every occurrence (same cap pattern as
# content_advisory.detect_content_risks / marking_detection.evaluate_markings).
_MAX_FINDINGS_PER_KIND = 5

_SSN_RE = re.compile(
    r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b",
)

# 13-19 digits, optionally grouped with spaces or dashes -- the range of
# real card-number lengths (Visa/Mastercard 16, Amex 15, some debit 13/19).
_CANDIDATE_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

_ROUTING_RE = re.compile(r"\b\d{9}\b")

_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_GENERIC_SECRET_RE = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret)\b
    \s*[:=]\s*
    ["']?([A-Za-z0-9_\-/+=]{20,})["']?
    """,
)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)


@dataclass(frozen=True)
class PiiFinding:
    """One sensitive-pattern signal found in parsed document text."""

    kind: str  # "ssn" | "credit_card" | "bank_routing" | "api_key" | "private_key_block"
    detail: str  # human-readable label for the kind -- never the matched value
    context: str  # sanitized text window with the match itself redacted
    offset: int

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "context": self.context,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class PiiAdvisory:
    """Everything detection found. Advisory only -- see module docstring."""

    findings: tuple[PiiFinding, ...] = ()

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings]}


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _aba_routing_valid(digits: str) -> bool:
    d = [int(c) for c in digits]
    checksum = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + 1 * (d[2] + d[5] + d[8])
    return checksum % 10 == 0


def _context(text: str, start: int, end: int) -> str:
    window_start = max(0, start - _CONTEXT_RADIUS)
    window_end = min(len(text), end + _CONTEXT_RADIUS)
    before = text[window_start:start]
    after = text[end:window_end]
    # Never echo the matched value back into evidence shown to the curator --
    # that would defeat the point of flagging it. \t/\n stay as-is; every
    # other non-printable character is escaped, same convention as
    # content_advisory._context.
    sanitize = lambda s: "".join(  # noqa: E731
        ch if ch.isprintable() or ch in "\t\n" else f"\\u{ord(ch):04x}" for ch in s
    )
    return f"{sanitize(before)}{_REDACTED}{sanitize(after)}"


class _FindingCollector:
    def __init__(self) -> None:
        self._findings: list[PiiFinding] = []
        self._counts: dict[str, int] = {}

    def add(self, text: str, kind: str, detail: str, start: int, end: int) -> None:
        if self._counts.get(kind, 0) >= _MAX_FINDINGS_PER_KIND:
            return
        self._counts[kind] = self._counts.get(kind, 0) + 1
        self._findings.append(
            PiiFinding(kind=kind, detail=detail, context=_context(text, start, end), offset=start)
        )

    def result(self) -> tuple[PiiFinding, ...]:
        return tuple(sorted(self._findings, key=lambda f: f.offset))


def detect_pii_risks(text: str) -> PiiAdvisory:
    """Scan `text` for common sensitive-data patterns. Pure and
    side-effect-free -- the caller decides what to do with the result, same
    contract as content_advisory.detect_content_risks and
    marking_detection.detect_markings."""
    if not text:
        return PiiAdvisory()

    collector = _FindingCollector()

    for match in _SSN_RE.finditer(text):
        collector.add(text, "ssn", "US Social Security Number pattern", *match.span())

    for match in _CANDIDATE_CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            collector.add(
                text, "credit_card", "Credit card number pattern (Luhn-valid)", *match.span()
            )

    for match in _ROUTING_RE.finditer(text):
        if _aba_routing_valid(match.group()):
            collector.add(
                text,
                "bank_routing",
                "Bank routing number pattern (ABA checksum-valid)",
                *match.span(),
            )

    for pattern, label in (
        (_AWS_ACCESS_KEY_RE, "AWS access key ID pattern"),
        (_GITHUB_TOKEN_RE, "GitHub access token pattern"),
        (_SLACK_TOKEN_RE, "Slack token pattern"),
    ):
        for match in pattern.finditer(text):
            collector.add(text, "api_key", label, *match.span())

    for match in _GENERIC_SECRET_RE.finditer(text):
        collector.add(text, "api_key", "possible API key/token/secret assignment", *match.span())

    for match in _PRIVATE_KEY_BLOCK_RE.finditer(text):
        collector.add(text, "private_key_block", "PEM private-key block header", *match.span())

    return PiiAdvisory(findings=collector.result())
