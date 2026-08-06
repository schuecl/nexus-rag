"""Issue #214: what a caller learns when something goes wrong.

Individually these were small hints. Together they mapped internal topology --
issuer and audience configuration, vector backend hostnames, collection names
-- to anyone holding a valid token or able to send a junk one.

The convention already existed in this codebase: `test_consumer_liveness.py`
asserts /health reports the exception *type* but not its message, so the
payload names the failure mode without handing out internal hostnames. These
sites predated it.
"""

from __future__ import annotations

import json
from typing import ClassVar

import jwt
import pytest
from starlette.requests import Request

from app import rag_search, server


def _request(*, body: bytes = b"", query_string: str = "", authorized: bool = True) -> Request:
    headers = [(b"content-type", b"application/json")] if body else []
    if authorized:
        headers.append((b"authorization", b"Bearer token"))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/debug/rag_search",
            "query_string": query_string.encode(),
            "headers": headers,
        },
        receive,
    )


class TestTokenRejectionSaysTypeNotMessage:
    async def test_the_pyjwt_message_is_not_returned(self, monkeypatch):
        # PyJWT's text names the expected issuer, audience, and algorithm.
        def _reject(_token):
            raise jwt.InvalidIssuerError(
                "Invalid issuer. Expected one of "
                "['http://keycloak:8080/realms/nexus-rag', 'https://internal.example.mil']"
            )

        monkeypatch.setattr(rag_search, "parse_claims", _reject)

        result = await rag_search.run_rag_search("Bearer t", "q", 5)

        assert "keycloak:8080" not in result["error"]
        assert "internal.example.mil" not in result["error"]

    async def test_the_exception_type_is_still_reported(self, monkeypatch):
        """The type is what a client needs: expired means "refresh and retry",
        anything else does not. #200 relies on that distinction."""

        def _reject(_token):
            raise jwt.ExpiredSignatureError("Signature has expired")

        monkeypatch.setattr(rag_search, "parse_claims", _reject)

        result = await rag_search.run_rag_search("Bearer t", "q", 5)

        assert "ExpiredSignatureError" in result["error"]


class TestDebugEndpointKeepsQueriesOutOfURLs:
    """#125 removed query text from the audit log because a question asked of
    a classified corpus is itself sensitive. This route then put the same text
    into every proxy and ingress log in the path."""

    async def test_the_query_is_read_from_a_json_body(self, monkeypatch):
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", True)
        captured: dict = {}

        async def _run(_auth, query, top_k, **_kw):
            captured.update(query=query, top_k=top_k)
            return {"results": []}

        monkeypatch.setattr(server, "run_rag_search", _run)

        resp = await server.debug_rag_search(
            _request(body=json.dumps({"query": "a sensitive question", "top_k": 3}).encode())
        )

        assert resp.status_code == 200
        assert captured["query"] == "a sensitive question"
        assert captured["top_k"] == 3

    async def test_the_query_string_form_still_works_but_warns(self, monkeypatch, caplog):
        """Deprecated, not removed -- the docs' curl examples and existing
        scripts use it, and silently breaking them would be worse than the
        logging exposure it warns about."""
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", True)

        async def _run(*_a, **_k):
            return {"results": []}

        monkeypatch.setattr(server, "run_rag_search", _run)

        resp = await server.debug_rag_search(_request(query_string="query=legacy+form"))

        assert resp.status_code == 200
        assert any("appear in proxy" in r.getMessage() for r in caplog.records)

    async def test_a_malformed_body_is_a_400_not_a_500(self, monkeypatch):
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", True)

        resp = await server.debug_rag_search(_request(body=b"{not json"))

        assert resp.status_code == 400

    async def test_missing_query_is_rejected(self, monkeypatch):
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", True)

        resp = await server.debug_rag_search(_request(body=b"{}"))

        assert resp.status_code == 400


class TestDebugEndpointCanBeDisabled:
    async def test_it_404s_when_turned_off(self, monkeypatch):
        """Authorization is enforced, so leaving it on is not a hole -- but it
        is surface nothing in a deployed environment needs, and it shipped in
        every production image with no way to remove it."""
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", False)

        resp = await server.debug_rag_search(_request(body=b'{"query": "x"}'))

        assert resp.status_code == 404

    def test_it_defaults_to_disabled_matching_214s_stated_intent(self):
        """#476: the code default itself must be closed -- docker-compose.yml
        and the Helm chart opt back in explicitly (both needed, since
        ingestion-api's /search page proxies to this route), but an unset env
        var (e.g. running this service directly) must land on off."""
        assert server.DEBUG_ENDPOINT_ENABLED is False


class TestBackendErrorsStayOutOfTheResponse:
    async def test_the_note_does_not_carry_the_exception_text(self, monkeypatch):
        """The note is returned to the caller and, through the MCP tool, into
        a model's context. A backend error string carries hostnames, ports,
        and collection names."""
        from common.vector_store import VectorStoreUnavailable

        class _Claims:
            sub = "u"
            preferred_username = "u"
            groups: ClassVar[list[str]] = []
            org = None
            can_query = True
            clearance = "UNCLASSIFIED"
            releasability: ClassVar[list[str]] = []

        class _Store:
            def hybrid_query(self, **_kw):
                raise VectorStoreUnavailable(
                    "connection refused to qdrant.internal.example.mil:6333 "
                    "collection nexus_rag_chunks"
                )

            def access_filter_summary(self, _c, _a):
                return {}

            def stored_embedding_model(self):
                return None

        async def _embed(_q):
            return [0.0]

        monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
        monkeypatch.setattr(rag_search, "get_session", lambda: iter([_Session()]))
        monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["UNCLASSIFIED"])
        monkeypatch.setattr(rag_search, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(rag_search, "_embed_query", _embed)
        monkeypatch.setattr(rag_search, "embed_sparse", lambda _t: [_sparse()])
        monkeypatch.setattr(rag_search, "get_store", _Store)

        result = await rag_search.run_rag_search("Bearer t", "q", 5)

        note = result.get("note", "")
        assert "internal.example.mil" not in note
        assert "6333" not in note
        # Still says the useful thing: this is expected before first ingestion.
        assert "not queryable" in note


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _sparse():
    from qdrant_client.models import SparseVector

    return SparseVector(indices=[0], values=[1.0])


@pytest.fixture(autouse=True)
def _caplog_at_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING)
