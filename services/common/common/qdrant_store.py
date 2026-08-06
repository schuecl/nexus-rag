"""FR-6: shared Qdrant collection/write helpers. Both ingestion-api (writes
chunks) and orchestration-mcp (reads them via qdrant_filters.build_access_filter)
need to agree on the collection naming and the payload shape -- centralized here
rather than duplicated per service.

FR-24: each point carries two named vectors -- a dense one (DENSE_VECTOR) for
semantic search and a BM25 sparse one (SPARSE_VECTOR, see common.sparse_embedding)
for keyword search -- so orchestration-mcp can fuse both at query time. The
sparse field's Modifier.IDF makes Qdrant apply real IDF weighting server-side
against the corpus, on top of the raw term-frequency vectors this project
generates; without it these would just be term counts, not BM25 scores.

Issue #229: one collection per Classification level, not one collection for
the whole corpus. `classification_collection_name` derives the collection for
a given value and every read/write helper below is scoped to it. This is
defence in depth on top of FR-26's mandatory claims-derived filter (which
stays -- see qdrant_filters.py -- and still applies inside each collection):
a store-level reader, or a future retrieval path that forgets the filter, is
now bounded to one compartment instead of the entire corpus. Classification
values are admin-configurable (C9) and collections are created on demand, so
this makes no assumption about a fixed set of levels.

A curator's classification correction (app/routes/curate.py) moves a
document's chunks between collections -- see
`_migrate_document_classification`. That is the one operation here that isn't
a plain per-collection write, and its safety argument is documented on that
function.

Schema note: this replaced the single unnamed vector used before hybrid search
was implemented, and now replaces the single shared collection used before
issue #229 -- an existing dev Qdrant volume created before either change needs
to be recreated (`docker compose down -v`).
"""

from __future__ import annotations

import logging
import os
import re
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
    Range,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

logger = logging.getLogger("qdrant_store")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "nexus_rag_chunks")
# NFR-15: Qdrant must require authenticated access in every environment,
# including local dev. Each caller sets this to whichever key its deployment
# config grants it -- ingestion-worker and ingestion-api both get the full
# read/write key (ingestion-worker creates collections and writes new points;
# ingestion-api updates/deletes points on approve/reject/supersede),
# orchestration-mcp gets a read-only key (it only ever calls query_points) --
# this module doesn't need to know which is which, it just forwards whatever's
# in its own environment.
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"

# Issue #122: which embedding model produced a chunk's dense vector, stamped
# into every point's payload at write time (ingestion-worker) and read back at
# query time (orchestration-mcp) to detect a model change.
#
# Qdrant has no collection-level metadata to hang this on, so provenance lives
# per-point and a collection's model is read by sampling one. That is
# sufficient because every point in a collection is written by the same
# ingestion path -- a mixed collection is precisely the state this is meant to
# make visible.
EMBEDDING_MODEL_KEY = "embedding_model"

# Issue #229: how a Classification value (an admin-configured, free-form
# string -- C9) becomes a valid Qdrant collection name segment.
_COLLECTION_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_COLLECTION_PREFIX = f"{QDRANT_COLLECTION}__"
# Migration pagination -- bounds how many points a single scroll page returns
# while moving a document's chunks between collections (mirrors the cap
# MilvusStore.update_document_payload uses for the same "one document's worth
# of chunks" shape).
_MIGRATION_PAGE_SIZE = 1000


def classification_collection_name(classification: str) -> str:
    """The Qdrant collection that holds chunks for one Classification value.

    Deterministic and collision-resistant for the values this project
    actually uses (short admin-configured labels like `UNCLASSIFIED`, `CUI`,
    `TOP SECRET`) -- not a general-purpose slugifier. An empty-after-slugging
    value (a classification of only symbols) falls back to a fixed segment
    rather than colliding with the bare prefix.
    """
    slug = _COLLECTION_SLUG_RE.sub("_", classification.strip().lower()).strip("_")
    return _COLLECTION_PREFIX + (slug or "unspecified")


