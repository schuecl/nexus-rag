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

Issue #229 parity (issue #546): QdrantStore splits the corpus into one
collection per classification level for defence in depth. This backend gets
the same property Milvus-natively via **one partition per classification**
inside `MILVUS_COLLECTION` (Milvus collections are heavyweight; partitions
are cheap and every read/write path accepts partition scoping):

- `ensure_ready` creates the level's partition (`cls_<slug>`, same slug rules
  as qdrant_store.classification_collection_name) and migrates any legacy
  rows out of `_default` (paged, routed by the typed `classification`
  column) -- the same auto-migration posture the Qdrant backend took for its
  pre-#229 shared collection.
- `upsert` routes each row to its classification's partition.
- `hybrid_query` searches only the partitions for classifications the caller
  is allowed (intersected with the partitions that actually exist; an empty
  intersection is an empty result, mirroring Qdrant's collection_exists
  skip). The FR-26 expression stays on BOTH ANN legs: partition scoping is
  the blast-radius bound, never the filter.
- A curator's classification correction MOVES a document's rows between
  partitions with qdrant_store's exact ordering and failure semantics
  (target written first, corrected fields only on the new copy, source
  delete failure logged-not-raised because the leftover is never `approved`,
  upsert failure raised with nothing changed), including both
  retry-idempotency fallbacks -- see update_document_payload.
- `find_similar_approved` deliberately stays collection-wide (Protocol
  semantics: every level searched, no caller identity to scope by).

Until a legacy `_default` row is migrated it is unreachable by the
partition-scoped query path -- fail closed, never exposure.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING

from common.log_safety import log_safe
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

logger = logging.getLogger(__name__)

# Fields needed to reconstruct a row for a partition move (vectors included).
_FULL_ROW_FIELDS = ("id", "dense", "sparse", *_PROMOTED, "payload")

_PARTITION_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PARTITION_PREFIX = "cls_"
# Milvus's built-in partition every row lands in when no partition is named --
# where pre-#546 rows live until ensure_ready migrates them.
_DEFAULT_PARTITION = "_default"
_MIGRATION_PAGE_SIZE = 1000


def partition_name_for(classification: str) -> str:
    """The partition that holds one Classification level's chunks -- same
    slug semantics as qdrant_store.classification_collection_name, same
    empty-slug fallback, so the two backends agree on which values are
    distinct."""
    slug = _PARTITION_SLUG_RE.sub("_", classification.strip().lower()).strip("_")
    return _PARTITION_PREFIX + (slug or "unspecified")


def _existing_classification_partitions(wanted: list[str]) -> list[str]:
    """`wanted` classifications' partition names, filtered to those that
    exist -- a level with zero ingested documents is an empty result, not an
    error (mirrors the Qdrant backend's collection_exists skip)."""
    client = _client()
    if not client.has_collection(MILVUS_COLLECTION):
        return []
    existing = set(client.list_partitions(collection_name=MILVUS_COLLECTION))
    return [name for name in (partition_name_for(c) for c in wanted) if name in existing]


