"""#160: the default VectorStore backend -- a thin adapter over the existing
common/qdrant_store.py and common/qdrant_filters.py.

Issue #229: `hybrid_query` fans out over one collection per classification the
caller is allowed to see, since the corpus is now split that way (see
qdrant_store's module docstring for the full rationale). Each collection is
still queried with the same dense+sparse Prefetch/FusionQuery(RRF) as before
the split -- that part is untouched -- but the per-collection results then go
through a second, client-side RRF pass (`common.vector_store.fuse_ranked`)
because scores from different collections aren't comparable. With exactly one
allowed classification (a common case: most users are cleared for one level),
this reduces to fusing a single already-fused list, which is a no-op re-sort,
so behavior for that case is unchanged from before the split.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Fusion, FusionQuery, PointStruct, Prefetch

from common.qdrant_filters import build_access_filter
from common.qdrant_store import (
    DENSE_VECTOR,
    SPARSE_VECTOR,
    any_collection_embedding_model,
    chunk_vector,
    classification_collection_name,
    delete_document_chunks,
    ensure_collection,
    get_qdrant_client,
    update_document_payload,
    upsert_chunks,
)
from common.vector_store import Hit, VectorStoreUnavailable, fuse_ranked

if TYPE_CHECKING:
    from qdrant_client.models import SparseVector

    from common.claims import UserClaims
    from common.vector_store import ChunkPoint


class QdrantStore:
    def ensure_ready(self, dense_size: int, classification: str) -> None:
        ensure_collection(get_qdrant_client(), dense_size=dense_size, classification=classification)

    def stored_embedding_model(self) -> str | None:
        return any_collection_embedding_model(get_qdrant_client())

    def upsert(self, points: list[ChunkPoint]) -> None:
        upsert_chunks(
            get_qdrant_client(),
            [
                PointStruct(id=p.id, vector=chunk_vector(p.dense, p.sparse), payload=p.payload)
                for p in points
            ],
        )

    def hybrid_query(
        self,
        *,
        dense: list[float],
        sparse: SparseVector,
        claims: UserClaims,
        allowed_classifications: list[str],
        limit: int,
    ) -> list[Hit]:
        # FR-26 filter, unchanged -- still applied inside every per-collection
        # query. Per-collection separation bounds the blast radius; it does
        # not replace claims-derived filtering (issue #229's stated scope).
        access_filter = build_access_filter(claims, allowed_classifications=allowed_classifications)
        client = get_qdrant_client()
        per_collection: list[list[Hit]] = []
        try:
            for classification in allowed_classifications:
                name = classification_collection_name(classification)
                if not client.collection_exists(name):
                    # Not every classification has been ingested into yet --
                    # a level with zero approved documents is an empty result,
                    # not an error.
                    continue
                hits = client.query_points(
                    collection_name=name,
                    prefetch=[
                        Prefetch(
                            query=dense,
                            using=DENSE_VECTOR,
                            filter=access_filter,
                            limit=limit,
                        ),
                        Prefetch(
                            query=sparse,
                            using=SPARSE_VECTOR,
                            filter=access_filter,
                            limit=limit,
                        ),
                    ],
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=limit,
                ).points
                per_collection.append(
                    [Hit(id=str(h.id), score=h.score, payload=h.payload or {}) for h in hits]
                )
        except (UnexpectedResponse, httpx.HTTPError) as exc:
            raise VectorStoreUnavailable(str(exc)) from exc
        return fuse_ranked(per_collection, limit=limit)

    def access_filter_summary(self, claims: UserClaims, allowed_classifications: list[str]) -> dict:
        # The FR-26 clauses, same shape as before the collection split, plus
        # (issue #229) which collections those clauses are actually applied
        # against -- useful audit/observability evidence that the split is
        # real, not just the filter's classification clause.
        summary = build_access_filter(
            claims, allowed_classifications=allowed_classifications
        ).model_dump(exclude_none=True)
        summary["collections"] = [
            classification_collection_name(c) for c in allowed_classifications
        ]
        return summary

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        update_document_payload(get_qdrant_client(), document_id, classification, fields)

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        delete_document_chunks(get_qdrant_client(), document_id, classification)
