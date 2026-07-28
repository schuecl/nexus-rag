"""Issue #208: the query string is bounded on every entry point.

top_k was bounded by #106 on both the MCP tool and the debug route. The query
itself was not, and it drives the same fan-out: an embedding call, a sparse
encode, and one (query, chunk) pair per candidate through a cross-encoder that
is CPU-bound, synchronous, and shared by every concurrent caller.

Bounding the count of chunks without bounding the size of each pair left that
half open, so these mirror test_top_k_bounds.py deliberately.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar

import pytest
from qdrant_client.models import SparseVector
from starlette.requests import Request

from app import rag_search, server


def _request(query_string: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/debug/rag_search",
            "query_string": query_string.encode(),
            "headers": [(b"authorization", b"Bearer token")],
        }
    )


class TestDebugEndpointBound:
    async def test_oversized_query_is_a_400(self):
        oversized = "a" * (rag_search.MAX_QUERY_CHARS + 1)

        resp = await server.debug_rag_search(_request(f"query={oversized}"))

        assert resp.status_code == 400
        assert str(rag_search.MAX_QUERY_CHARS).encode() in resp.body

    async def test_a_query_at_the_limit_is_accepted(self, monkeypatch):
        """Off-by-one guard: the bound is inclusive, so a query of exactly
        MAX_QUERY_CHARS must not be rejected."""
        captured: dict = {}

        async def _run(_auth, query, _top_k):
            captured["chars"] = len(query)
            return {"results": []}

        monkeypatch.setattr(server, "run_rag_search", _run)

        resp = await server.debug_rag_search(_request(f"query={'a' * rag_search.MAX_QUERY_CHARS}"))

        assert resp.status_code == 200
        assert captured["chars"] == rag_search.MAX_QUERY_CHARS


class TestRunRagSearchBound:
    """Defence in depth inside run_rag_search, so a transport added later
    can't reintroduce an unbounded query by skipping its own check."""

    async def test_oversized_query_is_rejected_before_any_backend_work(self, monkeypatch):
        called = []

        def _claims(_token):
            called.append("parse_claims")
            raise AssertionError("must not reach claim parsing")

        monkeypatch.setattr(rag_search, "parse_claims", _claims)

        result = await rag_search.run_rag_search(
            "Bearer t", "a" * (rag_search.MAX_QUERY_CHARS + 1), 5
        )

        assert "error" in result
        assert str(rag_search.MAX_QUERY_CHARS) in result["error"]
        # Rejected before the JWKS work the oversized request was trying to
        # make us do -- the point is to spend nothing on it.
        assert called == []

    async def test_a_normal_query_still_reaches_retrieval(self, monkeypatch):
        class _Claims:
            sub = "u"
            preferred_username = "u"
            clearance = "UNCLASSIFIED"
            groups: ClassVar[list[str]] = []
            org = None
            can_query = True
            releasability: ClassVar[list[str]] = []

        class _Stop(Exception):
            pass

        @contextmanager
        def _session():
            yield object()

        class _Store:
            def hybrid_query(self, **_kwargs):
                raise _Stop

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
        monkeypatch.setattr(
            rag_search, "embed_sparse", lambda _texts: [SparseVector(indices=[0], values=[1.0])]
        )
        monkeypatch.setattr(rag_search, "get_store", _Store)

        with pytest.raises(_Stop):
            await rag_search.run_rag_search("Bearer t", "how often are passwords rotated", 5)


class TestTheBoundIsUsable:
    def test_the_limit_leaves_room_for_real_questions(self):
        """A bound tight enough to reject legitimate use would get raised
        until it stopped protecting anything. The golden queries are the
        reference for what real looks like."""
        assert rag_search.MAX_QUERY_CHARS >= 1000
