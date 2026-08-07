"""Issue #564: the classification/releasability bootstrap vocabulary is
deploy-time configurable, and a malformed override stops the service.

Why these assert what they do: `_seed_defaults` runs unconditionally in the
FastAPI lifespan, so before #564 every environment -- including a production
Helm install against a fresh external Postgres -- silently inherited
REQUIREMENTS.md Section 6.3's dev example vocabulary. A site whose real marking
scheme differs then tags and filters uploads against values that do not exist in
its Keycloak ``rag-clearance:*``/``rag-releasability:*`` roles, and that
mismatch shows up as empty retrieval results rather than an error. Every test
here exists to keep one of the ways that can happen closed.
"""

from __future__ import annotations

import pytest

from app.main import (
    DEFAULT_CLASSIFICATIONS,
    DEFAULT_RELEASABILITY,
    VocabularyConfigError,
    _configured_vocabulary,
    _parse_classification_override,
    _parse_releasability_override,
)
from common.metadata import NO_RELEASABILITY_RESTRICTION


class TestClassificationOverride:
    def test_parses_value_rank_pairs(self):
        assert _parse_classification_override("PUBLIC:0, RESTRICTED:5, TOP:10") == [
            ("PUBLIC", 0),
            ("RESTRICTED", 5),
            ("TOP", 10),
        ]

    def test_rank_is_explicit_not_positional(self):
        """Ranks are taken from the value, not the order they appear in, so a
        reordered env var cannot silently change the clearance ceiling."""
        assert _parse_classification_override("TOP:10,PUBLIC:0") == [("TOP", 10), ("PUBLIC", 0)]

    @pytest.mark.parametrize(
        "raw",
        [
            "UNCLASSIFIED",  # no rank at all
            "UNCLASSIFIED:",  # empty rank
            ":0",  # empty value
            "UNCLASSIFIED:high",  # non-integer rank
            "",  # set but empty
            "   ",
        ],
    )
    def test_malformed_entries_raise(self, raw):
        with pytest.raises(VocabularyConfigError):
            _parse_classification_override(raw)

    def test_duplicate_value_raises(self):
        with pytest.raises(VocabularyConfigError, match="more than once"):
            _parse_classification_override("CUI:1,CUI:2")

    def test_duplicate_rank_raises(self):
        """A reused rank makes the clearance ceiling ambiguous: two levels compare
        equal, so which one a user may read depends on row order."""
        with pytest.raises(VocabularyConfigError, match="rank"):
            _parse_classification_override("CUI:1,SECRET:1")


class TestReleasabilityOverride:
    def test_parses_comma_separated_values(self):
        raw = f"{NO_RELEASABILITY_RESTRICTION},NATO,FVEY"
        assert _parse_releasability_override(raw) == [NO_RELEASABILITY_RESTRICTION, "NATO", "FVEY"]

    def test_must_start_with_the_unset_state(self):
        """The un-set state has to be the first <option> on the upload form, or
        every new document defaults to a coalition caveat nobody chose."""
        with pytest.raises(VocabularyConfigError, match="must start with"):
            _parse_releasability_override("NATO,FVEY")

    def test_duplicate_raises(self):
        raw = f"{NO_RELEASABILITY_RESTRICTION},NATO,NATO"
        with pytest.raises(VocabularyConfigError, match="more than once"):
            _parse_releasability_override(raw)

    def test_empty_raises(self):
        with pytest.raises(VocabularyConfigError):
            _parse_releasability_override(" , ")


class TestConfiguredVocabulary:
    def test_unset_env_keeps_the_dev_defaults(self, monkeypatch):
        """The compose stack must be unchanged by #564."""
        for var in ("CLASSIFICATION_LEVELS", "RELEASABILITY_VALUES", "SEED_DEFAULT_VOCAB"):
            monkeypatch.delenv(var, raising=False)
        assert _configured_vocabulary() == (DEFAULT_CLASSIFICATIONS, DEFAULT_RELEASABILITY)

    def test_opt_out_returns_none(self, monkeypatch):
        monkeypatch.setenv("SEED_DEFAULT_VOCAB", "false")
        assert _configured_vocabulary() is None

    def test_opt_out_is_case_insensitive_and_wins_over_overrides(self, monkeypatch):
        monkeypatch.setenv("SEED_DEFAULT_VOCAB", "False")
        monkeypatch.setenv("CLASSIFICATION_LEVELS", "PUBLIC:0")
        assert _configured_vocabulary() is None

    def test_each_list_overrides_independently(self, monkeypatch):
        monkeypatch.delenv("SEED_DEFAULT_VOCAB", raising=False)
        monkeypatch.setenv("CLASSIFICATION_LEVELS", "PUBLIC:0,RESTRICTED:1")
        monkeypatch.delenv("RELEASABILITY_VALUES", raising=False)
        levels, releasability = _configured_vocabulary()
        assert levels == [("PUBLIC", 0), ("RESTRICTED", 1)]
        assert releasability == DEFAULT_RELEASABILITY

    def test_malformed_override_raises_rather_than_falling_back(self, monkeypatch):
        """The whole point: a typo must stop the service, not quietly seed the dev
        vocabulary a site's Keycloak roles do not match."""
        monkeypatch.delenv("SEED_DEFAULT_VOCAB", raising=False)
        monkeypatch.setenv("CLASSIFICATION_LEVELS", "UNCLASSIFIED:zero")
        with pytest.raises(VocabularyConfigError):
            _configured_vocabulary()