def existing_classification_collections(client: QdrantClient) -> list[str]:
    """Every per-classification collection that currently exists. Used for
    provenance checks (see `any_collection_embedding_model`) and admin/debug
    tooling -- never assumes a fixed set of classification values (C9), since
    levels can be added or retired at runtime."""
    return [
        c.name
        for c in client.get_collections().collections
        if c.name.startswith(_COLLECTION_PREFIX)
    ]


def collection_embedding_model(client: QdrantClient, classification: str) -> str | None:
    """The embedding model recorded on one classification collection's
    points, or None.

    None means "cannot tell", not "no mismatch", and has two distinct causes
    the caller must treat as non-fatal:

    - the collection doesn't exist yet (created lazily on first ingestion of
      that classification), or is empty;
    - the points predate this stamp (written before #122), so nothing
      recorded which model produced them.

    Hard-failing on either would break every existing deployment on upgrade
    and every fresh stack before its first document. The check only fails
    closed on a *positive* mismatch -- see rag_search's use of it.
    """
    name = classification_collection_name(classification)
    if not client.collection_exists(name):
        return None
    points, _ = client.scroll(collection_name=name, limit=1, with_payload=True, with_vectors=False)
    if not points:
        return None
    return (points[0].payload or {}).get(EMBEDDING_MODEL_KEY)


def any_collection_embedding_model(client: QdrantClient) -> str | None:
    """Provenance across every existing classification collection.

    Issue #229 means sampling one arbitrary collection (the pre-split
    behavior) is no longer sufficient: each classification's collection is
    populated -- and could in principle be re-embedded -- independently.
    Returns the single model name if every sampled collection agrees, `None`
    if nothing can be read yet (no collections exist, or none are stamped),
    or a synthetic `mixed:<names>` value if collections disagree. The caller
    (issue #122's mismatch check in orchestration-mcp) compares this against
    its configured EMBEDDING_MODEL and fails closed on any difference, so a
    `mixed:` value is deliberately going to be treated as a mismatch -- that
    is the correct outcome, not a false positive: collections disagreeing
    about their embedding model is exactly the silently-degraded state #122
    exists to catch.
    """
    models: set[str] = set()
    for name in existing_classification_collections(client):
        points, _ = client.scroll(
            collection_name=name, limit=1, with_payload=True, with_vectors=False
        )
        if not points:
            continue
        model = (points[0].payload or {}).get(EMBEDDING_MODEL_KEY)
        if model:
            models.add(model)
    if not models:
        return None
    if len(models) == 1:
        return next(iter(models))
    return "mixed:" + ",".join(sorted(models))


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def ensure_collection(client: QdrantClient, dense_size: int, classification: str) -> str:
    """Creates the collection for `classification` if it doesn't already
    exist. Returns the resolved collection name, so a caller that needs it
    for a follow-up call doesn't have to recompute it."""
    name = classification_collection_name(classification)
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config={DENSE_VECTOR: VectorParams(size=dense_size, distance=Distance.COSINE)},
            sparse_vectors_config={SPARSE_VECTOR: SparseVectorParams(modifier=Modifier.IDF)},
        )
    return name


def chunk_vector(dense: list[float], sparse: SparseVector) -> dict:
    return {DENSE_VECTOR: dense, SPARSE_VECTOR: sparse}


def upsert_chunks(client: QdrantClient, points: list[PointStruct]) -> None:
    """Routes each point to the collection for its own payload classification.
    In practice every point in one call belongs to a single document and
    therefore a single classification (ingestion-worker calls `ensure_ready`
    for that one classification immediately before this), but grouping
    defensively costs nothing and keeps this correct if that ever changes.

    Issue #271: a point without a non-empty `classification` payload used to
    default to `""`, which `classification_collection_name` slugs to the
    `__unspecified` collection -- one no retrieval path ever queries. That's
    fail-closed (nothing leaks) but silent: the chunk would simply never
    become retrievable, with no error, log, or metric. Every current writer
    stamps this field, so this is a defensive check against a future bug,
    not a live one -- consistent with this repo's fail-loud convention for a
    missing mandatory access-control field (cf. the embedding-provenance
    mismatch check, issue #122).
    """
    by_classification: dict[str, list[PointStruct]] = {}
    for point in points:
        classification = (point.payload or {}).get("classification", "")
        if not classification:
            raise ValueError(f"point {point.id!r} missing non-empty 'classification' payload field")
        by_classification.setdefault(classification, []).append(point)
    for classification, group in by_classification.items():
        client.upsert(collection_name=classification_collection_name(classification), points=group)


