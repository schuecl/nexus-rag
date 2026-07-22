"""FR-6: shared Qdrant collection/write helpers. Both ingestion-api (writes
chunks) and orchestration-mcp (reads them via qdrant_filters.build_access_filter)
need to agree on the collection name and the payload shape -- centralized here
rather than duplicated per service.

FR-24: each point carries two named vectors -- a dense one (DENSE_VECTOR) for
semantic search and a BM25 sparse one (SPARSE_VECTOR, see common.sparse_embedding)
for keyword search -- so orchestration-mcp can fuse both at query time. The
sparse field's Modifier.IDF makes Qdrant apply real IDF weighting server-side
against the corpus, on top of the raw term-frequency vectors this project
generates; without it these would just be term counts, not BM25 scores.

Schema note: this replaces the single unnamed vector used before hybrid search
was implemented -- an existing dev Qdrant volume created before this change
needs to be recreated (`docker compose down -v`), since ensure_collection only
configures a collection when it doesn't already exist.
"""

from __future__ import annotations

import os
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "nexus_rag_chunks")

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(client: QdrantClient, dense_size: int) -> None:
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config={DENSE_VECTOR: VectorParams(size=dense_size, distance=Distance.COSINE)},
            sparse_vectors_config={SPARSE_VECTOR: SparseVectorParams(modifier=Modifier.IDF)},
        )


def chunk_vector(dense: list[float], sparse: SparseVector) -> dict:
    return {DENSE_VECTOR: dense, SPARSE_VECTOR: sparse}


def upsert_chunks(client: QdrantClient, points: list[PointStruct]) -> None:
    client.upsert(collection_name=QDRANT_COLLECTION, points=points)


def update_document_payload(client: QdrantClient, document_id: str, fields: dict) -> None:
    """FR-13: propagate a curator's decision -- status, and any classification/
    releasability/access_scope corrections made at approval time -- to every
    chunk of a document. Qdrant is the enforcement point for FR-26, so this is
    what actually changes what's (in)visible to queries; the Postgres Document
    row (common.models) is the system of record for the curation workflow
    itself, and this keeps Qdrant's copy from going stale relative to it.
    Corrections matter here as much as status: an uncorrected Qdrant payload
    would keep enforcing the uploader's original (possibly wrong) tags even
    after a curator fixes them."""
    client.set_payload(
        collection_name=QDRANT_COLLECTION,
        payload=fields,
        points=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
            )
        ),
    )


def set_document_status(client: QdrantClient, document_id: str, status: str) -> None:
    update_document_payload(client, document_id, {"status": status})


def delete_document_chunks(client: QdrantClient, document_id: str) -> None:
    """FR-7: remove every chunk belonging to a document that's been superseded
    by a newer version, so re-ingestion doesn't leave orphaned or duplicate
    entries behind. Called at the point a curator approves the *replacing*
    document (app/routes/curate.py), not at submission time -- see
    common.models.Document.supersedes_document_id."""
    client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
            )
        ),
    )
