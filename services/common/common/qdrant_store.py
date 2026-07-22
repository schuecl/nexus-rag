"""FR-6: shared Qdrant collection/write helpers. Both ingestion-api (writes
chunks) and orchestration-mcp (reads them via qdrant_filters.build_access_filter)
need to agree on the collection name and the payload shape -- centralized here
rather than duplicated per service."""

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
    PointStruct,
    VectorParams,
)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "nexus_rag_chunks")


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(client: QdrantClient, vector_size: int) -> None:
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


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