def _document_filter(document_id: str) -> Filter:
    return Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])


def _migrate_document_classification(
    client: QdrantClient,
    document_id: str,
    current_collection: str,
    new_classification: str,
    fields: dict,
) -> bool:
    """Issue #229: a curator's classification correction moves a document's
    chunks out of its current collection and into the target one -- the
    collection *is* the classification now, so a corrected value can't stay
    where it is the way a status/releasability/access_scope correction can.

    Safety argument (mirrors purge.py's "a partial failure always leaves the
    document less exposed, never more"): the target collection is written
    *before* the source is cleared, and every field this call is correcting
    (typically `status` alongside `classification`, from curate.py's
    approve()) is written only onto the *new* copy. The old collection's
    points are never payload-mutated, only deleted -- so if that delete fails
    partway through, the leftover old-collection duplicate still carries
    whatever status it had before this call (`pending_review`, since
    corrections only happen during approval of a still-pending document), and
    FR-26 already excludes anything that isn't `approved`. A failed delete
    therefore leaves an inert, orphaned duplicate -- worth cleaning up, never
    a spillage risk -- so it's logged rather than raised. A failure during the
    upsert into the new collection, by contrast, is raised: nothing has
    changed yet, so the caller's existing failure handling (curate.py's NFR-13
    revert) applies exactly as it would to a plain set_payload failure.

    Returns whether anything was actually moved, so `update_document_payload`
    can tell "nothing to move because there's nothing to move" apart from
    "nothing at the claimed source" -- see that function's fallback for a
    prior attempt that already completed this exact move.
    """
    if not client.collection_exists(current_collection):
        return False
    doc_filter = _document_filter(document_id)
    moved: list[PointStruct] = []
    dense_size: int | None = None
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=current_collection,
            scroll_filter=doc_filter,
            limit=_MIGRATION_PAGE_SIZE,
            with_payload=True,
            with_vectors=True,
            offset=offset,
        )
        for point in points:
            payload = {**(point.payload or {}), **fields}
            vector = point.vector
            if not isinstance(vector, dict):
                # with_vectors=True on a named-vector collection (every
                # collection this module creates) always returns a dict here;
                # anything else means the point predates the DENSE_VECTOR/
                # SPARSE_VECTOR schema and there is nothing safe to move it as.
                raise RuntimeError(
                    f"point {point.id} for document {document_id} has no named vectors to migrate"
                )
            if dense_size is None:
                dense = vector.get(DENSE_VECTOR)
                if isinstance(dense, list):
                    dense_size = len(dense)
            # dict is invariant in mypy's eyes, so `vector`'s narrower value
            # type isn't seen as assignable to PointStruct's broader one even
            # though every runtime value it can hold is -- a real false
            # positive, not a signal to widen storage-format code around it.
            moved.append(PointStruct(id=point.id, vector=vector, payload=payload))  # type: ignore[arg-type]
        if offset is None:
            break
    if not moved:
        # Nothing to move -- either the document has no chunks yet, or a
        # prior attempt at this same correction already moved them and only
        # failed on the (non-raising) delete step below. Either way there is
        # nothing more for *this* function to do; the caller decides what
        # "nothing at the source" means.
        return False
    if dense_size is None:
        raise RuntimeError(f"could not determine a dense vector size for document {document_id}")

    new_name = ensure_collection(client, dense_size=dense_size, classification=new_classification)
    client.upsert(collection_name=new_name, points=moved)

    try:
        client.delete(
            collection_name=current_collection,
            points_selector=FilterSelector(filter=doc_filter),
        )
    except Exception:
        logger.warning(
            "moved document %s to collection %s but could not clear the old copy from "
            "%s; the leftover points are not retrievable (still status=pending_review) "
            "but should be cleaned up manually",
            document_id,
            new_name,
            current_collection,
            exc_info=True,
        )
    return True


def _has_document_points(client: QdrantClient, collection_name: str, document_id: str) -> bool:
    if not client.collection_exists(collection_name):
        return False
    points, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=_document_filter(document_id),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return bool(points)


