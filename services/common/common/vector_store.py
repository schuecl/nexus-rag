"""#160: the vector-store seam -- Qdrant by default, Milvus as an opt-in
alternative, strictly either/or.

One deployment runs ONE backend, selected by VECTOR_BACKEND:

    VECTOR_BACKEND=qdrant   (default, and the absence of the variable) --
                            exactly the pre-#160 behavior: every method here
                            delegates to the existing common/qdrant_store.py
                            and common/qdrant_filters.py code paths unchanged.
    VECTOR_BACKEND=milvus   the same pipeline against Milvus (see
                            common/milvus_store.py). Opt-in per deployment;
                            never mixed with Qdrant, no dual-writes, no
                            cross-backend reads.

An unrecognized value raises at startup rather than silently falling back:
a typo'd backend name is an operator error about *which engine holds the
corpus*, and guessing would hide it.

The seam exists to make an A/B comparison honest (#160's whole point): both
backends sit behind the same six operations, under the same #134 span names
and #72 stage timers, enforcing the same FR-26 filter semantics -- so a
quality or latency delta measured by the #71 golden-query harness is
attributable to the engine, not to divergent plumbing.

FR-26 note: the mandatory access filter is applied *inside* hybrid_query by
each backend, built server-side from verified claims -- call sites cannot
forget it and clients cannot supply it, same as before this seam existed.

Issue #229: QdrantStore additionally splits the corpus into one collection
per Classification level (see common/qdrant_store.py's module docstring for
why) and fans hybrid_query out across every collection an allowed set of
classifications resolves to, fusing the per-collection results by rank with
`fuse_ranked` below -- per-collection scores aren't comparable once BM25's
IDF is relative to a smaller, classification-skewed corpus. `ensure_ready`,
`update_document_payload`, and `delete_document_chunks` take a `classification`
argument for the same reason: it selects which collection the operation
applies to. MilvusStore does not implement the split (see its module
docstring) -- it accepts the same parameter and ignores it, an explicit,
recorded "not yet" rather than a silent gap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from qdrant_client.models import SparseVector

    from common.claims import UserClaims


@dataclass(frozen=True)
class ChunkPoint:
    """A backend-neutral chunk record. `sparse` stays in qdrant-client's
    SparseVector shape (indices/values) because common/sparse_embedding.py
    produces it; MilvusStore converts. Payload carries everything else,
    including the FR-26 fields (status/classification/releasability/
    access_scope/document_id) each backend promotes into filterable form."""

    id: str
    dense: list[float]
    sparse: SparseVector
    payload: dict


@dataclass(frozen=True)
class Hit:
    """One hybrid-query result, shaped like the fields rag_search consumes."""

    id: str
    score: float
    payload: dict = field(default_factory=dict)


class VectorStoreUnavailable(Exception):
    """Backend-neutral 'engine not reachable / collection not queryable'.
    rag_search catches this (instead of qdrant-specific exceptions) to keep
    its degraded-mode path identical across backends."""


class VectorStore(Protocol):
    def ensure_ready(self, dense_size: int, classification: str) -> None:
        """Create the collection/schema for `classification` if it doesn't
        exist (idempotent). Issue #229: one collection per classification
        level for QdrantStore; MilvusStore ignores the argument (see its
        module docstring)."""

    def stored_embedding_model(self) -> str | None:
        """#122 provenance: the embedding model stamped on stored chunks, or
        None when nothing exists yet/is empty/predates the stamp. Issue #229:
        for QdrantStore this is checked across every classification
        collection, not just one -- see
        qdrant_store.any_collection_embedding_model."""

    def upsert(self, points: list[ChunkPoint]) -> None: ...

    def hybrid_query(
        self,
        *,
        dense: list[float],
        sparse: SparseVector,
        claims: UserClaims,
        allowed_classifications: list[str],
        limit: int,
    ) -> list[Hit]:
        """Dense + sparse legs fused with RRF, with the mandatory FR-26
        filter (built here, server-side, from `claims`) applied to BOTH legs.
        Issue #229: for QdrantStore, `allowed_classifications` also selects
        *which collections* are queried -- one per classification the caller
        is cleared for -- and per-collection results are fused again by rank
        (`fuse_ranked`) since scores aren't comparable across collections.
        Raises VectorStoreUnavailable when the engine/collection can't be
        queried."""

    def access_filter_summary(self, claims: UserClaims, allowed_classifications: list[str]) -> dict:
        """The applied-filter description rag_search returns to callers and
        writes to the FR-31 audit detail."""

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        """FR-13: propagate curation decisions (status, corrected tags) to
        every chunk of a document. `classification` is the value the chunks
        are *currently* stamped with (issue #229: which collection they're
        currently in) -- if `fields` corrects `classification` to something
        else, QdrantStore moves the chunks to the target collection rather
        than writing in place."""

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        """FR-7 supersede / #123 purge: remove every chunk of a document.
        `classification` selects which collection to delete from (issue
        #229)."""


# Reciprocal Rank Fusion constant, matching the informal literature default
# (and Qdrant's own FusionQuery(Fusion.RRF), which uses the same value) --
# not tuned for this corpus, kept consistent with the per-collection fusion
# each backend already does so the cross-collection pass behaves the same way.
DEFAULT_RRF_K = 60


def fuse_ranked(result_lists: list[list[Hit]], *, limit: int, k: int = DEFAULT_RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion over already-ranked hit lists from separate
    collections or queries.

    Issue #229: once retrieval fans out over one collection per
    classification level, per-collection similarity scores are no longer
    comparable -- BM25's IDF is computed server-side from each collection's
    own (now classification-skewed) document statistics, so the same query
    against the same text yields a different score depending on which
    collection it landed in. Combining results across collections therefore
    has to be rank-based, not score-based. RRF is what the pipeline already
    uses to fuse the dense and sparse legs *within* one collection
    (Qdrant/Milvus-native FusionQuery/RRFRanker); this is the same primitive
    applied a second time, client-side, across collections.

    A chunk belongs to exactly one classification, so the same id can appear
    in at most one input list -- this never needs to merge scores for one id
    across lists, only combine and re-sort what's already there.
    """
    scores: dict[str, float] = {}
    hits_by_id: dict[str, Hit] = {}
    for hits in result_lists:
        for rank, hit in enumerate(hits, start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank)
            hits_by_id.setdefault(hit.id, hit)
    ordered = sorted(hits_by_id.values(), key=lambda h: scores[h.id], reverse=True)
    return [Hit(id=h.id, score=scores[h.id], payload=h.payload) for h in ordered[:limit]]


def backend_name() -> str:
    """The resolved backend name, for #134 span attributes and log lines --
    spans keep one name (vector.query / vector.upsert) across backends with
    the backend as an attribute, so a Tempo A/B compares like with like."""
    return os.environ.get("VECTOR_BACKEND", "qdrant").strip().lower() or "qdrant"


@lru_cache(maxsize=1)
def get_store() -> VectorStore:
    backend = backend_name()
    if backend == "qdrant":
        from common.qdrant_backend import QdrantStore

        return QdrantStore()
    if backend == "milvus":
        from common.milvus_store import MilvusStore

        return MilvusStore()
    raise ValueError(
        f"VECTOR_BACKEND={backend!r} is not 'qdrant' or 'milvus' -- refusing to "
        "guess which engine holds the corpus"
    )
