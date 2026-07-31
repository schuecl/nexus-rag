"""Issue #229: one Qdrant collection per Classification level instead of one
shared collection for the whole corpus.

Uses a small in-memory fake standing in for QdrantClient -- qdrant_store.py
is excluded from the coverage gate (.coveragerc: needs a live Qdrant to
exercise for real) but its collection-naming, routing, and migration logic is
pure enough to pin down against a fake, the same technique
test_embedding_provenance.py already uses for collection_embedding_model.
"""

from __future__ import annotations

import pytest
from qdrant_client.models import PointStruct, SparseVector

from common import qdrant_store
from common.qdrant_store import (
    DENSE_VECTOR,
    classification_collection_name,
    delete_document_chunks,
    ensure_collection,
    existing_classification_collections,
    update_document_payload,
    upsert_chunks,
)


class _FakePoint:
    def __init__(self, id_, vector, payload):
        self.id = id_
        self.vector = vector
        self.payload = payload


class _Collection:
    def __init__(self, name):
        self.name = name


class _Collections:
    def __init__(self, names):
        self.collections = [_Collection(n) for n in names]


class FakeQdrantClient:
    """Interprets exactly the Filter/FilterSelector shapes qdrant_store.py
    builds (a single `document_id == value` clause) -- not a general Qdrant
    simulator."""

    def __init__(self):
        self.collections: dict[str, dict[str, _FakePoint]] = {}
        self.created: list[tuple[str, int]] = []
        self.delete_failures: set[str] = set()

    def collection_exists(self, name):
        return name in self.collections

    def create_collection(self, *, collection_name, vectors_config, sparse_vectors_config):
        del sparse_vectors_config
        self.collections.setdefault(collection_name, {})
        self.created.append((collection_name, vectors_config[DENSE_VECTOR].size))

    def get_collections(self):
        return _Collections(list(self.collections.keys()))

    def upsert(self, *, collection_name, points):
        bucket = self.collections.setdefault(collection_name, {})
        for p in points:
            bucket[p.id] = _FakePoint(p.id, p.vector, dict(p.payload or {}))

    def scroll(
        self,
        *,
        collection_name,
        scroll_filter=None,
        limit=100,
        with_payload=True,
        with_vectors=False,
        offset=None,
    ):
        del with_payload, with_vectors
        bucket = self.collections.get(collection_name, {})
        items = list(bucket.values())
        if scroll_filter is not None:
            doc_id = scroll_filter.must[0].match.value
            items = [p for p in items if p.payload.get("document_id") == doc_id]
        start = offset or 0
        page = items[start : start + limit]
        next_offset = start + limit if start + limit < len(items) else None
        return page, next_offset

    def set_payload(self, *, collection_name, payload, points):
        bucket = self.collections.get(collection_name, {})
        doc_id = points.filter.must[0].match.value
        for p in bucket.values():
            if p.payload.get("document_id") == doc_id:
                p.payload.update(payload)

    def delete(self, *, collection_name, points_selector):
        if collection_name in self.delete_failures:
            raise RuntimeError("qdrant delete unreachable")
        bucket = self.collections.get(collection_name, {})
        doc_id = points_selector.filter.must[0].match.value
        for pid in [pid for pid, p in bucket.items() if p.payload.get("document_id") == doc_id]:
            del bucket[pid]


def _point(id_, document_id, classification, extra=None) -> PointStruct:
    payload = {"document_id": document_id, "classification": classification, **(extra or {})}
    return PointStruct(
        id=id_,
        vector={DENSE_VECTOR: [0.1, 0.2], "bm25": SparseVector(indices=[1], values=[0.5])},
        payload=payload,
    )


class TestCollectionNaming:
    def test_value_is_slugified(self):
        assert classification_collection_name("CUI") == f"{qdrant_store.QDRANT_COLLECTION}__cui"

    def test_spaces_become_underscores(self):
        name = classification_collection_name("TOP SECRET")
        assert name == f"{qdrant_store.QDRANT_COLLECTION}__top_secret"

    def test_symbols_are_replaced(self):
        name = classification_collection_name("Weird/Value!")
        assert name == f"{qdrant_store.QDRANT_COLLECTION}__weird_value"

    def test_all_symbol_value_falls_back_rather_than_colliding_with_the_prefix(self):
        name = classification_collection_name("!!!")
        assert name == f"{qdrant_store.QDRANT_COLLECTION}__unspecified"

    def test_distinct_values_never_collide(self):
        names = {classification_collection_name(v) for v in ("UNCLASSIFIED", "CUI", "SECRET")}
        assert len(names) == 3


class TestExistingClassificationCollections:
    def test_only_prefixed_collections_are_returned(self):
        client = FakeQdrantClient()
        ensure_collection(client, dense_size=2, classification="CUI")
        client.collections["some_unrelated_collection"] = {}

        found = existing_classification_collections(client)

        assert found == [classification_collection_name("CUI")]

    def test_empty_when_nothing_ingested_yet(self):
        assert existing_classification_collections(FakeQdrantClient()) == []


