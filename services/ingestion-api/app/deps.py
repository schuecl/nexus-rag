from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from common.claims import OIDC_ISSUERS, UserClaims, parse_claims
from common.classification import allowed_classifications as _allowed_classifications
from common.db import get_session
from common.models import UserSession
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

SESSION_COOKIE = "nexus_rag_session"
# NFR-14: double-submit CSRF cookie, set alongside SESSION_COOKIE at login
# (routes/auth.py's callback()) -- deliberately NOT HttpOnly, since the page's
# own JS has to read it and echo it back as a header (base.html's
# authHeaders()). A cross-site attacker can still make the browser *send*
# nexus_rag_session on a forged request (that's the CSRF scenario this
# defends), but can't read this cookie's value to forge the matching header,
# and can't attach custom headers to a cross-site request in the first place
# without triggering a CORS preflight this app doesn't allow.
CSRF_COOKIE = "nexus_rag_csrf"
CSRF_HEADER = "X-CSRF-Token"
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "rag-app")
OIDC_CLIENT_SECRET = os.environ.get("RAG_APP_KEYCLOAK_CLIENT_SECRET", "dev-rag-app-secret")
# Token endpoint calls (auth-code exchange in routes/auth.py, refresh below)
# are server-to-server, so they use the container-reachable issuer --
# OIDC_ISSUERS[0] -- same as the JWKS fetch in claims.py, never the
# browser-facing one (routes/auth.py's OIDC_BROWSER_ISSUER).
OIDC_TOKEN_ISSUER = OIDC_ISSUERS[0]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(dt: datetime) -> datetime:
    """Some drivers/dialects round-trip a tz-aware datetime as naive (sqlite
    always does; a plain, non-timezone(True) column can too) -- treat a naive
    value read back from the DB as UTC rather than letting the `>` comparison
    below raise on offset-naive vs. offset-aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _refresh_session(db: Session, row: UserSession) -> UserClaims | None:
    if not row.refresh_token:
        return None
    resp = httpx.post(
        f"{OIDC_TOKEN_ISSUER}/protocol/openid-connect/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": row.refresh_token,
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        db.delete(row)
        db.commit()
        return None
    tokens = resp.json()
    row.access_token = tokens["access_token"]
    row.refresh_token = tokens.get("refresh_token", row.refresh_token)
    row.expires_at = _utcnow() + timedelta(seconds=tokens.get("expires_in", 900))
    db.add(row)
    db.commit()
    try:
        return parse_claims("Bearer " + row.access_token)
    except jwt.PyJWTError:
        return None


def _claims_from_session(db: Session, row: UserSession) -> UserClaims | None:
    if _as_aware_utc(row.expires_at) > _utcnow():
        try:
            return parse_claims("Bearer " + row.access_token)
        except jwt.PyJWTError:
            pass
    return _refresh_session(db, row)


def get_current_user(request: Request, db: Session = Depends(get_session)) -> UserClaims:
    """Browser requests (session cookie set by /auth/login -> /auth/callback,
    ARCHITECTURE.md Section 4.4) and API/MCP callers (raw Authorization
    header) both resolve to the same UserClaims here -- the cookie is just an
    indirection to an access token, still handed to the same parse_claims()
    either way, so no enforcement logic forks between the two paths."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        row = db.get(UserSession, session_id)
        claims = _claims_from_session(db, row) if row else None
        if claims is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired, please log in again")
        return claims

    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return parse_claims(auth)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc


def get_current_access_token(request: Request, db: Session = Depends(get_session)) -> str:
    """Raw bearer token for the current request -- session cookie (refreshed
    transparently by expiry if needed) or the Authorization header -- always
    returned without a "Bearer " prefix either way. For server-side calls
    this app makes to other services on the caller's own behalf (see
    routes/search.py proxying to orchestration-mcp), where the caller's own
    token, not a re-derivation of it, is what has to be forwarded.

    Deliberately does not share _claims_from_session/_refresh_session with
    get_current_user above: a route needing both a token and validated
    claims in one request would otherwise resolve this session twice
    independently, and if Keycloak rotates refresh tokens (a common default),
    the second resolution could try to reuse one already consumed by the
    first. Keeping this separate means it only ever refreshes a session
    that's actually expired by clock, once.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        row = db.get(UserSession, session_id)
        if row is not None:
            if _as_aware_utc(row.expires_at) > _utcnow():
                return row.access_token
            if _refresh_session(db, row) is not None:
                return row.access_token
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired, please log in again")

    auth = request.headers.get("Authorization")
    if not auth:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return auth.removeprefix("Bearer ").strip()


def get_current_user_optional(
    request: Request, db: Session = Depends(get_session)
) -> UserClaims | None:
    """Same resolution as get_current_user, but returns None instead of
    raising -- for page routes (main.py) that render whether or not the
    visitor is logged in, and just want to know who (if anyone) to display
    in the nav."""
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def verify_csrf(request: Request) -> None:
    """NFR-14: required on every state-changing route. A caller presenting a
    raw Authorization header (API/MCP client, e.g. curl or orchestration-mcp)
    is never CSRF-exposed -- a cross-site page can't make the victim's
    browser attach an arbitrary header -- so this only enforces anything when
    a session cookie is present, i.e. a browser-driven request."""
    if not request.cookies.get(SESSION_COOKIE):
        return
    cookie_token = request.cookies.get(CSRF_COOKIE)
    header_token = request.headers.get(CSRF_HEADER)
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")


def require_ingest(user: UserClaims = Depends(get_current_user)) -> UserClaims:
    if not user.can_ingest:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing rag-ingest role")
    return user


def require_curator(user: UserClaims = Depends(get_current_user)) -> UserClaims:
    if not user.curatable_orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing any rag-curate:<org> role")
    return user


def require_admin(user: UserClaims = Depends(get_current_user)) -> UserClaims:
    if "rag-admin" not in user.rag_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing rag-admin role")
    return user


def allowed_classifications(session: Session, clearance: str) -> list[str]:
    """Every Classification value at or below the user's clearance rank (FR-18)."""
    return _allowed_classifications(session, clearance)


SessionDep = Depends(get_session)
