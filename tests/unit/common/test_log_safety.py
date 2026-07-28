"""Coverage for the log-injection escaping CodeQL flagged on #136.

The concern is concrete: `actor_username` comes from the OIDC
`preferred_username` claim, so a value containing a newline could forge what
reads as a separate, earlier log entry -- and once the audit trail is exported
(#73) or shipped to a log aggregator (#133), a forged line arrives already
indexed alongside genuine ones.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine

from common import purge as purge_mod
from common.log_safety import log_safe
from common.models import Document

FORGED = "mallory\n2026-01-01 00:00:00 WARNING document 0000 purged by admin"


class TestLogSafe:
    def test_newlines_are_escaped_not_stripped(self):
        """Escaped rather than removed, so the attempt stays visible in the
        log instead of being silently swallowed."""
        out = log_safe("a\nb")

        assert "\n" not in out
        assert out == "a\\x0ab"

    @pytest.mark.parametrize("ch", ["\r", "\x00", "\x1b", "\x7f"])
    def test_other_control_characters_are_escaped(self, ch):
        assert ch not in log_safe(f"x{ch}y")

    def test_a_forged_log_line_cannot_survive(self):
        out = log_safe(FORGED)

        assert "\n" not in out
        assert out.count("\\x0a") == 1

    def test_printable_unicode_is_left_alone(self):
        """A username in a non-Latin script is not an attack; mangling it
        would be its own bug."""
        assert log_safe("Ünïcode-用户") == "Ünïcode-用户"

    def test_non_strings_are_rendered(self):
        u = uuid.uuid4()

        assert log_safe(u) == str(u)
        assert log_safe(7) == "7"


class TestPurgeLogging:
    def test_a_hostile_username_cannot_forge_a_log_line(self, monkeypatch, caplog):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        class _Store:
            def delete(self, _k):
                return None

        class _FakeStore:  # #160: purge goes through the vector-store seam now
            def delete_document_chunks(self, _i):
                return None

        monkeypatch.setattr(purge_mod, "get_store", _FakeStore)
        monkeypatch.setattr(purge_mod, "get_object_store", _Store)

        with Session(engine) as db:
            d = Document(
                filename="f.pdf",
                uploader_sub="a",
                uploader_username="a",
                owner_org="o",
                classification="CUI",
                releasability=["FVEY"],
                access_scope=["o"],
                source_originator="s",
                doc_type="report",
                original_object_key="k",
                status="approved",
            )
            db.add(d)
            db.commit()

            with caplog.at_level(logging.WARNING, logger="purge"):
                purge_mod.purge_document(db, d.id, actor_sub="m", actor_username=FORGED, reason="r")

        engine.dispose()

        record = next(r for r in caplog.records if r.name == "purge")
        assert "\n" not in record.getMessage(), "a claim value must not break the log line"
