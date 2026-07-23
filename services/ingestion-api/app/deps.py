from __future__ import annotations

import os
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
