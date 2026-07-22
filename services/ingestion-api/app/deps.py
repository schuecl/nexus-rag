from __future__ import annotations

import jwt
from common.claims import UserClaims, parse_claims
from common.classification import allowed_classifications as _allowed_classifications
from common.db import get_session
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session


def get_current_user(request: Request) -> UserClaims:
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
