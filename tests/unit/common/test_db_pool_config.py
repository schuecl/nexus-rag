"""Issue #236 follow-up: DB_POOL_RECYCLE_SECONDS parsing.

The parse runs at import time of ``common.db``, which every service imports at
startup, so a bad value must not raise -- it would crash all three services
with a traceback several layers from its cause. These tests pin the
degrade-loudly behaviour, especially the empty-string case, which is what an
unset key in a .env file (``DB_POOL_RECYCLE_SECONDS=``) produces once Compose
passes it through.
"""

from __future__ import annotations

import pytest

from common.db import DEFAULT_POOL_RECYCLE_SECONDS, _pool_recycle_seconds


class TestPoolRecycleSeconds:
    def test_unset_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("DB_POOL_RECYCLE_SECONDS", raising=False)

        assert _pool_recycle_seconds() == DEFAULT_POOL_RECYCLE_SECONDS

    def test_valid_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "300")

        assert _pool_recycle_seconds() == 300

    def test_minus_one_disables_recycling(self, monkeypatch):
        """SQLAlchemy's own sentinel, so it has to survive validation."""
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "-1")

        assert _pool_recycle_seconds() == -1

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_value_falls_back_instead_of_raising(self, monkeypatch, raw):
        """`DB_POOL_RECYCLE_SECONDS=` in a .env is the realistic failure: Compose
        passes it through as an empty string, and int("") raises. Crashing every
        service at startup over a tuning knob is the wrong trade.
        """
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", raw)

        assert _pool_recycle_seconds() == DEFAULT_POOL_RECYCLE_SECONDS

    def test_non_numeric_falls_back_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "half an hour")

        with caplog.at_level("WARNING"):
            assert _pool_recycle_seconds() == DEFAULT_POOL_RECYCLE_SECONDS

        assert "not an integer" in caplog.text

    @pytest.mark.parametrize("raw", ["0", "-2", "-3600"])
    def test_unusable_intervals_fall_back_with_a_warning(self, monkeypatch, caplog, raw):
        """0 would retire every connection on checkout -- pooling silently off.
        Negative values other than -1 are undefined rather than meaningful.
        """
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", raw)

        with caplog.at_level("WARNING"):
            assert _pool_recycle_seconds() == DEFAULT_POOL_RECYCLE_SECONDS

        assert "not a usable interval" in caplog.text

    def test_a_bad_value_is_never_silently_accepted(self, monkeypatch, caplog):
        """Degrading quietly would be its own trap: the operator sets a value,
        sees no error, and believes it took effect.
        """
        monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "0")

        with caplog.at_level("WARNING"):
            _pool_recycle_seconds()

        assert caplog.records, "a rejected value must be logged"