def fetch_document_chunks(
    client: QdrantClient, document_id: str, classification: str
) -> list[dict]:
    """Issue #284: read back the parsed chunk text a curator is being asked to
    approve, from the same collection retrieval would serve it from -- what a
    curator sees here is what `rag_search` will actually return, not a
    separate re-render of the original upload.

    `classification` selects the collection the same way every other
    per-document operation in this module does (issue #229) -- while a
    document is `pending_review` its chunks live in the collection matching
    its *current* tag; a correction only moves them once approval commits.

    Returns payload dicts sorted by `chunk_index` -- Qdrant's scroll makes no
    ordering guarantee, and a curator reading top to bottom should see the
    document in its original order, not scroll order.
    """
    name = classification_collection_name(classification)
    if not client.collection_exists(name):
        return []
    doc_filter = _document_filter(document_id)
    chunks: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=name,
            scroll_filter=doc_filter,
            limit=_MIGRATION_PAGE_SIZE,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        chunks.extend(point.payload or {} for point in points)
        if offset is None:
            break
    chunks.sort(key=lambda payload: payload.get("chunk_index", 0))
    return chunks


def _set_payload_if_present(
    client: QdrantClient, document_id: str, collection_name: str, fields: dict
) -> bool:
    """set_payload only if the document actually has points in this
    collection -- Qdrant's set_payload against a filter matching nothing
    succeeds silently, which is exactly the shape of the retry bug this
    guards against (see update_document_payload's fallback below)."""
    if not _has_document_points(client, collection_name, document_id):
        return False
    client.set_payload(
        collection_name=collection_name,
        payload=fields,
        points=FilterSelector(filter=_document_filter(document_id)),
    )
    return True


def update_document_payload(
    client: QdrantClient, document_id: str, current_classification: str, fields: dict
) -> None:
    """FR-13: propagate a curator's decision -- status, and any classification/
    releasability/access_scope corrections made at approval time -- to every
    chunk of a document. Qdrant is the enforcement point for FR-26, so this is
    what actually changes what's (in)visible to queries; the Postgres Document
    row (common.models) is the system of record for the curation workflow
    itself, and this keeps Qdrant's copy from going stale relative to it.
    Corrections matter here as much as status: an uncorrected Qdrant payload
    would keep enforcing the uploader's original (possibly wrong) tags even
    after a curator fixes them.

    `current_classification` is the value the document's chunks are stamped
    with *before* this call -- i.e. which collection they're currently in --
    not necessarily the corrected value. Issue #229: if `fields` corrects
    `classification` to something else, this delegates to
    `_migrate_document_classification` instead of a same-collection
    set_payload, since the collection itself encodes classification.

    Retry/idempotency note (issue #229 follow-up): `current_classification`
    is only the caller's best belief about where the chunks are -- it comes
    from a Postgres value that can be stale relative to Qdrant after a
    partial failure. NFR-13's whole design is that a curator retries a
    failed approve/reject through the *same* API call, and a prior attempt
    at this exact call may have already completed a classification move (or
    failed only on that move's non-raising cleanup delete) without the
    Postgres commit that would have advanced `current_classification`. Acting
    only on `current_classification` in that case silently no-ops: Qdrant's
    set_payload against a filter matching zero points succeeds without
    telling the caller nothing happened. So: if the claimed location has
    nothing, look for the document where a completed-but-uncommitted prior
    attempt would have put it (the correction's target, if this is itself a
    classification correction; otherwise every other classification
    collection) before giving up.
    """
    current_name = classification_collection_name(current_classification)
    new_classification = fields.get("classification")

    if new_classification and new_classification != current_classification:
        if _migrate_document_classification(
            client, document_id, current_name, new_classification, fields
        ):
            return
        # Nothing at the claimed source. A prior attempt at this same
        # correction may have already moved the chunks to the target and
        # only failed on the old collection's (non-raising) cleanup delete --
        # complete it idempotently rather than silently doing nothing.
        target_name = classification_collection_name(new_classification)
        _set_payload_if_present(client, document_id, target_name, fields)
        return

    if _set_payload_if_present(client, document_id, current_name, fields):
        return
    # Not at the claimed (non-migrating) location either -- a classification
    # correction from an earlier attempt may have moved these chunks
    # somewhere the caller doesn't know about (e.g. this call is a plain
    # status retry/reject issued after that move). Search every other
    # collection rather than silently no-op'ing.
    for name in existing_classification_collections(client):
        if name != current_name and _set_payload_if_present(client, document_id, name, fields):
            return


