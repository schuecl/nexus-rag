"""Issue #284, item 2: an advisory, non-blocking heuristic that flags a
parsed document's own content for hidden-instruction risk -- the
zero-width/invisible-Unicode and control-character tricks used to smuggle
text a human reviewer cannot see, plus a short list of common prompt-injection
trigger phrases (OWASP RAG security cheat sheet; Arzanipour et al., "RAG
Security and Privacy: Formalizing the Threat Model and Attack Surface",
arXiv:2509.20324). Same posture as common/marking_detection.py (issue #138):
pure, dependency-free, and advisory only -- `detect_content_risks` returns
findings for a curator to look at; nothing here decides, blocks, or corrects
anything. Without a content view (issue #284) this was the only way a
curator could learn a document carries hidden text at all; with one, it
directs attention to where in the now-visible text to look.

Detection is deliberately narrow to keep false positives low, the same
tradeoff marking_detection.py makes:

- Only Unicode categories Cc (control) and Cf (format) are flagged. Cf is
  the category real "invisible prompt injection" text uses -- zero-width
  joiners, bidi-override characters, and the Unicode Tag block
  (U+E0000-U+E007F) the "ASCII smuggling" technique hides text inside.
  Categories like Zs/Zl/Zp/Co/Cs are left alone on purpose: NBSP and other
  Unicode space variants are common, legitimate output of Word/PDF exports,
  and flagging them would swamp a curator with noise for no security value.
- SOFT HYPHEN (U+00AD, ordinary hyphenation) and a leading BOM (U+FEFF at
  offset 0, an encoding artifact, not hidden content) are excluded.
- Injection-marker phrases are matched as plain case-insensitive substrings
  from a small, fixed list, never a general-purpose regex grammar -- avoids
  both false positives from an over-eager pattern and the ReDoS surface a
  hand-rolled catastrophic-backtracking regex could introduce (see
  marking_detection.py's own _CAVEAT_TAIL comment for that history).

Evidence strings shown to the curator never echo a matched invisible/control
character or its surrounding raw text back verbatim -- that would just
re-hide the same character inside the advisory meant to reveal it. Instead
each finding reports the Unicode codepoint/name (or, for a phrase match, the
fixed phrase text itself, never attacker-controlled), plus a context window
with every non-printable character escaped to \\uXXXX.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Cf characters known to be benign/typographic rather than hidden-instruction
# vectors -- excluded so ordinary real-world documents don't get flagged.
# Written as \u escapes, not the literal characters, so the source file
# itself never contains an invisible character.
_BENIGN_CF = frozenset({"\u00ad"})  # SOFT HYPHEN
_LEADING_BOM = "\ufeff"  # benign only as a byte-order mark at offset 0

# How much sanitized context to show on either side of a flagged offset.
_CONTEXT_RADIUS = 20

# Deliberately plain substrings, not regex -- see module docstring.
_INJECTION_PHRASES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore the above instructions",
    "disregard previous instructions",
    "disregard all previous instructions",
    "disregard the above instructions",
    "new instructions:",
    "you are now a",
    "you are now an",
    "act as if you are",
    "do not tell the user",
    "do not inform the user",
    "do not alert the curator",
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "[system]",
    "### instruction",
)

# How many findings of each kind to keep -- an adversarial document could
# repeat one character or phrase thousands of times; the curator needs to
# know it's present, not see every occurrence (same cap pattern as
# marking_detection.evaluate_markings' 5-item evidence list).
_MAX_FINDINGS_PER_KIND = 5


@dataclass(frozen=True)
class ContentFinding:
    """One hidden-instruction-risk signal found in parsed document text."""

    kind: str  # "invisible_unicode" | "control_character" | "injection_marker"
    detail: str  # codepoint/name, or the matched phrase -- never attacker text
    context: str  # sanitized text window around the offset
    offset: int

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "context": self.context,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class ContentAdvisory:
    """Everything detection found. Advisory only -- see module docstring."""

    findings: tuple[ContentFinding, ...] = ()

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings]}


def _context(text: str, offset: int, length: int = 1) -> str:
    start = max(0, offset - _CONTEXT_RADIUS)
    end = min(len(text), offset + length + _CONTEXT_RADIUS)
    window = text[start:end]
    # Never echo a non-printable character back into evidence shown to the
    # curator -- that would just re-hide it inside the advisory meant to
    # reveal it. \t/\n are common and harmless as prose context.
    return "".join(ch if ch.isprintable() or ch in "\t\n" else f"\\u{ord(ch):04x}" for ch in window)


def _flag_char(ch: str, offset: int) -> str | None:
    if ch in "\t\n\r":
        return None
    category = unicodedata.category(ch)
    if category == "Cc":
        return "control_character"
    if category == "Cf":
        if ch in _BENIGN_CF or (ch == _LEADING_BOM and offset == 0):
            return None
        return "invisible_unicode"
    return None


def detect_content_risks(text: str) -> ContentAdvisory:
    """Scan `text` for invisible/control characters and injection-marker
    phrases. Pure and side-effect-free -- the caller decides what to do with
    the result, same contract as marking_detection.detect_markings."""
    if not text:
        return ContentAdvisory()

    findings: list[ContentFinding] = []
    counts: dict[str, int] = {}

    for offset, ch in enumerate(text):
        kind = _flag_char(ch, offset)
        if kind is None or counts.get(kind, 0) >= _MAX_FINDINGS_PER_KIND:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        # Control characters below U+0020 have no assigned Unicode Name
        # property (unicodedata.name raises ValueError for them) -- fall back
        # to just the codepoint rather than repeating it as a fake "name".
        name = unicodedata.name(ch, "")
        detail = f"U+{ord(ch):04X}" + (f" {name}" if name else "")
        findings.append(
            ContentFinding(
                kind=kind,
                detail=detail,
                context=_context(text, offset),
                offset=offset,
            )
        )

    lowered = text.casefold()
    for phrase in _INJECTION_PHRASES:
        needle = phrase.casefold()
        start = 0
        while counts.get("injection_marker", 0) < _MAX_FINDINGS_PER_KIND:
            idx = lowered.find(needle, start)
            if idx == -1:
                break
            counts["injection_marker"] = counts.get("injection_marker", 0) + 1
            findings.append(
                ContentFinding(
                    kind="injection_marker",
                    detail=phrase,
                    context=_context(text, idx, length=len(phrase)),
                    offset=idx,
                )
            )
            start = idx + len(needle)

    findings.sort(key=lambda f: f.offset)
    return ContentAdvisory(findings=tuple(findings))
