"""Issue #138, Phase 1: adaptive/assistive tagging -- the rule-based
marking-mismatch guardrail.

This module is pure and dependency-free: it scans a document's already-parsed
text for CAPCO/DoD-style classification markings (banner lines and portion
marks) and compares what it finds against the tags a human assigned, so a
curator can be *warned* when a document appears to be tagged below its own
markings (the under-classification / spillage case FR-11 exists to control).

Deliberately advisory only. Nothing here decides or applies a tag:
`evaluate_markings` returns findings, and the caller (the ingestion worker)
records them for the curator to confirm or override -- it never mutates the
document's classification. A cleared human stays the classification authority;
this is decision-support, not automation. See the issue for the non-goals.

Detection is intentionally conservative to keep false positives low:
- Banner tokens (SECRET//NOFORN, CUI, UNCLASSIFIED//FOUO) match only when they
  appear in upper case, exactly as a real marking would -- so lower-case prose
  ("keep it secret") is never mistaken for a marking.
- Single-letter abbreviations (S, TS, C, U) are recognised only inside portion
  marks -- "(S)", "(TS//NF)" -- never as bare words, where they would be almost
  entirely noise.
The controlled vocabulary is not hardcoded into the comparison: `evaluate_markings`
ranks detected tokens against the admin-configured ClassificationLevel list
(passed in as a value->rank map), so it adapts when an admin edits that list (C9).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Canonical classification value each recognised token maps to. Keys are the
# forms that may appear in a marking; values are the canonical banner form,
# which `evaluate_markings` matches (case-insensitively) against the configured
# ClassificationLevel.value list. Abbreviations flagged _PORTION_ONLY are only
# honoured inside portion marks -- see _PORTION_ONLY_ALIASES below.
_CANONICAL: dict[str, str] = {
    "TOP SECRET": "TOP SECRET",
    "TS": "TOP SECRET",
    "SECRET": "SECRET",  # nosec B105 -- classification level, not a credential
    "S": "SECRET",
    "CONFIDENTIAL": "CONFIDENTIAL",
    "C": "CONFIDENTIAL",
    "CUI": "CUI",
    "CONTROLLED UNCLASSIFIED INFORMATION": "CUI",
    "UNCLASSIFIED": "UNCLASSIFIED",
    "UNCLAS": "UNCLASSIFIED",
    "U": "UNCLASSIFIED",
}

# Single-letter / ambiguous abbreviations honoured only inside portion marks.
_PORTION_ONLY_ALIASES = frozenset({"TS", "S", "C", "U"})

# Full-word banner tokens, longest-first so "TOP SECRET" wins over "SECRET".
_BANNER_WORDS = [
    "CONTROLLED UNCLASSIFIED INFORMATION",
    "TOP SECRET",
    "UNCLASSIFIED",
    "CONFIDENTIAL",
    "SECRET",
    "UNCLAS",
    "CUI",
]

# Case-sensitive on purpose (no re.IGNORECASE): a real banner marking is upper
# case, and requiring that is what keeps lower-case prose from matching.
# The caveat tail is a run of `//SEGMENT` groups. The segment body excludes
# '/' on purpose: '/' only ever appears as the `//` separator between segments,
# so keeping it out of the character class removes the one-slash-two-ways
# ambiguity that let `//0//0//0...` backtrack exponentially (CodeQL py/redos).
# `_parse_caveats` still splits the captured tail on '/' itself, so a segment
# body never needs to match a bare slash.
_CAVEAT_TAIL = r"((?://[A-Z0-9][A-Z0-9 ,]*)*)"
_BANNER_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_BANNER_WORDS) + r")" + _CAVEAT_TAIL + r"(?![A-Za-z])"
)

# Portion mark: a parenthesised token, optionally with //caveats -- "(U)",
# "(S//NF)", "(TS//REL TO USA, FVEY)". The leading token may be a single letter.
_PORTION_RE = re.compile(r"\(([A-Z][A-Z ]*?)" + _CAVEAT_TAIL + r"\)")

# Caveat tokens recognised inside a //... marking segment. NF is the portion
# abbreviation for NOFORN; both normalise to NOFORN.
_CAVEAT_ALIASES = {"NF": "NOFORN"}
# Words that structure a marking but are not themselves releasability caveats.
_CAVEAT_NOISE = frozenset({"REL", "TO"})


@dataclass(frozen=True)
class MarkingMatch:
    """One classification marking found in the text."""

    classification: str  # canonical banner form, e.g. "SECRET"
    caveats: tuple[str, ...]  # e.g. ("NOFORN",) or ("NATO", "FVEY")
    matched_text: str  # the exact substring matched, for evidence
    offset: int  # character offset into the scanned text


@dataclass(frozen=True)
class DetectedMarkings:
    """Everything detection found, deduplicated for convenience."""

    matches: tuple[MarkingMatch, ...] = ()

    @property
    def classifications(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.matches:
            if m.classification not in seen:
                seen.append(m.classification)
        return tuple(seen)

    @property
    def caveats(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.matches:
            for c in m.caveats:
                if c not in seen:
                    seen.append(c)
        return tuple(seen)


def _parse_caveats(segment: str) -> tuple[str, ...]:
    """Pull releasability caveats out of a `//...//...` marking tail."""
    caveats: list[str] = []
    # Split on whitespace as well as / and , so "REL TO USA, FVEY" tokenises to
    # REL / TO / USA / FVEY -- the REL/TO structure words are then dropped as
    # noise, leaving the coalition caveats themselves.
    for raw in re.split(r"[\s/,]+", segment):
        token = raw.strip()
        if not token or token in _CAVEAT_NOISE:
            continue
        token = _CAVEAT_ALIASES.get(token, token)
        if token not in caveats:
            caveats.append(token)
    return tuple(caveats)


def detect_markings(text: str) -> DetectedMarkings:
    """Scan `text` for classification banner and portion markings."""
    if not text:
        return DetectedMarkings()

    matches: list[MarkingMatch] = []

    for m in _BANNER_RE.finditer(text):
        token = m.group(1)
        canonical = _CANONICAL.get(token)
        if canonical is None:
            continue
        matches.append(
            MarkingMatch(
                classification=canonical,
                caveats=_parse_caveats(m.group(2)),
                matched_text=m.group(0).strip(),
                offset=m.start(),
            )
        )

    for m in _PORTION_RE.finditer(text):
        token = m.group(1).strip()
        canonical = _CANONICAL.get(token)
        if canonical is None:
            continue
        # A multi-letter portion token that isn't a portion-only alias (e.g.
        # "SECRET") is already covered by the banner pass at a nearby offset;
        # only the single-letter/ambiguous abbreviations are unique here.
        if token not in _PORTION_ONLY_ALIASES and any(
            token in existing.matched_text for existing in matches
        ):
            continue
        matches.append(
            MarkingMatch(
                classification=canonical,
                caveats=_parse_caveats(m.group(2)),
                matched_text=m.group(0),
                offset=m.start(),
            )
        )

    matches.sort(key=lambda mm: mm.offset)
    return DetectedMarkings(matches=tuple(matches))


@dataclass
class TaggingAdvisory:
    """Advisory result surfaced to the curator (issue #138). Never applied."""

    assigned_classification: str
    detected_classification: str | None = None
    under_classified: bool = False
    detected_caveats: list[str] = field(default_factory=list)
    unassigned_caveats: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return self.under_classified or bool(self.unassigned_caveats)

    def to_dict(self) -> dict:
        return {
            "assigned_classification": self.assigned_classification,
            "detected_classification": self.detected_classification,
            "under_classified": self.under_classified,
            "detected_caveats": self.detected_caveats,
            "unassigned_caveats": self.unassigned_caveats,
            "evidence": self.evidence,
            "notes": self.notes,
        }


def evaluate_markings(
    *,
    assigned_classification: str,
    assigned_releasability: list[str],
    detected: DetectedMarkings,
    rank_by_value: Mapping[str, int],
) -> TaggingAdvisory:
    """Compare detected markings against the human-assigned tags (issue #138).

    `rank_by_value` is the admin-configured ClassificationLevel list as a
    value->rank map (higher rank = more sensitive). Detected tokens that aren't
    in it are ignored -- we can only compare against configured levels. The
    result is advisory: `under_classified` means the document's own markings
    appear *higher* than the tag a human chose, which a curator should confirm.
    """
    ranks = {value.upper(): rank for value, rank in rank_by_value.items()}
    advisory = TaggingAdvisory(assigned_classification=assigned_classification)

    assigned_rank = ranks.get(assigned_classification.upper())
    if assigned_rank is None:
        advisory.notes.append(
            f"assigned classification '{assigned_classification}' is not in the "
            "configured classification list; classification mismatch not evaluated"
        )
    else:
        # Highest detected marking that maps to a configured level.
        best_rank = -1
        for match in detected.matches:
            rank = ranks.get(match.classification.upper())
            if rank is None:
                continue
            if rank > best_rank:
                best_rank = rank
                advisory.detected_classification = match.classification
        if best_rank > assigned_rank:
            advisory.under_classified = True
            advisory.evidence = [
                m.matched_text
                for m in detected.matches
                if ranks.get(m.classification.upper(), -1) == best_rank
            ][:5]

    held = {c.upper() for c in assigned_releasability}
    advisory.detected_caveats = list(detected.caveats)
    advisory.unassigned_caveats = [
        c for c in detected.caveats if c.upper() not in held
    ]
    return advisory
