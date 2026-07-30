"""Issue #213: application-layer encryption for the OIDC access/refresh/id
tokens UserSession (common/models.py) stores server-side. A read-only
compromise of the app database alone must not yield live, usable Keycloak
credentials -- see the issue for why dropping or hashing the tokens instead
isn't viable (`refresh_token` drives real silent renewal in
app/deps.py._refresh_session, and `access_token` is forwarded verbatim as a
bearer token to downstream calls, so both have to remain retrievable in
plaintext by the app).

EncryptedString is a SQLAlchemy TypeDecorator, not a change to how callers
use these columns: encryption happens at bind time (Python str -> ciphertext
going into the DB) and decryption at load time (ciphertext -> Python str
coming out), so routes/auth.py and app/deps.py keep reading and writing
row.access_token/.refresh_token/.id_token as plain strings.

Fernet (AES-128-CBC + HMAC, from the `cryptography` package already a
project dependency) rather than a hash: the whole point is these values stay
retrievable, so a one-way function is off the table -- see #213's rejection
of "store a hash of the access token" for the same reason.

Issue #281 gap G5 stage 2: SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS, if set,
is accepted for decryption alongside the primary key via `MultiFernet`, so
rotating the primary key doesn't instantly strand every session created
under the old one. New writes always encrypt under the primary key; a row
still under the previous key gets re-encrypted under the primary the next
time it's written (routes/auth.py's session refresh already rewrites
access_token/refresh_token/id_token periodically) -- there's no separate
migration pass, since UserSession rows are ephemeral and bounded by
SESSION_LIFETIME anyway (see the comment below). Retire the previous key
once you don't need sessions older than that to keep working.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

# Dev-only default, same convention as every other dev-* secret in
# .env.example (QDRANT_API_KEY, NATS credentials, ...) -- a real deployment
# must set SESSION_TOKEN_ENCRYPTION_KEY from its secret store (Helm's
# sessionTokenEncryption.existingSecret). Losing this key makes every stored
# session unreadable, which just forces re-login (UserSession rows are
# ephemeral, bounded by SESSION_LIFETIME) -- not a data-loss event the way
# losing a key over durable document content would be.
_DEV_DEFAULT_KEY = "VDwCC09I-Kf3YNJMRhkUN1bkC0bCXc9cxh7agmNqsIM="


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    key = os.environ.get("SESSION_TOKEN_ENCRYPTION_KEY", _DEV_DEFAULT_KEY)
    keys = [Fernet(key.encode())]
    previous_key = os.environ.get("SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS", "")
    if previous_key:
        keys.append(Fernet(previous_key.encode()))
    return MultiFernet(keys)


class EncryptedString(TypeDecorator[str]):
    """A String column whose value is Fernet-encrypted at rest. `cache_ok`
    is safe here: the type's encryption behavior depends only on the
    process's env var, not on any per-instance state SQLAlchemy would need
    to key its statement cache on."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return _fernet().decrypt(value.encode()).decode()
