"""Issue #215: OIDC_SKIP_VERIFY must not take effect silently.

The other dev-credential fallbacks in this codebase (db.py, job_queue.py,
deps.py) degrade convenience if misapplied. This one disables the signature
check that every Classification/Releasability/Access-scope decision ultimately
rests on -- and a stack running with it set looks entirely healthy, which is
what makes silence the problem rather than the flag itself.
"""

from __future__ import annotations

import importlib
import logging


def _reload_claims(monkeypatch, value: str):
    monkeypatch.setenv("OIDC_SKIP_VERIFY", value)
    import common.claims as claims

    return importlib.reload(claims)


class TestTheWarningFires:
    def test_enabling_it_logs_at_critical(self, monkeypatch, caplog):
        with caplog.at_level(logging.CRITICAL, logger="claims"):
            module = _reload_claims(monkeypatch, "true")

        assert module.OIDC_SKIP_VERIFY is True
        records = [r for r in caplog.records if r.name == "claims"]
        assert records, "enabling OIDC_SKIP_VERIFY must not be silent"
        assert records[0].levelno == logging.CRITICAL, (
            "WARNING is the level real operational noise lives at, so it is "
            "the level this would be scrolled past at"
        )

    def test_the_message_names_the_variable_and_what_it_disables(self, monkeypatch, caplog):
        with caplog.at_level(logging.CRITICAL, logger="claims"):
            _reload_claims(monkeypatch, "true")

        message = next(r.getMessage() for r in caplog.records if r.name == "claims")

        # An operator finding this line in a log needs to know what to unset
        # and what it cost them.
        assert "OIDC_SKIP_VERIFY" in message
        assert "not being verified" in message.lower()
        assert "never be set" in message.lower()


class TestTheDefaultIsQuietAndSafe:
    def test_disabled_by_default_logs_nothing(self, monkeypatch, caplog):
        monkeypatch.delenv("OIDC_SKIP_VERIFY", raising=False)
        with caplog.at_level(logging.DEBUG, logger="claims"):
            import common.claims as claims

            module = importlib.reload(claims)

        assert module.OIDC_SKIP_VERIFY is False
        assert not [r for r in caplog.records if r.name == "claims"]

    def test_a_non_true_value_does_not_enable_it(self, monkeypatch, caplog):
        # Fail-closed on anything that isn't an explicit "true": a typo must
        # not disable signature verification.
        with caplog.at_level(logging.DEBUG, logger="claims"):
            module = _reload_claims(monkeypatch, "yes")

        assert module.OIDC_SKIP_VERIFY is False
        assert not [r for r in caplog.records if r.name == "claims"]


def teardown_module():
    """Leave the module in its default state -- other suites import it."""
    import os

    os.environ.pop("OIDC_SKIP_VERIFY", None)
    import common.claims as claims

    importlib.reload(claims)
