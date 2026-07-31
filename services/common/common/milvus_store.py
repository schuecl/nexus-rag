"""#160: the opt-in Milvus implementation of the VectorStore seam.

Selected only by VECTOR_BACKEND=milvus; a deployment runs this XOR the
Qdrant backend, never both. The mapping is deliberately one-to-one with the
Qdrant path so an A/B comparison measures the engine, not the plumbing:

    Qdrant                                Milvus (here)
    ------                                -------------
    dense+sparse named vectors            dense FLOAT_VECTOR + sparse
                                          SPARSE_FLOAT_VECTOR fields
    Prefetch x2 + FusionQuery(RRF)        AnnSearchRequest x2 + RRFRanker
    payload filter on both legs (FR-26)   boolean expr on both requests
    set_payload by document_id filter     query rows -> patch -> upsert
    delete by document_id filter          delete(filter=...)

Two deliberate sameness choices, both about comparability (#160):

- The sparse leg reuses common/sparse_embedding.py's client-side fastembed
  BM25 vectors -- NOT Milvus's server-side BM25 Function. Milvus's Function
  tokenizes with its own analyzer, which would make the sparse legs differ
  by construction and un-attribute any quality delta. Same input vectors,
  same tokenizer, both engines: the engine is the only variable. (Trade-off
  honestly noted: this skips Milvus's built-in IDF weighting; the sparse
  metric is IP over the same raw term-frequency vectors Qdrant receives.)
- Dense metric is COSINE, matching qdrant_store.ensure_collection.

FR-26 (Section 6.1) is enforced exactly as the Qdrant filter does: status
approved AND classification in the user's allowed set AND releasability
overlaps {NONE, *user's} AND access_scope overlaps {ALL_AUTHENTICATED, sub,
groups, org}. Built server-side from verified claims; string values are
escaped before entering the expression so a hostile group/org name cannot
break out of the filter (the injection rule log_safety.py applies to logs,
applied here to filter expressions).

Issue #229 (recorded "not yet", not a silent gap): QdrantStore splits the
corpus into one collection per classification level for defence in depth;
this backend does not. `ensure_ready`/`update_document_payload`/
`delete_document_chunks` accept the same `classification` parameter the
Protocol now requires but ignore it, continuing to operate on the single
`MILVUS_COLLECTION` -- the FR-26 boolean expression above is still the sole
enforcement point here, exactly as it was before #229. Milvus support for the
same per-classification split, if wanted, is separate follow-up work: unlike
Qdrant's collections (cheap, created on demand), Milvus collection creation is
comparatively heavyweight and its RBAC/partition model would need its own
design pass rather than a direct port of qdrant_store.py's approach.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

from common.metadata import ALL_AUTHENTICATED_ACCESS_SCOPE, NO_RELEASABILITY_RESTRICTION
from common.qdrant_store import EMBEDDING_MODEL_KEY
from common.vector_store import Hit, VectorStoreUnavailable

if TYPE_CHECKING:
    from qdrant_client.models import SparseVector

    from common.claims import UserClaims
    from common.vector_store import ChunkPoint

MILVUS_URL = os.environ.get("MILVUS_URL", "http://milvus:19530")
# NFR-15: authenticated access in every environment. "user:password" form;
# dev default matches Milvus's bootstrap credentials, production overrides.
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "root:Milvus")
MILVUS_COLLECTION = os.environ.get("MILVUS_COLLECTION", "nexus_rag_chunks")

# FR-26 fields promoted out of the payload into typed, filterable columns.
_PROMOTED = ("document_id", "status", "classification", "releasability", "access_scope")


@lru_cache(maxsize=1)
def _client():
    from pymilvus import MilvusClient

    return MilvusClient(uri=MILVUS_URL, token=MILVUS_TOKEN)


def _quote(value: str) -> str:
    """Escape a string for a Milvus boolean expression. Claims are verified,
    classification values come from admin lists -- but a group name is still
    external input, and a quote inside one must not terminate the string
    literal it sits in."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _string_list(values: list[str]) -> str:
    return "[" + ", ".join(_quote(v) for v in values) + "]"


