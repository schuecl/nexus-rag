"""Coverage for issue #106: top_k reaches retrieval bounded on every entry
point. It drives the retrieval fan-out (rag_search.hybrid_limit) and, through
it, how many chunks reranker-service cross-encodes in a single call, so an
unbounded value is an availability lever rather than just an odd request.

The access filter is applied before any of this and is unaffected either way
-- these tests are about fan-out and input handling, not FR-26.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar

import pytest
from qdrant_client.models import SparseVector
from starlette.requests import Request

from app import rag_search, server


class _StopHere(Exception):
    """Sentinel: unwinds run_rag_search once the value under test has been
    observed, so these tests never need Qdrant, Ollama, or a database.
    Deliberately not an UnexpectedResponse/httpx.HTTPError, both of which
    run_rag_search catches and turns into an empty-result response."""


def _request(query_string: str, *, authorized: bool = True) -> Request:
    """A Starlette Request carrying just a query string -- enough for
    debug_rag_search, which reads only query_params and the auth header."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/debug/rag_search",
            "query_string": query_string.encode(),
            "headers": [(b"authorization", b"Bearer token")] if authorized else [],
        }
    )


class TestDebugEndpointValidation:
    """POST /debug/rag_search parses top_k out of the raw query string, so it
    has to do its own validation -- the MCP tool's schema doesn't cover it."""

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(server, "DEBUG_ENDPOINT_ENABLED", True)

    async def test_non_integer_top_k_is_a_400_not_a_500(self):
        # Previously `int(request.query_params.get("top_k", 5))` raised
        # ValueError straight out of the route.
        resp = await server.debug_rag_search(_request("query=hello&top_k=abc"))

        assert resp.status_code == 400
        assert b"integer" in resp.body

    async def test_top_k_above_the_ceiling_is_rejected(self):
        resp = await server.debug_rag_search(
            _request(f"query=hello&top_k={rag_search.MAX_TOP_K + 1}")
        )

        assert resp.status_code == 400
        assert str(rag_search.MAX_TOP_K).encode() in resp.body

    @pytest.mark.parametrize("value", ["0", "-1"])
    async def test_top_k_below_one_is_rejected(self, value):
        resp = await server.debug_rag_search(_request(f"query=hello&top_k={value}"))

        assert resp.status_code == 400

    async def test_missing_authorization_takes_precedence_over_a_bad_top_k(self):
        resp = await server.debug_rag_search(_request("query=hello&top_k=abc", authorized=False))

        # The auth check runs first: an unauthenticated caller learns nothing
        # about which other parameters would also have been rejected.
        assert resp.status_code == 401


def _stub_retrieval(monkeypatch, captured: dict) -> None:
    """Stub everything run_rag_search touches up to the Qdrant call, then
    capture the fan-out limit it computed and unwind."""

    class _Claims:
        sub = "u"
        preferred_username = "u"
        clearance = "UNCLASSIFIED"
        groups: ClassVar[list[str]] = []
        org = None
        can_query = True
        releasability: ClassVar[list[str]] = []

    @contextmanager
    def _session():
        yield object()

    class _Store:  # #160: the seam receives one limit for both legs
        def hybrid_query(self, **kwargs):
            captured["limit"] = kwargs["limit"]
            captured["prefetch_limits"] = [kwargs["limit"], kwargs["limit"]]
            raise _StopHere

        def access_filter_summary(self, _claims, _allowed):
            return {}

        def stored_embedding_model(self):
            return None

    async def _embed(_query):
        return [0.0]

    monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
    monkeypatch.setattr(rag_search, "get_session", lambda: iter([_session()]))
    monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["UNCLASSIFIED"])
    monkeypatch.setattr(rag_search, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(rag_search, "_embed_query", _embed)
    # A real SparseVector, not a placeholder: Prefetch validates its `query`
    # field at construction, which happens before query_points is ever called.
    monkeypatch.setattr(
        rag_search, "embed_sparse", lambda _texts: [SparseVector(indices=[0], values=[1.0])]
    )
    monkeypatch.setattr(rag_search, "get_store", _Store)


class TestRunRagSearchClamp:
    """Defence in depth inside run_rag_search itself, so a transport added
    later can't reintroduce an unbounded fan-out by skipping its own check.
    Neither current caller can reach these branches."""

    @pytest.mark.parametrize("requested", [rag_search.MAX_TOP_K + 1, 10_000])
    async def test_oversized_top_k_is_clamped_before_sizing_the_fan_out(
        self, monkeypatch, requested
    ):
        # hybrid_limit = max(top_k * 4, 20) is what actually sizes both Qdrant
        # prefetch legs, so the clamp is only useful if it lands first.
        captured: dict = {}
        _stub_retrieval(monkeypatch, captured)

        with pytest.raises(_StopHere):
            await rag_search.run_rag_search("Bearer t", "q", requested)

        expected = rag_search.MAX_TOP_K * rag_search.HYBRID_CANDIDATE_MULTIPLIER
        assert captured["limit"] == expected
        # Both legs, not just the outer fusion query -- FR-26 aside, an
        # unbounded prefetch is the expensive half.
        assert captured["prefetch_limits"] == [expected, expected]

    @pytest.mark.parametrize("requested", [0, -5])
    async def test_non_positive_top_k_is_clamped_up_to_one(self, monkeypatch, requested):
        captured: dict = {}
        _stub_retrieval(monkeypatch, captured)

        with pytest.raises(_StopHere):
            await rag_search.run_rag_search("Bearer t", "q", requested)

        # MIN_HYBRID_CANDIDATES still floors the candidate pool, so this
        # asserts the clamp didn't leave a zero/negative limit reaching Qdrant.
        assert captured["limit"] == rag_search.MIN_HYBRID_CANDIDATES

    async def test_in_range_top_k_is_left_alone(self, monkeypatch):
        captured: dict = {}
        _stub_retrieval(monkeypatch, captured)

        with pytest.raises(_StopHere):
            await rag_search.run_rag_search("Bearer t", "q", 12)

        assert captured["limit"] == 12 * rag_search.HYBRID_CANDIDATE_MULTIPLIER
