"""Coverage for issues #125 and #127: what a retrieval attempt leaves behind,
and what it hands back.

Both are confidentiality properties rather than authorization ones -- FR-26
still decides *what may be retrieved*, and nothing here changes that. These
tests pin what the system records about a query (#125) and what it discloses
about the corpus beyond the results themselves (#127).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from qdrant_client.models import SparseVector

from app import rag_search


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    clearance = "SECRET"
    groups = ("analysts",)
    org = "USAREUR-AF"
    can_query = True
    releasability = ("FVEY",)


SENSITIVE = "what is the deployment schedule for operation blueprint"


def _stub(monkeypatch, *, hits, audits: list) -> None:
    @contextmanager
    def _session():
        yield object()

    class _Store:  # #160: rag_search goes through the vector-store seam now
        def hybrid_query(self, **_kwargs):
            return hits

        def access_filter_summary(self, _claims, _allowed):
            return {"backend": "fake"}

        def stored_embedding_model(self):
            return None

    async def _embed(_q):
        return [0.0]

    monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
    monkeypatch.setattr(rag_search, "get_session", lambda: iter([_session()]))
    monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["SECRET"])
    monkeypatch.setattr(rag_search, "_embed_query", _embed)
    monkeypatch.setattr(
        rag_search, "embed_sparse", lambda _t: [SparseVector(indices=[0], values=[1.0])]
    )
    monkeypatch.setattr(rag_search, "get_store", _Store)
    monkeypatch.setattr(
        rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
    )


class _Hit:
    def __init__(self, id_, score, payload):
        self.id = id_
        self.score = score
        self.payload = payload


def _hit(id_="c1", score=0.87):
    return _Hit(id_, score, {"document_id": "d1", "text": "body", "content_type": "text"})


class TestAuditOmitsQueryText:
    """#125: audit_log lives in Postgres and NFR-2's hardening makes it
    append-only without restricting reads, so anything written there is
    readable by any holder of APP_DB_USER -- outside the boundary
    rag-admin/ownership scoping enforces everywhere else."""

    async def test_denied_query_is_audited_without_its_text(self, monkeypatch):
        # The sharpest case: a denial records what someone *tried* to reach.
        class _NoQuery(_Claims):
            can_query = False

        audits: list = []
        monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _NoQuery())
        monkeypatch.setattr(
            rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
        )

        await rag_search.run_rag_search("Bearer t", SENSITIVE)

        assert len(audits) == 1
        action, detail = audits[0]
        assert action == "query.denied"
        assert SENSITIVE not in str(detail)
        assert detail["reason"] == "missing rag-query role"

    async def test_successful_query_is_audited_without_its_text(self, monkeypatch):
        audits: list = []
        _stub(monkeypatch, hits=[_hit()], audits=audits)

        async def _rerank(_q, candidates, top_k, **_kw):
            return candidates[:top_k], "stubbed"

        monkeypatch.setattr(rag_search, "rerank", _rerank)

        await rag_search.run_rag_search("Bearer t", SENSITIVE)

        action, detail = audits[0]
        assert action == "query"
        assert SENSITIVE not in str(detail)

    async def test_accountability_fields_survive(self, monkeypatch):
        """Dropping the text must not gut FR-31: the entry still has to prove
        the filter applied, to whom, and what it permitted."""
        audits: list = []
        _stub(monkeypatch, hits=[_hit()], audits=audits)

        async def _rerank(_q, candidates, top_k, **_kw):
            return candidates[:top_k], "stubbed"

        monkeypatch.setattr(rag_search, "rerank", _rerank)

        await rag_search.run_rag_search("Bearer t", SENSITIVE, 5)

        _, detail = audits[0]
        assert detail["applied_filter"], "the filter must stay auditable (FR-26 evidence)"
        assert detail["result_count"] == 1
        assert detail["result_document_ids"] == ["d1"]
        assert detail["top_k"] == 5

    async def test_query_length_is_retained_for_anomaly_detection(self, monkeypatch):
        audits: list = []
        _stub(monkeypatch, hits=[], audits=audits)

        await rag_search.run_rag_search("Bearer t", SENSITIVE)

        assert audits[0][1]["query_chars"] == len(SENSITIVE)

    @pytest.mark.parametrize("detail", [{"a": 1}, {}])
    def test_helper_never_emits_the_text(self, detail):
        out = rag_search._audit_query_detail(SENSITIVE, **detail)

        assert SENSITIVE not in str(out)
        assert out["query_chars"] == len(SENSITIVE)


class TestResponseOmitsSimilarityScores:
    """#127: OWASP's RAG guidance is explicit that similarity scores must not
    be returned -- the score gradient is what document-level membership
    inference reads."""

    async def test_results_carry_no_score(self, monkeypatch):
        audits: list = []
        _stub(monkeypatch, hits=[_hit("c1", 0.91), _hit("c2", 0.42)], audits=audits)

        async def _rerank(_q, candidates, top_k, **_kw):
            return candidates[:top_k], "stubbed"

        monkeypatch.setattr(rag_search, "rerank", _rerank)

        result = await rag_search.run_rag_search("Bearer t", SENSITIVE)

        assert result["results"], "sanity: the stub returned hits"
        for r in result["results"]:
            assert "score" not in r, f"similarity score leaked: {r}"
        # And not smuggled inside the payload either.
        assert all("score" not in r["payload"] for r in result["results"])

    async def test_rank_order_is_preserved_without_scores(self, monkeypatch):
        """Dropping the score must not drop the ranking -- list order is what
        the calling model actually consumes."""
        audits: list = []
        _stub(monkeypatch, hits=[_hit("c1", 0.91), _hit("c2", 0.42)], audits=audits)

        async def _rerank(_q, candidates, top_k, **_kw):
            return list(reversed(candidates))[:top_k], "stubbed"

        monkeypatch.setattr(rag_search, "rerank", _rerank)

        result = await rag_search.run_rag_search("Bearer t", SENSITIVE)

        assert [r["id"] for r in result["results"]] == ["c2", "c1"]

    async def test_payload_and_citation_fields_survive(self, monkeypatch):
        """FR-27 citation data must not be collateral damage."""
        audits: list = []
        _stub(monkeypatch, hits=[_hit()], audits=audits)

        async def _rerank(_q, candidates, top_k, **_kw):
            return candidates[:top_k], "stubbed"

        monkeypatch.setattr(rag_search, "rerank", _rerank)

        result = await rag_search.run_rag_search("Bearer t", SENSITIVE)

        payload = result["results"][0]["payload"]
        assert payload["document_id"] == "d1"
        assert rag_search._UNTRUSTED_CONTENT_MARKER in payload["text"]
