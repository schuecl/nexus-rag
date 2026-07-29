"""Issue #216: /rerank otherwise has no authorization model of its own --
reachability on the network is authorization. These cover the shared-secret
dependency directly rather than through TestClient, since going through the
app would trigger the lifespan's real CrossEncoder load."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import main


def test_noop_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "")

    main._check_shared_secret(x_reranker_shared_secret=None)
    main._check_shared_secret(x_reranker_shared_secret="anything")


def test_accepts_matching_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    main._check_shared_secret(x_reranker_shared_secret="s3cr3t")


def test_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException) as exc_info:
        main._check_shared_secret(x_reranker_shared_secret=None)
    assert exc_info.value.status_code == 401


def test_rejects_wrong_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException) as exc_info:
        main._check_shared_secret(x_reranker_shared_secret="wrong")
    assert exc_info.value.status_code == 401


def test_rejects_empty_header_even_if_secret_is_falsy_like(monkeypatch):
    # Guards against a future refactor reintroducing `if not secret: return`
    # ambiguity between "unconfigured" and "configured as an empty string".
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException):
        main._check_shared_secret(x_reranker_shared_secret="")