def set_document_status(
    client: QdrantClient, document_id: str, status: str, classification: str
) -> None:
    update_document_payload(client, document_id, classification, {"status": status})


def delete_document_chunks(client: QdrantClient, document_id: str, classification: str) -> None:
    """FR-7: remove every chunk belonging to a document that's been superseded
    by a newer version, or #123 purged. Called at the point a curator approves
    the *replacing* document (app/routes/curate.py) or an admin purges
    (common/purge.py) -- see common.models.Document.supersedes_document_id.

    `classification` is accepted for API symmetry with the other per-document
    helpers but is deliberately NOT used to scope which collection gets
    swept (issue #229 follow-up): destruction sweeps *every* existing
    classification collection, not just the caller-claimed one. A
    classification-correction migration that failed partway through its own
    cleanup delete (see `_migrate_document_classification`'s docstring) can
    leave an inert duplicate in a *different* collection than the one
    `classification` names -- inert for retrieval (FR-26 still excludes it),
    but purge's entire purpose is that the bytes are actually gone, and a
    leftover duplicate is exactly the store-level-reader exposure #229
    exists to bound. A document that never finished embedding has no
    collection at all yet, which is a no-op, not an error, same as before.

    Issue #477: the bare `QDRANT_COLLECTION` name itself (pre-#229, no
    classification suffix) is swept too, if it still exists. It never
    matches `existing_classification_collections`'s `<name>__` prefix
    filter, so on a Qdrant volume that predates the per-classification
    split it is invisible to retrieval (correct) but was also invisible to
    purge (not correct) -- a purge could report success while that
    collection's copy of the chunk text survived indefinitely. Not a
    classification collection, so not folded into
    `existing_classification_collections` itself (that helper also drives
    #122's embedding-model provenance check, which must not sample stale
    pre-migration data) -- listed here explicitly instead, alongside it,
    for the same "destruction must reach every store-level copy" reason.
    """
    del classification
    doc_filter = _document_filter(document_id)
    names = existing_classification_collections(client)
    if client.collection_exists(QDRANT_COLLECTION):
        names = [*names, QDRANT_COLLECTION]
    for name in names:
        client.delete(collection_name=name, points_selector=FilterSelector(filter=doc_filter))


def replace_document_chunks(
    client: QdrantClient, document_id: str, classification: str, points: list[PointStruct]
) -> None:
    """Issue #362: re-embed a document's chunks in place, for the re-embedding
    path #122/PR #130 shipped detection for but not a fix.

    `points` is the caller's freshly re-parsed/re-chunked/re-embedded set for
    this document, built with the same deterministic point-id scheme
    ingestion uses (``uuid5(document_id, f"chunk:{chunk_index}")``,
    processing.py) -- so upserting them overwrites the matching old points
    directly, with no window where a still-current chunk index serves a
    stale vector. New-before-old, mirroring
    `_migrate_document_classification`'s ordering: the new points land first,
    and only then are any *old* points beyond the new chunk count (the
    document re-chunked shorter than it was before) swept. A failure between
    the two steps therefore leaves at worst a few harmless extra stale
    trailing chunks -- never fewer chunks than the new, correct count, and
    never a partially-overwritten chunk.

    `classification` selects the one collection this touches (issue #229) --
    the same one the caller read the document's existing chunks from before
    rebuilding them, since a re-embed does not change what collection a
    document lives in (unlike a curator's classification correction).
    """
    name = classification_collection_name(classification)
    client.upsert(collection_name=name, points=points)
    client.delete(
        collection_name=name,
        points_selector=FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(key="document_id", match=MatchValue(value=document_id)),
                    FieldCondition(key="chunk_index", range=Range(gte=len(points))),
                ]
            )
        ),
    )