class TestEnsureCollectionIsIdempotent:
    def test_second_call_does_not_recreate(self):
        client = FakeQdrantClient()
        ensure_collection(client, dense_size=2, classification="CUI")
        ensure_collection(client, dense_size=2, classification="CUI")

        assert client.created == [(classification_collection_name("CUI"), 2)]


class TestUpsertRoutesByClassification:
    def test_points_land_in_their_own_classification_collection(self):
        client = FakeQdrantClient()
        points = [
            _point("p1", "doc-1", "CUI"),
            _point("p2", "doc-1", "CUI"),
            _point("p3", "doc-2", "SECRET"),
        ]

        upsert_chunks(client, points)

        cui = client.collections[classification_collection_name("CUI")]
        secret = client.collections[classification_collection_name("SECRET")]
        assert set(cui) == {"p1", "p2"}
        assert set(secret) == {"p3"}


class TestUpsertRejectsMissingClassification:
    """Issue #271: a point without a non-empty `classification` payload used
    to default to `""`, silently routing to the `__unspecified` collection --
    one no retrieval path ever queries -- instead of failing loudly."""

    def test_missing_classification_key_raises(self):
        client = FakeQdrantClient()
        point = PointStruct(
            id="p1",
            vector={DENSE_VECTOR: [0.1, 0.2], "bm25": SparseVector(indices=[1], values=[0.5])},
            payload={"document_id": "doc-1"},
        )

        with pytest.raises(ValueError, match="classification"):
            upsert_chunks(client, [point])

        assert client.collections == {}

    def test_empty_string_classification_raises(self):
        client = FakeQdrantClient()

        with pytest.raises(ValueError, match="classification"):
            upsert_chunks(client, [_point("p1", "doc-1", "")])

        assert classification_collection_name("") not in client.collections

    def test_a_valid_point_earlier_in_the_batch_is_not_left_partially_written(self):
        """Grouping happens before any client.upsert call, so a bad point
        anywhere in the batch must not leave a good point from the same call
        already written to Qdrant."""
        client = FakeQdrantClient()
        points = [_point("p1", "doc-1", "CUI"), _point("p2", "doc-2", "")]

        with pytest.raises(ValueError, match="classification"):
            upsert_chunks(client, points)

        assert client.collections == {}


class TestDeleteDocumentChunks:
    def test_absent_collection_is_a_no_op(self):
        # A document that was superseded/purged before it ever finished
        # embedding may have no collection at all yet.
        delete_document_chunks(FakeQdrantClient(), "doc-1", "CUI")  # must not raise

    def test_only_the_named_document_is_removed(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI"), _point("p2", "doc-2", "CUI")])

        delete_document_chunks(client, "doc-1", "CUI")

        remaining = client.collections[classification_collection_name("CUI")]
        assert set(remaining) == {"p2"}

    def test_sweeps_every_classification_collection_not_just_the_named_one(self):
        """Review followup (PR #267): a classification-migration that failed
        partway through its own cleanup delete can leave an orphaned
        duplicate in a *different* collection than the one the caller names.
        Purge's whole point is that the bytes are actually gone, so
        destruction must not be scoped to a single, possibly-stale
        classification."""
        client = FakeQdrantClient()
        # Simulate exactly that leftover: doc-1's chunks exist in both CUI
        # (the pre-migration collection, cleanup delete never succeeded) and
        # SECRET (the post-migration collection).
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])
        upsert_chunks(client, [_point("p2", "doc-1", "SECRET")])

        # The caller only knows/claims one of them (e.g. Postgres's current
        # classification value) -- destruction must not stop there.
        delete_document_chunks(client, "doc-1", "SECRET")

        assert client.collections[classification_collection_name("CUI")] == {}
        assert client.collections[classification_collection_name("SECRET")] == {}

    def test_other_documents_survive_a_sweep(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])
        upsert_chunks(client, [_point("p2", "doc-2", "SECRET")])

        delete_document_chunks(client, "doc-1", "CUI")

        assert "p2" in client.collections[classification_collection_name("SECRET")]


class TestUpdateDocumentPayloadWithoutClassificationChange:
    def test_status_only_correction_writes_in_place(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])

        update_document_payload(client, "doc-1", "CUI", {"status": "approved"})

        point = client.collections[classification_collection_name("CUI")]["p1"]
        assert point.payload["status"] == "approved"
        assert point.payload["classification"] == "CUI"