def _all_classification_partitions() -> list[str]:
    client = _client()
    if not client.has_collection(MILVUS_COLLECTION):
        return []
    return [
        name
        for name in client.list_partitions(collection_name=MILVUS_COLLECTION)
        if name.startswith(_PARTITION_PREFIX)
    ]


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
        from pymilvus import DataType

        client = _client()
        if client.has_collection(MILVUS_COLLECTION):
            self._ensure_partition(classification)
            _migrate_default_partition()
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
        self._ensure_partition(classification)

    def _ensure_partition(self, classification: str) -> None:
        """Issue #546 (#229 parity): the level's partition, created on demand
        the way the Qdrant backend creates a level's collection."""
        self._ensure_partition_by_name(partition_name_for(classification))

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
        by_partition: dict[str, list[dict]] = {}
        for p in points:
            payload = dict(p.payload)
            classification = str(payload.get("classification", ""))
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
                "classification": classification,
                "releasability": list(payload.get("releasability", [])),
                "access_scope": list(payload.get("access_scope", [])),
                "payload": payload,
            }
            # #546: rows live in their level's partition. ensure_ready has
            # normally created it already (the worker calls it per document);
            # _ensure_partition is cheap idempotence for any other caller.
            by_partition.setdefault(partition_name_for(classification), []).append(row)
        client = _client()
        for partition, rows in by_partition.items():
            self._ensure_partition_by_name(partition)
            client.upsert(collection_name=MILVUS_COLLECTION, data=rows, partition_name=partition)

    def _ensure_partition_by_name(self, name: str) -> None:
        client = _client()
        if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=name):
            client.create_partition(collection_name=MILVUS_COLLECTION, partition_name=name)

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
            # #546 (#229 parity): only the allowed levels' partitions are
            # searched -- the blast-radius bound. The expr on both legs stays
            # the enforcement point, exactly as the collection split does not
            # replace the filter on the Qdrant side.
            partitions = _existing_classification_partitions(allowed_classifications)
            if not partitions:
                return []
            results = _client().hybrid_search(
                collection_name=MILVUS_COLLECTION,
                partition_names=partitions,
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

        Issue #546 (#229 parity): `classification` is which partition the
        chunks are in *before* this call -- the caller's best belief, from a
        Postgres value that can be stale after a partial failure (NFR-13
        retries the same API call). Semantics mirror
        qdrant_store.update_document_payload exactly:

        - fields correcting `classification` -> move the rows to the target
          partition (_move_document_partition); if the claimed source has
          nothing, a prior attempt may have completed the move and failed
          only on cleanup -- patch the target instead of silently no-op'ing.
        - non-migrating update -> patch in the claimed partition; if nothing
          is there, search every other classification partition before
          giving up, since an earlier correction may have moved the rows
          somewhere this caller doesn't know about.
        """
        current = partition_name_for(classification)
        new_classification = fields.get("classification")

        if new_classification and new_classification != classification:
            if _move_document_partition(
                document_id, current, partition_name_for(str(new_classification)), fields
            ):
                return
            _patch_if_present(document_id, partition_name_for(str(new_classification)), fields)
            return

        if _patch_if_present(document_id, current, fields):
            return
        for name in _all_classification_partitions():
            if name != current and _patch_if_present(document_id, name, fields):
                return

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        # #547 review: `classification` is accepted for API symmetry but
        # deliberately NOT used for scoping, mirroring
        # qdrant_store.delete_document_chunks -- destruction sweeps EVERY
        # classification partition plus `_default`. A classification-
        # correction move whose cleanup delete failed (the documented
        # logged-not-raised path in _move_document_partition) leaves an inert
        # duplicate in a partition other than the one Postgres currently
        # names; it can never pass FR-26, but purge (#123) and supersession
        # (FR-7) exist to make the bytes actually gone, and a single-
        # partition delete would report success while the old partition's
        # copy of the chunk text survived indefinitely. `_default` is swept
        # for the same reason the legacy-migration story exists: pre-#546
        # rows live there until ensure_ready migrates them.
        del classification
        client = _client()
        # A document that never finished embedding has no collection at all
        # yet: a no-op, not an error (same as the Qdrant backend).
        if not client.has_collection(MILVUS_COLLECTION):
            return
        expr = f"document_id == {_quote(document_id)}"
        for name in [*_all_classification_partitions(), _DEFAULT_PARTITION]:
            if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=name):
                continue
            client.delete(
                collection_name=MILVUS_COLLECTION,
                partition_name=name,
                filter=expr,
            )

    def fetch_document_chunks(self, document_id: str, classification: str) -> list[dict]:
        # #546: scoped to the level's partition, mirroring QdrantStore's
        # per-collection lookup.
        name = partition_name_for(classification)
        client = _client()
        if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=name):
            return []
        rows = client.query(
            collection_name=MILVUS_COLLECTION,
            partition_names=[name],
            filter=f"document_id == {_quote(document_id)}",
            output_fields=["payload"],
            limit=16384,
        )
        chunks = [dict(row.get("payload") or {}) for row in rows]
        chunks.sort(key=lambda payload: payload.get("chunk_index", 0))
        return chunks

    def replace_document_chunks(
        self, document_id: str, classification: str, points: list[ChunkPoint]
    ) -> None:
        """Issue #362. classification: unused, see module docstring (#229 not
        implemented here).

        `upsert` already replaces same-id rows in place (Milvus upserts by
        primary key), so new-before-old here is really "upsert, then sweep
        anything the new chunk count leaves behind" -- there's no typed
        `chunk_index` column to filter a delete expression on (it lives only
        in the JSON `payload`, like update_document_payload's fields), so the
        stale ids are found the same way that method finds rows to patch:
        query by document_id, inspect `payload` in Python, delete by id.

        #546: the sweep is scoped to the level's partition (upsert already
        routed the new rows there by their own classification).
        """
        self.upsert(points)
        name = partition_name_for(classification)
        client = _client()
        if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=name):
            return
        new_count = len(points)
        rows = client.query(
            collection_name=MILVUS_COLLECTION,
            partition_names=[name],
            filter=f"document_id == {_quote(document_id)}",
            output_fields=["id", "payload"],
            limit=16384,
        )
        stale_ids = [
            row["id"]
            for row in rows
            if (row.get("payload") or {}).get("chunk_index", 0) >= new_count
        ]
        if stale_ids:
            client.delete(
                collection_name=MILVUS_COLLECTION,
                partition_name=name,
                filter=f"id in {_string_list(stale_ids)}",
            )


def _patch_if_present(document_id: str, partition: str, fields: dict) -> bool:
    """Patch a document's rows inside one partition, in place. Returns
    whether anything was there -- the mirror of qdrant_store's
    _set_payload_if_present, so update_document_payload's retry fallbacks
    can tell "patched" apart from "nothing at this location"."""
    client = _client()
    if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=partition):
        return False
    rows = client.query(
        collection_name=MILVUS_COLLECTION,
        partition_names=[partition],
        filter=f"document_id == {_quote(document_id)}",
        output_fields=list(_FULL_ROW_FIELDS),
        limit=16384,
    )
    if not rows:
        return False
    for row in rows:
        payload = dict(row.get("payload") or {})
        payload.update(fields)
        row["payload"] = payload
        for key in _PROMOTED:
            if key in fields:
                row[key] = fields[key]
    client.upsert(collection_name=MILVUS_COLLECTION, data=rows, partition_name=partition)
    return True


def _move_document_partition(document_id: str, source: str, target: str, fields: dict) -> bool:
    """Issue #546: a curator's classification correction moves a document's
    rows between partitions -- the partition *is* the classification now, so
    a corrected value can't stay where it is the way a status/releasability/
    access_scope correction can.

    Safety argument, verbatim from qdrant_store._migrate_document_classification
    (and purge.py's rule that a partial failure always leaves the document
    less exposed, never more): the target partition is written *before* the
    source is cleared, and every corrected field is written only onto the
    new copy. The source rows are never payload-mutated, only deleted -- a
    failed delete leaves an inert duplicate that is not `approved` (curation
    corrections happen during approval of a still-pending document) and so
    can never pass FR-26; it is logged rather than raised. A failure during
    the target upsert, by contrast, is raised: nothing has changed yet, so
    the caller's NFR-13 revert applies as it would to any patch failure.

    Returns whether anything was actually moved, so update_document_payload
    can tell "nothing to move" apart from "a prior attempt already moved it".
    """
    from pymilvus import MilvusException

    client = _client()
    if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=source):
        return False
    rows = client.query(
        collection_name=MILVUS_COLLECTION,
        partition_names=[source],
        filter=f"document_id == {_quote(document_id)}",
        output_fields=list(_FULL_ROW_FIELDS),
        limit=16384,
    )
    if not rows:
        return False
    moved_ids: list[str] = []
    for row in rows:
        payload = dict(row.get("payload") or {})
        payload.update(fields)
        row["payload"] = payload
        for key in _PROMOTED:
            if key in fields:
                row[key] = fields[key]
        moved_ids.append(row["id"])
    if not client.has_partition(collection_name=MILVUS_COLLECTION, partition_name=target):
        client.create_partition(collection_name=MILVUS_COLLECTION, partition_name=target)
    client.upsert(collection_name=MILVUS_COLLECTION, data=rows, partition_name=target)
    try:
        client.delete(
            collection_name=MILVUS_COLLECTION,
            partition_name=source,
            filter=f"id in {_string_list(moved_ids)}",
        )
    except MilvusException:
        # log_safe (#465 pattern): document_id is uuid-typed in every caller,
        # but the partition names derive from the user-supplied classification
        # string at upload -- none of the three may inject log lines.
        logger.warning(
            "document %s moved to partition %s but cleanup delete from %s failed; "
            "the leftover rows are not approved and cannot pass FR-26 -- clean up "
            "with a retry or re-approval",
            log_safe(document_id),
            log_safe(target),
            log_safe(source),
        )
    return True


# #547 review (minor): ensure_ready runs once per ingested document, but the
# `_default` migration is a one-time healing step -- without this flag every
# ingest for the process's whole lifetime would pay a query against a long-
# empty partition. Once a probe confirms `_default` empty, later ensure_ready
# calls skip it. Per-process, deliberately: a worker restart re-probes once,
# which is also what makes rows appearing in `_default` later (another
# pre-#546 writer, a restored volume) heal on the next process's first
# ingest rather than never.
_default_migration_state = {"confirmed_empty": False}


def _migrate_default_partition() -> None:
    """Issue #546: rows ingested before partitioning live in Milvus's
    built-in `_default` partition, unreachable by the partition-scoped query
    path (fail closed, never exposure). Route them to their level's
    partition by the typed `classification` column -- the same auto-migration
    posture qdrant_store took for its pre-#229 shared collection. Paged;
    runs from ensure_ready, so a stack heals on its next ingest without an
    operator step."""
    if _default_migration_state["confirmed_empty"]:
        return
    client = _client()
    if not client.has_collection(MILVUS_COLLECTION):
        return
    while True:
        rows = client.query(
            collection_name=MILVUS_COLLECTION,
            partition_names=[_DEFAULT_PARTITION],
            filter='id != ""',
            output_fields=list(_FULL_ROW_FIELDS),
            limit=_MIGRATION_PAGE_SIZE,
        )
        if not rows:
            _default_migration_state["confirmed_empty"] = True
            return
        by_partition: dict[str, list[dict]] = {}
        for row in rows:
            by_partition.setdefault(
                partition_name_for(str(row.get("classification", ""))), []
            ).append(row)
        for partition, batch in by_partition.items():
            if not client.has_partition(
                collection_name=MILVUS_COLLECTION, partition_name=partition
            ):
                client.create_partition(collection_name=MILVUS_COLLECTION, partition_name=partition)
            client.upsert(collection_name=MILVUS_COLLECTION, data=batch, partition_name=partition)
        migrated_ids = [row["id"] for row in rows]
        client.delete(
            collection_name=MILVUS_COLLECTION,
            partition_name=_DEFAULT_PARTITION,
            filter=f"id in {_string_list(migrated_ids)}",
        )
        logger.info(
            "migrated %d legacy rows out of %s into classification partitions",
            len(rows),
            _DEFAULT_PARTITION,
        )
