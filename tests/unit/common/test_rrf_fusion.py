"""Issue #229: coverage for common.vector_store.fuse_ranked, the client-side
Reciprocal Rank Fusion pass QdrantStore.hybrid_query applies across the
per-classification collections a query fans out over.

Per-collection scores aren't comparable once each classification collection
computes its own BM25 IDF -- see qdrant_backend.py's module docstring -- so
this has to combine by rank, not score. These tests pin down the arithmetic
and the two properties retrieval quality actually depends on: a single input
list is a no-op re-sort (the common one-classification case must behave
exactly like the pre-#229 single-collection fusion), and combining several
lists never drops or duplicates a hit.
"""

from __future__ import annotations

from common.vector_store import Hit, fuse_ranked


def _hit(id_: str, score: float = 0.0) -> Hit:
    return Hit(id=id_, score=score, payload={"document_id": id_})


class TestSingleListIsANoOpReorder:
    def test_order_is_preserved(self):
        hits = [_hit("a"), _hit("b"), _hit("c")]

        fused = fuse_ranked([hits], limit=10)

        assert [h.id for h in fused] == ["a", "b", "c"]

    def test_truncates_to_limit(self):
        hits = [_hit("a"), _hit("b"), _hit("c")]

        fused = fuse_ranked([hits], limit=2)

        assert [h.id for h in fused] == ["a", "b"]


class TestCrossListFusion:
    def test_top_rank_in_every_list_wins(self):
        list_a = [_hit("x"), _hit("y")]
        list_b = [_hit("x"), _hit("z")]

        fused = fuse_ranked([list_a, list_b], limit=10)

        assert fused[0].id == "x"

    def test_every_hit_from_every_list_survives(self):
        list_a = [_hit("a1"), _hit("a2")]
        list_b = [_hit("b1"), _hit("b2"), _hit("b3")]

        fused = fuse_ranked([list_a, list_b], limit=10)

        assert {h.id for h in fused} == {"a1", "a2", "b1", "b2", "b3"}

    def test_no_hit_is_duplicated_across_lists(self):
        # A chunk belongs to exactly one classification collection, so the
        # same id never legitimately appears twice -- but the function must
        # still not double-count it if it somehow did.
        list_a = [_hit("shared"), _hit("only_a")]
        list_b = [_hit("shared"), _hit("only_b")]

        fused = fuse_ranked([list_a, list_b], limit=10)

        assert [h.id for h in fused].count("shared") == 1

    def test_a_hit_ranked_first_everywhere_beats_one_ranked_first_once(self):
        # Reciprocal rank fusion property: consistent middling relevance across
        # several sources should be able to outrank a single first place.
        list_a = [_hit("consistent"), _hit("solo")]
        list_b = [_hit("consistent")]
        list_c = [_hit("consistent")]

        fused = fuse_ranked([list_a, list_b, list_c], limit=10)

        assert fused[0].id == "consistent"

    def test_empty_lists_are_ignored(self):
        fused = fuse_ranked([[], [_hit("only")], []], limit=10)

        assert [h.id for h in fused] == ["only"]

    def test_no_lists_returns_no_hits(self):
        assert fuse_ranked([], limit=10) == []

    def test_scores_are_strictly_descending(self):
        list_a = [_hit("a"), _hit("b"), _hit("c")]
        list_b = [_hit("b"), _hit("c"), _hit("a")]

        fused = fuse_ranked([list_a, list_b], limit=10)

        scores = [h.score for h in fused]
        assert scores == sorted(scores, reverse=True)
