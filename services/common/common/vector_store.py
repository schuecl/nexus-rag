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
    def ensure_ready(self, dense_size: int) -> None:
        """Create the collection/schema if it doesn't exist (idempotent)."""

    def stored_embedding_model(self) -> str | None:
        """#122 provenance: the embedding model stamped on stored chunks, or
        None when the collection is absent/empty/pre-stamp."""

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
        Raises VectorStoreUnavailable when the engine/collection can't be
        queried."""

    def access_filter_summary(self, claims: UserClaims, allowed_classifications: list[str]) -> dict:
        """The applied-filter description rag_search returns to callers and
        writes to the FR-31 audit detail."""

    def update_document_payload(self, document_id: str, fields: dict) -> None:
        """FR-13: propagate curation decisions (status, corrected tags) to
        every chunk of a document."""

    def delete_document_chunks(self, document_id: str) -> None:
        """FR-7 supersede / #123 purge: remove every chunk of a document."""


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
