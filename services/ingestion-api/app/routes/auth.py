"""Browser OIDC Authorization Code + PKCE login (ARCHITECTURE.md Section
4.4), replacing the old paste-a-token dev workaround. Bearer-token API/MCP
callers are untouched -- see deps.get_current_user, which checks this flow's
session cookie first and falls back to the Authorization header either way.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from app.deps import (
    CSRF_COOKIE,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_TOKEN_ISSUER,
    SESSION_COOKIE,
    SESSION_LIFETIME,
    _as_aware_utc,
)
from common.claims import OIDC_ISSUERS
from common.db import get_session
from common.models import OAuthState, UserSession
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, delete

router = APIRouter(prefix="/auth", tags=["auth"])

# The browser has to land somewhere it can actually reach itself, which for
# the dev Compose stack's two-hostnames-one-Keycloak setup (claims.py) is the
# last, host-external OIDC_ISSUERS entry -- never OIDC_ISSUERS[0]
# (container-internal, used only for server-to-server calls like the token
# exchange below).
OIDC_BROWSER_ISSUER = OIDC_ISSUERS[-1]
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "http://localhost:8001/auth/callback")
# Dev Compose serves the ingestion UI over plain http://localhost, where a
# Secure cookie would never be sent back -- set COOKIE_SECURE=false there.
# Always true in any real (https) deployment.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"

STATE_COOKIE = "nexus_rag_oauth_state"

# Issue #108: how long a `state` row stays redeemable. Matches STATE_COOKIE's
# max_age below -- past that the browser no longer sends the cookie the state
# has to match, so a row older than this could never be redeemed anyway; it
# was simply sitting in the table forever. /callback's "unknown or expired
# OAuth state" message described this behaviour before anything implemented
# it.
OAUTH_STATE_TTL = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _purge_expired(db: Session) -> None:
    """Reap rows that can no longer be used, on the one unauthenticated write
    path that creates them.

    /auth/login is the only endpoint in this service that writes to the
    database without a caller identity, and it inserted an oauth_states row
    per call with nothing ever deleting the abandoned ones -- so a trivial
    loop against it grew the table without bound, and because every service
    shares one Postgres instance that degrades Keycloak and LiteLLM too, not
    just ingestion.

    Purging here rather than on a timer means the requests driving the growth
    are the same ones paying to clean it up. It bounds the table at roughly
    (login rate x TTL) rather than eliminating the flood outright -- a real
    rate limit in front of /auth/login is a separate concern this doesn't
    pretend to solve. user_sessions is swept on the same trigger since new
    sessions are created here too.

    This is housekeeping, never an enforcement point. The comparison below
    happens in SQL, so _as_aware_utc can't normalise it and the exact
    boundary depends on how the dialect compares a naive column against an
    aware parameter. That's tolerable precisely because nothing trusts it:
    whether a state or session is still usable is decided in Python on every
    use (callback() below, and deps.session_expired), so a sweep that runs a
    few hours late deletes a row that already stopped working.
    """
    now = _utcnow()
    db.exec(delete(OAuthState).where(OAuthState.created_at < now - OAUTH_STATE_TTL))
    db.exec(delete(UserSession).where(UserSession.created_at < now - SESSION_LIFETIME))
    db.commit()


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/login")
def login(db: Session = Depends(get_session)) -> RedirectResponse:
    _purge_expired(db)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    db.add(OAuthState(id=state, code_verifier=verifier))
    db.commit()

    params = {
        "client_id": OIDC_CLIENT_ID,
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": OIDC_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    resp = RedirectResponse(f"{OIDC_BROWSER_ISSUER}/protocol/openid-connect/auth?{urlencode(params)}")
    # Belt-and-suspenders CSRF binding: the state round-tripped through
    # Keycloak already has to match this cookie at /callback, so a forged
    # callback request (attacker knows/guesses a state) still needs the
    # victim's browser to have actually initiated that exact login.
    resp.set_cookie(STATE_COOKIE, state, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=600)
    return resp


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_session),
) -> RedirectResponse:
    cookie_state = request.cookies.get(STATE_COOKIE)
    if not code or not state or not cookie_state or state != cookie_state:
        raise HTTPException(400, "invalid or missing OAuth state")
    row = db.get(OAuthState, state)
    if row is None:
        raise HTTPException(400, "unknown or expired OAuth state")
    # Consume the row before deciding whether it was still valid: an expired
    # state is spent either way, so a caller can't retry against it.
    expired = _as_aware_utc(row.created_at) + OAUTH_STATE_TTL <= _utcnow()
    code_verifier = row.code_verifier
    db.delete(row)
    db.commit()
    if expired:
        raise HTTPException(400, "unknown or expired OAuth state")

    token_resp = httpx.post(
        f"{OIDC_TOKEN_ISSUER}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OIDC_REDIRECT_URI,
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            # The local, not row.code_verifier: the row is deleted and the
            # transaction committed above, so the instance is expired and
            # touching it here would re-query (or fail) for no reason.
            "code_verifier": code_verifier,
        },
        timeout=10,
    )
    if token_resp.status_code != 200:
        raise HTTPException(502, f"token exchange failed: {token_resp.text}")
    tokens = token_resp.json()

    session_id = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            id=session_id,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            id_token=tokens.get("id_token"),
            expires_at=_utcnow() + timedelta(seconds=tokens.get("expires_in", 900)),
        )
    )
    db.commit()

    resp = RedirectResponse("/")
    resp.delete_cookie(STATE_COOKIE)
    resp.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    # NFR-14: double-submit CSRF token, deliberately NOT httponly -- base.html's
    # JS has to read it and echo it back as a header on state-changing requests
    # (deps.verify_csrf checks cookie == header). Not itself a secret the way
    # the session cookie is; its only job is being unreadable to a cross-site
    # attacker who can make the browser *send* cookies but can't read them.
    resp.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    return resp


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_session)) -> RedirectResponse:
    """Clears this app's session *and* redirects through Keycloak's
    RP-initiated logout (end_session_endpoint) so the browser's Keycloak SSO
    session ends too -- otherwise logging back in wouldn't re-prompt for
    credentials. Uses `id_token_hint` (not just `client_id`, which newer
    Keycloak versions reject) -- requires the id_token captured at /callback,
    so a session predating that change, or one that's already gone, just
    falls back to a local-only redirect to "/".
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    id_token = None
    if session_id:
        row = db.get(UserSession, session_id)
        if row is not None:
            id_token = row.id_token
            db.delete(row)
            db.commit()

    if id_token:
        post_logout_redirect_uri = OIDC_REDIRECT_URI.rsplit("/auth/callback", 1)[0] or "/"
        params = {"id_token_hint": id_token, "post_logout_redirect_uri": post_logout_redirect_uri}
        target = f"{OIDC_BROWSER_ISSUER}/protocol/openid-connect/logout?{urlencode(params)}"
    else:
        target = "/"

    resp = RedirectResponse(target)
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(CSRF_COOKIE)
    return resp