def build_access_expr(claims: UserClaims, *, allowed_classifications: list[str]) -> str:
    """The FR-26 mandatory filter as a Milvus boolean expression -- the exact
    semantics of qdrant_filters.build_access_filter, clause for clause."""
    scope_values = {ALL_AUTHENTICATED_ACCESS_SCOPE, claims.sub, *claims.groups}
    if claims.org:
        scope_values.add(claims.org)
    return (
        f'status == "approved"'
        f" and classification in {_string_list(allowed_classifications)}"
        f" and array_contains_any(releasability, "
        f"{_string_list([NO_RELEASABILITY_RESTRICTION, *claims.releasability])})"
        f" and array_contains_any(access_scope, {_string_list(sorted(scope_values))})"
    )


def _sparse_dict(sparse: SparseVector) -> dict[int, float]:
    return {int(i): float(v) for i, v in zip(sparse.indices, sparse.values, strict=True)}


class MilvusStore:
    def ensure_ready(self, dense_size: int, classification: str) -> None:
        # classification: unused, see module docstring (#229 not implemented here).
        del classification
        from pymilvus import DataType

        client = _client()
        if client.has_collection(MILVUS_COLLECTION):
            return
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dense_size)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("status", DataType.VARCHAR, max_length=32)
        schema.add_field("classification", DataType.VARCHAR, max_length=128)
        schema.add_field(
            "releasability",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=64,
            max_length=256,
        )
        schema.add_field(
            "access_scope",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=256,
            max_length=256,
        )
        schema.add_field("payload", DataType.JSON)
        index_params = client.prepare_index_params()
        # COSINE to match the Qdrant collection's dense metric; the sparse
        # side is IP over the shared client-side BM25 vectors (see module
        # docstring for why not Milvus's server-side BM25 Function).
        index_params.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
        )
        # Strong consistency, deliberately: Milvus defaults to bounded
        # staleness, under which a curator's approve/reject flip (or a fresh
        # upsert) may not be visible to the very next query -- behavior the
        # pipeline nowhere tolerates, because Qdrant reads-after-writes
        # immediately and the curation flow (and its tests) rely on that.
        # The latency cost of Strong is part of what the #160 comparison
        # measures rather than something to hide. Surfaced by the live
        # validation run, not by review.
        client.create_collection(
            collection_name=MILVUS_COLLECTION,
            schema=schema,
            index_params=index_params,
            consistency_level="Strong",
        )

    def stored_embedding_model(self) -> str | None:
        client = _client()
        if not client.has_collection(MILVUS_COLLECTION):
            return None
        rows = client.query(
            collection_name=MILVUS_COLLECTION, filter="", output_fields=["payload"], limit=1
        )
        if not rows:
            return None
        return (rows[0].get("payload") or {}).get(EMBEDDING_MODEL_KEY)

    def upsert(self, points: list[ChunkPoint]) -> None:
        rows = []
        for p in points:
            payload = dict(p.payload)
            row = {
                "id": p.id,
                "dense": p.dense,
                "sparse": _sparse_dict(p.sparse),
                # FR-26 fields become typed columns; everything else (text,
                # heading, provenance stamp, ...) stays in the JSON payload,
                # which hybrid_query returns whole so rag_search sees the
                # same payload shape either backend.
                "document_id": str(payload.get("document_id", "")),
                "status": str(payload.get("status", "")),
                "classification": str(payload.get("classification", "")),
                "releasability": list(payload.get("releasability", [])),
                "access_scope": list(payload.get("access_scope", [])),
                "payload": payload,
            }
            rows.append(row)
        _client().upsert(collection_name=MILVUS_COLLECTION, data=rows)

    def hybrid_query(
        self,
        *,
        dense: list[float],
        sparse: SparseVector,
        claims: UserClaims,
        allowed_classifications: list[str],
        limit: int,
    ) -> list[Hit]:
        from pymilvus import AnnSearchRequest, MilvusException, RRFRanker

        expr = build_access_expr(claims, allowed_classifications=allowed_classifications)
        try:
            results = _client().hybrid_search(
                collection_name=MILVUS_COLLECTION,
                reqs=[
                    # FR-26 on BOTH legs, same as the Qdrant Prefetch pair --
                    # neither retrieval path can bypass the filter.
                    AnnSearchRequest(
                        data=[dense],
                        anns_field="dense",
                        param={"metric_type": "COSINE"},
                        limit=limit,
                        expr=expr,
                    ),
                    AnnSearchRequest(
                        data=[_sparse_dict(sparse)],
                        anns_field="sparse",
                        param={"metric_type": "IP"},
                        limit=limit,
                        expr=expr,
                    ),
                ],
                ranker=RRFRanker(),
                limit=limit,
                output_fields=["payload"],
            )
        except MilvusException as exc:
            raise VectorStoreUnavailable(str(exc)) from exc
        return [
            Hit(
                id=str(r["id"]),
                score=float(r["distance"]),
                payload=(r.get("entity") or {}).get("payload") or {},
            )
            for r in (results[0] if results else [])
        ]

    def find_similar_approved(
        self, *, dense: list[float], limit: int, exclude_document_id: str | None = None
    ) -> list[Hit]:
        # Issue #229 not implemented here (see module docstring): one
        # collection, so unlike QdrantStore's per-collection fan-out this is
        # a single search. No claims-derived expr -- see the Protocol
        # docstring for why this path has no caller identity to scope one
        # from; `status == "approved"` is still hardcoded.
        from pymilvus import MilvusException

        expr = 'status == "approved"'
        if exclude_document_id is not None:
            expr += f" and document_id != {_quote(exclude_document_id)}"
        try:
            results = _client().search(
                collection_name=MILVUS_COLLECTION,
                data=[dense],
                anns_field="dense",
                search_params={"metric_type": "COSINE"},
                limit=limit,
                filter=expr,
                output_fields=["payload"],
            )
        except MilvusException as exc:
            raise VectorStoreUnavailable(str(exc)) from exc
        return [
            Hit(
                id=str(r["id"]),
                score=float(r["distance"]),
                payload=(r.get("entity") or {}).get("payload") or {},
            )
            for r in (results[0] if results else [])
        ]

    def access_filter_summary(self, claims: UserClaims, allowed_classifications: list[str]) -> dict:
        return {
            "backend": "milvus",
            "expr": build_access_expr(claims, allowed_classifications=allowed_classifications),
        }

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        """FR-13 parity. Milvus has no update-by-filter, so: read the
        document's rows (vectors included), patch, upsert back. Bounded by a
        document's chunk count, and curation is a human-paced action -- the
        cost is acceptable and stated rather than hidden.

        classification: unused, see module docstring (#229 not implemented
        here) -- a classification correction is a plain payload patch like
        any other field, not a move between collections.
        """
        del classification
        client = _client()
        rows = client.query(
            collection_name=MILVUS_COLLECTION,
            filter=f"document_id == {_quote(document_id)}",
            output_fields=[
                "id",
                "dense",
                "sparse",
                *_PROMOTED,
                "payload",
            ],
            limit=16384,
        )
        if not rows:
            return
        for row in rows:
            payload = dict(row.get("payload") or {})
            payload.update(fields)
            row["payload"] = payload
            for key in _PROMOTED:
                if key in fields:
                    row[key] = fields[key]
        client.upsert(collection_name=MILVUS_COLLECTION, data=rows)

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        # classification: unused, see module docstring (#229 not implemented here).
        del classification
        _client().delete(
            collection_name=MILVUS_COLLECTION,
            filter=f"document_id == {_quote(document_id)}",
        )
