"""Unit tests for common.marking_detection -- the issue #138 Phase 1
marking-mismatch guardrail. Pure functions, no DB: detection runs on strings
and evaluation takes a plain value->rank map, so these mirror the configured
vocabulary from docs/dev-setup.md (UNCLASSIFIED < CUI < SECRET) without a live
Postgres.
"""

from __future__ import annotations

from common.marking_detection import (
    detect_markings,
    evaluate_markings,
)

# Deployment vocabulary per conftest.py / realm-export: three ranked levels.
RANKS = {"UNCLASSIFIED": 1, "CUI": 2, "SECRET": 3}


class TestDetectBanners:
    def test_plain_banner(self):
        det = detect_markings("SECRET\n\nThe report follows.")
        assert det.classifications == ("SECRET",)

    def test_banner_with_caveat(self):
        det = detect_markings("SECRET//NOFORN")
        assert det.classifications == ("SECRET",)
        assert det.caveats == ("NOFORN",)

    def test_rel_to_caveats(self):
        det = detect_markings("SECRET//REL TO USA, FVEY")
        assert det.classifications == ("SECRET",)
        # REL/TO are structural noise; the coalition tokens are the caveats.
        assert set(det.caveats) == {"USA", "FVEY"}

    def test_cui_and_unclassified(self):
        assert detect_markings("CUI").classifications == ("CUI",)
        assert detect_markings("UNCLASSIFIED//FOUO").classifications == ("UNCLASSIFIED",)

    def test_hyphenated_cui_control_marking_consumed_whole(self):
        # PR #2 review: "CUI//SP-CTI//NOFORN" used to stop matching at the
        # first hyphen ("CUI//SP"), inventing an SP caveat and dropping the
        # NOFORN restriction entirely.
        det = detect_markings("CUI//SP-CTI//NOFORN")
        assert det.classifications == ("CUI",)
        assert set(det.caveats) == {"SP-CTI", "NOFORN"}

    def test_top_secret_wins_over_secret(self):
        # Longest-token-first: the string contains "SECRET" as a substring of
        # "TOP SECRET" but must be read as TOP SECRET, once.
        det = detect_markings("TOP SECRET//NOFORN")
        assert det.classifications == ("TOP SECRET",)

    def test_lowercase_prose_is_not_a_marking(self):
        # The whole point of the case-sensitive banner match.
        assert detect_markings("Please keep this secret and confidential.").matches == ()

    def test_word_boundary_not_tripped_by_substring(self):
        # "SECRETARIAT" must not read as a SECRET banner.
        assert detect_markings("THE SECRETARIAT MET TODAY").classifications == ()


class TestDetectPortionMarks:
    def test_single_letter_portion_marks(self):
        det = detect_markings("(U) Intro. (S) The sensitive part. (U) Closing.")
        assert set(det.classifications) == {"UNCLASSIFIED", "SECRET"}

    def test_portion_mark_with_caveat(self):
        det = detect_markings("(S//NF) sensitive")
        assert det.classifications == ("SECRET",)
        assert det.caveats == ("NOFORN",)  # NF normalises to NOFORN

    def test_bare_single_letters_in_prose_ignored(self):
        # S / U as bare words are noise; only recognised inside parens.
        assert detect_markings("Section S and part U of the manual").matches == ()

    def test_ts_portion_alias(self):
        det = detect_markings("(TS//REL TO NATO) text")
        assert det.classifications == ("TOP SECRET",)
        assert det.caveats == ("NATO",)


class TestEvaluateMarkings:
    def _detect_eval(self, text, assigned, releasability=None):
        return evaluate_markings(
            assigned_classification=assigned,
            assigned_releasability=releasability or ["NONE"],
            detected=detect_markings(text),
            rank_by_value=RANKS,
        )

    def test_under_classification_flagged(self):
        adv = self._detect_eval("SECRET//NOFORN", assigned="CUI", releasability=["NOFORN"])
        assert adv.under_classified is True
        assert adv.detected_classification == "SECRET"
        assert adv.has_findings is True
        assert "SECRET//NOFORN" in adv.evidence

    def test_matching_classification_not_flagged(self):
        adv = self._detect_eval("SECRET", assigned="SECRET", releasability=["NONE"])
        assert adv.under_classified is False

    def test_over_classification_not_flagged(self):
        # Document marked CUI but tagged SECRET -- not a spillage risk, so the
        # guardrail stays quiet on the classification axis.
        adv = self._detect_eval("CUI", assigned="SECRET")
        assert adv.under_classified is False

    def test_highest_detected_marking_wins(self):
        adv = self._detect_eval("(U) intro (S) body (U) end", assigned="CUI")
        assert adv.detected_classification == "SECRET"
        assert adv.under_classified is True

    def test_unconfigured_detected_level_ignored(self):
        # TOP SECRET isn't in this deployment's ranked list, so it can't be
        # ranked/compared -- must not crash or spuriously flag.
        adv = self._detect_eval("TOP SECRET", assigned="CUI")
        assert adv.under_classified is False

    def test_unconfigured_assigned_classification_notes_not_flags(self):
        adv = evaluate_markings(
            assigned_classification="BOGUS",
            assigned_releasability=["NONE"],
            detected=detect_markings("SECRET"),
            rank_by_value=RANKS,
        )
        assert adv.under_classified is False
        assert adv.notes  # explains why classification wasn't evaluated

    def test_unassigned_caveat_flagged(self):
        adv = self._detect_eval("SECRET//NOFORN", assigned="SECRET", releasability=["NONE"])
        assert "NOFORN" in adv.unassigned_caveats
        assert adv.has_findings is True

    def test_caveat_already_held_not_flagged(self):
        adv = self._detect_eval("SECRET//REL TO NATO", assigned="SECRET", releasability=["NATO"])
        assert adv.unassigned_caveats == []

    def test_known_caveats_scopes_unassigned_to_configured_vocabulary(self):
        # PR #2 review: a CUI control segment (SP-CTI) is not a releasability
        # value; with the configured vocabulary provided it must not be flagged
        # as "missing releasability" -- while a genuine configured caveat the
        # uploader didn't assign (NOFORN) still is. Both stay visible in
        # detected_caveats.
        adv = evaluate_markings(
            assigned_classification="CUI",
            assigned_releasability=["NONE"],
            detected=detect_markings("CUI//SP-CTI//NOFORN"),
            rank_by_value=RANKS,
            known_caveats=["NONE", "NOFORN", "USA", "NATO", "FVEY"],
        )
        assert adv.unassigned_caveats == ["NOFORN"]
        assert set(adv.detected_caveats) == {"SP-CTI", "NOFORN"}

    def test_clean_document_has_no_findings(self):
        adv = self._detect_eval("This is ordinary prose with no markings.", assigned="CUI")
        assert adv.has_findings is False
        assert adv.to_dict()["under_classified"] is False
