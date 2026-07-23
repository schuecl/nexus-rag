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
from app.deps import CSRF_COOKIE, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, OIDC_TOKEN_ISSUER, SESSION_COOKIE
from common.claims import OIDC_ISSUERS
from common.db import get_session
from common.models import OAuthState, UserSession
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

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
SESSION_LIFETIME = timedelta(hours=8)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/login")
def login(db: Session = Depends(get_session)) -> RedirectResponse:
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
    db.delete(row)
    db.commit()

    token_resp = httpx.post(
        f"{OIDC_TOKEN_ISSUER}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OIDC_REDIRECT_URI,
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            "code_verifier": row.code_verifier,
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
