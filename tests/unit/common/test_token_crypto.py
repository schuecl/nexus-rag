"""Issue #213: UserSession's access/refresh/id tokens are encrypted at rest
via EncryptedString (common/token_crypto.py). These pin down the property
that actually matters -- a plaintext token never reaches the database -- not
just that round-tripping happens to work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from common.models import UserSession
from common.token_crypto import EncryptedString, _fernet


class TestEncryptedStringTypeDecorator:
    def test_none_binds_and_reads_back_as_none(self):
        column = EncryptedString()

        assert column.process_bind_param(None, None) is None
        assert column.process_result_value(None, None) is None

    def test_a_value_is_encrypted_on_bind(self):
        column = EncryptedString()
        plaintext = "eyJhbGciOiJSUzI1NiJ9.super-secret-access-token"

        ciphertext = column.process_bind_param(plaintext, None)

        assert ciphertext != plaintext
        assert plaintext not in ciphertext

    def test_a_bound_value_decrypts_back_to_the_original(self):
        column = EncryptedString()
        plaintext = "super-secret-refresh-token"

        ciphertext = column.process_bind_param(plaintext, None)

        assert column.process_result_value(ciphertext, None) == plaintext

    def test_two_encryptions_of_the_same_value_differ(self):
        """Fernet includes a random IV/nonce per encryption -- if this ever
        started producing identical ciphertext for identical plaintext, that
        would leak which sessions share a token."""
        column = EncryptedString()

        first = column.process_bind_param("same-token", None)
        second = column.process_bind_param("same-token", None)

        assert first != second


class TestUserSessionPersistsEncrypted:
    def test_the_raw_database_value_is_not_the_plaintext_token(self):
        """The property #213 exists for: a read-only compromise of the
        database (raw SQL, not the ORM) must not yield a usable token."""
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                session.add(
                    UserSession(
                        id="sid",
                        access_token="plaintext-access-token",
                        refresh_token="plaintext-refresh-token",
                        id_token="plaintext-id-token",
                        expires_at=datetime.now(UTC) + timedelta(minutes=15),
                    )
                )
                session.commit()

            raw_row = (
                engine.raw_connection()
                .execute(
                    "SELECT access_token, refresh_token, id_token FROM user_sessions"
                    " WHERE id = 'sid'"
                )
                .fetchone()
            )

            assert "plaintext-access-token" not in raw_row[0]
            assert "plaintext-refresh-token" not in raw_row[1]
            assert "plaintext-id-token" not in raw_row[2]
        finally:
            engine.dispose()

    def test_reading_it_back_through_the_orm_returns_the_plaintext(self):
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                session.add(
                    UserSession(
                        id="sid",
                        access_token="plaintext-access-token",
                        refresh_token="plaintext-refresh-token",
                        expires_at=datetime.now(UTC) + timedelta(minutes=15),
                    )
                )
                session.commit()

            with Session(engine) as session:
                row = session.exec(select(UserSession)).one()

                assert row.access_token == "plaintext-access-token"
                assert row.refresh_token == "plaintext-refresh-token"
                assert row.id_token is None
        finally:
            engine.dispose()


class TestFernetKeyIsCached:
    def test_repeated_calls_return_the_same_instance(self):
        """Just documents the @lru_cache -- a Fernet instance is
        stateless/thread-safe, so re-instantiating per call would be pure
        waste with no correctness benefit."""
        assert _fernet() is _fernet()
