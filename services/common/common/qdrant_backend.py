"""#160: the default VectorStore backend -- a thin adapter over the existing
common/qdrant_store.py and common/qdrant_filters.py, which are deliberately
untouched. Every method is pure delegation; with VECTOR_BACKEND unset (or
"qdrant") the pipeline's behavior is byte-for-byte what it was before the
seam existed. That is a stated gate of #160: the comparison is only honest
if the incumbent's path didn't change while the seam went in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Fusion, FusionQuery, PointStruct, Prefetch

from common.qdrant_filters import build_access_filter
from common.qdrant_store import (
    DENSE_VECTOR,
    QDRANT_COLLECTION,
    SPARSE_VECTOR,
    chunk_vector,
    collection_embedding_model,
    delete_document_chunks,
    ensure_collection,
    get_qdrant_client,
    update_document_payload,
    upsert_chunks,
)
from common.vector_store import Hit, VectorStoreUnavailable

if TYPE_CHECKING:
    from qdrant_client.models import SparseVector

    from common.claims import UserClaims
    from common.vector_store import ChunkPoint


class QdrantStore:
    def ensure_ready(self, dense_size: int) -> None:
        ensure_collection(get_qdrant_client(), dense_size=dense_size)

    def stored_embedding_model(self) -> str | None:
        return collection_embedding_model(get_qdrant_client())

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
        access_filter = build_access_filter(claims, allowed_classifications=allowed_classifications)
        try:
            hits = (
                get_qdrant_client()
                .query_points(
                    collection_name=QDRANT_COLLECTION,
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
                )
                .points
            )
        except (UnexpectedResponse, httpx.HTTPError) as exc:
            raise VectorStoreUnavailable(str(exc)) from exc
        return [Hit(id=str(h.id), score=h.score, payload=h.payload or {}) for h in hits]

    def access_filter_summary(self, claims: UserClaims, allowed_classifications: list[str]) -> dict:
        # Identical to the pre-seam rag_search summary: the built Filter's
        # model_dump, so audit rows and API responses don't change shape.
        return build_access_filter(
            claims, allowed_classifications=allowed_classifications
        ).model_dump(exclude_none=True)

    def update_document_payload(self, document_id: str, fields: dict) -> None:
        update_document_payload(get_qdrant_client(), document_id, fields)

    def delete_document_chunks(self, document_id: str) -> None:
        delete_document_chunks(get_qdrant_client(), document_id)