class TestUpdateDocumentPayloadMigratesOnClassificationChange:
    def test_points_move_to_the_new_collection(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI"), _point("p2", "doc-1", "CUI")])

        update_document_payload(
            client, "doc-1", "CUI", {"status": "approved", "classification": "SECRET"}
        )

        old = client.collections.get(classification_collection_name("CUI"), {})
        new = client.collections[classification_collection_name("SECRET")]
        assert old == {}
        assert set(new) == {"p1", "p2"}

    def test_moved_points_carry_the_correction_and_keep_their_vectors(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI", extra={"heading": "H1"})])

        update_document_payload(
            client, "doc-1", "CUI", {"status": "approved", "classification": "SECRET"}
        )

        moved = client.collections[classification_collection_name("SECRET")]["p1"]
        assert moved.payload["classification"] == "SECRET"
        assert moved.payload["status"] == "approved"
        assert moved.payload["heading"] == "H1"  # untouched fields survive the move
        assert moved.vector[DENSE_VECTOR] == [0.1, 0.2]

    def test_other_documents_in_the_old_collection_are_untouched(self):
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI"), _point("p2", "doc-2", "CUI")])

        update_document_payload(client, "doc-1", "CUI", {"classification": "SECRET"})

        old = client.collections[classification_collection_name("CUI")]
        assert set(old) == {"p2"}

    def test_nothing_to_move_is_a_no_op_and_does_not_create_the_target(self):
        client = FakeQdrantClient()
        # doc-1 has no points anywhere at all -- not even at the correction's
        # target -- so there's genuinely nothing to do.

        update_document_payload(client, "doc-1", "CUI", {"classification": "SECRET"})

        assert classification_collection_name("SECRET") not in client.collections


class TestUpdateDocumentPayloadRetryAfterPartialMigration:
    """Review followup (PR #267): NFR-13 lets a curator retry a failed
    approve/reject through the *same* API call. If a prior attempt already
    moved a document's chunks to the target collection (and only failed on
    that move's non-raising old-collection cleanup, or failed the Postgres
    commit after a fully successful move+revert), `current_classification`
    on retry is the caller's stale belief -- the source is empty, but the
    correction still needs to land somewhere. Silently no-op'ing there (the
    pre-fix behavior) leaves Postgres saying `approved` while the chunks stay
    `pending_review` forever."""

    def test_retrying_the_same_correction_completes_it_at_the_target(self):
        client = FakeQdrantClient()
        # Simulates: first attempt's migration fully succeeded (points now
        # live in SECRET, source CUI already cleared), but the NFR-13 revert
        # reset status back to pending_review after a failed Postgres commit.
        upsert_chunks(client, [_point("p1", "doc-1", "SECRET", extra={"status": "pending_review"})])

        # Retry: caller still believes the source is CUI (Postgres rolled
        # back), resubmitting the identical correction.
        update_document_payload(
            client, "doc-1", "CUI", {"status": "approved", "classification": "SECRET"}
        )

        point = client.collections[classification_collection_name("SECRET")]["p1"]
        assert point.payload["status"] == "approved"
        assert classification_collection_name("CUI") not in client.collections

    def test_retrying_without_the_correction_still_finds_the_moved_chunks(self):
        """A plain status retry (or a reject) issued after an earlier
        classification move the caller doesn't know about -- no
        `classification` field in `fields` this time, so there's no
        target to check directly; every other collection must be searched."""
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "SECRET", extra={"status": "pending_review"})])

        update_document_payload(client, "doc-1", "CUI", {"status": "approved"})

        point = client.collections[classification_collection_name("SECRET")]["p1"]
        assert point.payload["status"] == "approved"

    def test_a_genuinely_missing_document_is_still_a_no_op(self):
        """The fallback search must not paper over a document that really
        doesn't exist anywhere -- only complete a move that actually
        happened."""
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("other", "doc-2", "SECRET")])

        update_document_payload(client, "doc-1", "CUI", {"status": "approved"})  # must not raise

        # doc-2's point is untouched -- fields were never applied to it.
        untouched = client.collections[classification_collection_name("SECRET")]["other"]
        assert untouched.payload.get("status") != "approved"

    def test_a_failed_delete_of_the_old_copy_does_not_raise(self):
        """Safety argument (see qdrant_store._migrate_document_classification's
        docstring): the old copy's status is never mutated, only deleted, so a
        failed delete leaves an inert -- not spillage-risking -- duplicate.
        That's a cleanup job, not a caller-visible failure."""
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])
        client.delete_failures.add(classification_collection_name("CUI"))

        update_document_payload(
            client, "doc-1", "CUI", {"status": "approved", "classification": "SECRET"}
        )  # must not raise

        assert "p1" in client.collections[classification_collection_name("SECRET")]
        assert "p1" in client.collections[classification_collection_name("CUI")]  # leftover

    def test_a_failed_upsert_into_the_new_collection_does_raise(self):
        """Nothing has changed yet in this case, so the caller's existing
        NFR-13 failure handling applies exactly as it would to a plain
        set_payload failure."""
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])

        def _broken_upsert(*, collection_name, points):
            raise RuntimeError("qdrant unreachable")

        client.upsert = _broken_upsert  # type: ignore[method-assign]

        try:
            update_document_payload(client, "doc-1", "CUI", {"classification": "SECRET"})
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the upsert failure to propagate")

    def test_same_value_correction_is_not_treated_as_a_move(self):
        """Re-submitting the same classification a document already has must
        not trigger the migration path."""
        client = FakeQdrantClient()
        upsert_chunks(client, [_point("p1", "doc-1", "CUI")])

        update_document_payload(client, "doc-1", "CUI", {"classification": "CUI", "status": "x"})

        point = client.collections[classification_collection_name("CUI")]["p1"]
        assert point.payload["status"] == "x"
