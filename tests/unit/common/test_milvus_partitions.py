"""Issue #546 (#229 parity): the Milvus backend's per-classification
partition scoping, exercised against a fake in-memory client -- the DB fetch
paths in test_milvus_filters.py's sibling stay pure-expression; these tests
cover the partition routing, the fail-closed scoping intersection, the
classification-correction move (Qdrant-mirroring ordering and failure
semantics, both retry fallbacks), and the legacy `_default` migration.

The fake implements only the MilvusClient surface milvus_store.py calls, and
only the three filter shapes it builds (document_id equality, id-in-list,
the migration's match-all) -- a schema-faithful Milvus emulator would test
the emulator, not the store.
"""

from __future__ import annotations

import pytest
from pymilvus import MilvusException

import common.milvus_store as ms
from common.claims import UserClaims
from common.milvus_store import (
    MILVUS_COLLECTION,
    MilvusStore,
    partition_name_for,
)
from common.vector_store import ChunkPoint

try:  # SparseVector is only needed to build ChunkPoints.
    from qdrant_client.models import SparseVector
except ImportError:  # pragma: no cover
    pytest.skip("qdrant-client not installed", allow_module_level=True)


def _claims() -> UserClaims:
    return UserClaims(
        sub="user-0001",
        preferred_username="user-0001",
        groups=["analysts"],
        org="USAREUR-AF",
        rag_roles=["rag-query", "rag-clearance:SECRET", "rag-releasability:FVEY"],
    )


def _match_filter(row: dict, expr: str) -> bool:
    if expr == 'id != ""':
        return True
    if expr.startswith("document_id == "):
        value = expr.split("== ", 1)[1].strip('"')
        return row.get("document_id") == value
    if expr.startswith("id in ["):
        inner = expr[len("id in [") : -1]
        ids = [part.strip().strip('"') for part in inner.split(",") if part.strip()]
        return row.get("id") in ids
    raise AssertionError(f"fake client got an unexpected filter shape: {expr!r}")


class FakeMilvusClient:
    """Rows live per partition; every mutating call is recorded in `calls`
    so tests can assert ordering (target-write-before-source-delete)."""

    def __init__(self) -> None:
        self.partitions: dict[str, dict[str, dict]] = {"_default": {}}
        self.calls: list[tuple] = []
        self.fail_delete_partitions: set[str] = set()
        self.fail_upsert_partitions: set[str] = set()

    # -- collection/partition surface -------------------------------------
    def has_collection(self, collection_name: str) -> bool:
        return True

    def list_partitions(self, collection_name: str) -> list[str]:
        return list(self.partitions)

    def has_partition(self, collection_name: str, partition_name: str) -> bool:
        return partition_name in self.partitions

    def create_partition(self, collection_name: str, partition_name: str) -> None:
        self.calls.append(("create_partition", partition_name))
        self.partitions.setdefault(partition_name, {})

    # -- data surface ------------------------------------------------------
    def upsert(self, collection_name: str, data: list[dict], partition_name: str | None = None):
        target = partition_name or "_default"
        self.calls.append(("upsert", target, [row["id"] for row in data]))
        if target in self.fail_upsert_partitions:
            raise MilvusException(message=f"upsert into {target} failed")
        bucket = self.partitions.setdefault(target, {})
        for row in data:
            bucket[row["id"]] = dict(row)

    def query(
        self,
        collection_name: str,
        filter: str,
        output_fields: list[str],
        limit: int,
        partition_names: list[str] | None = None,
    ) -> list[dict]:
        names = partition_names or list(self.partitions)
        rows = []
        for name in names:
            for row in self.partitions.get(name, {}).values():
                if _match_filter(row, filter):
                    rows.append(dict(row))
        return rows[:limit]

    def delete(
        self,
        collection_name: str,
        filter: str,
        partition_name: str | None = None,
    ) -> None:
        target = partition_name or "_default"
        self.calls.append(("delete", target, filter))
        if target in self.fail_delete_partitions:
            raise MilvusException(message=f"delete from {target} failed")
        bucket = self.partitions.get(target, {})
        for row_id in [rid for rid, row in bucket.items() if _match_filter(row, filter)]:
            del bucket[row_id]

    def hybrid_search(
        self, collection_name: str, reqs, ranker, limit, output_fields, partition_names
    ):
        self.calls.append(("hybrid_search", tuple(partition_names)))
        rows = []
        for name in partition_names:
            rows.extend(self.partitions.get(name, {}).values())
        return [
            [
                {"id": r["id"], "distance": 1.0, "entity": {"payload": r["payload"]}}
                for r in rows[:limit]
            ]
        ]


@pytest.fixture()
def fake(monkeypatch) -> FakeMilvusClient:
    client = FakeMilvusClient()
    monkeypatch.setattr(ms, "_client", lambda: client)
    # #547 review: the migration-probe gate is per-process state; reset it so
    # test outcomes can't depend on execution order.
    monkeypatch.setitem(ms._default_migration_state, "confirmed_empty", False)
    return client


def _point(doc: str, idx: int, classification: str, status: str = "pending_review") -> ChunkPoint:
    return ChunkPoint(
        id=f"{doc}-{idx}",
        dense=[0.1, 0.2],
        sparse=SparseVector(indices=[1], values=[1.0]),
        payload={
            "document_id": doc,
            "chunk_index": idx,
            "classification": classification,
            "status": status,
            "releasability": ["NONE"],
            "access_scope": ["ALL_AUTHENTICATED"],
            "text": f"chunk {idx}",
        },
    )


class TestPartitionNaming:
    def test_slug_matches_qdrant_semantics(self) -> None:
        assert partition_name_for("TOP SECRET") == "cls_top_secret"
        assert partition_name_for("CUI") == "cls_cui"
        assert partition_name_for("  UNCLASSIFIED  ") == "cls_unclassified"

    def test_symbol_only_value_gets_fixed_fallback(self) -> None:
        assert partition_name_for("///") == "cls_unspecified"
        assert partition_name_for("") == "cls_unspecified"


class TestUpsertRouting:
    def test_rows_land_in_their_classification_partition(self, fake: FakeMilvusClient) -> None:
        MilvusStore().upsert(
            [_point("d1", 0, "CUI"), _point("d1", 1, "CUI"), _point("d2", 0, "SECRET")]
        )
        assert set(fake.partitions["cls_cui"]) == {"d1-0", "d1-1"}
        assert set(fake.partitions["cls_secret"]) == {"d2-0"}
        assert fake.partitions["_default"] == {}


class TestHybridQueryScoping:
    def test_only_allowed_existing_partitions_are_searched(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI"), _point("d2", 0, "SECRET")])
        claims = _claims()
        store.hybrid_query(
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[1.0]),
            claims=claims,
            allowed_classifications=["UNCLASSIFIED", "CUI"],
            limit=5,
        )
        searched = [c for c in fake.calls if c[0] == "hybrid_search"]
        # UNCLASSIFIED has no partition (never ingested) -> silently skipped;
        # SECRET exists but is not allowed -> never in the list.
        assert searched == [("hybrid_search", ("cls_cui",))]

    def test_no_matching_partitions_is_empty_not_an_error(self, fake: FakeMilvusClient) -> None:
        result = MilvusStore().hybrid_query(
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[1.0]),
            claims=_claims(),
            allowed_classifications=["UNCLASSIFIED"],
            limit=5,
        )
        assert result == []
        assert not [c for c in fake.calls if c[0] == "hybrid_search"]


class TestUpdateDocumentPayload:
    def test_non_migrating_update_patches_in_claimed_partition(self, fake) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI")])
        store.update_document_payload("d1", "CUI", {"status": "approved"})
        row = fake.partitions["cls_cui"]["d1-0"]
        assert row["status"] == "approved"
        assert row["payload"]["status"] == "approved"

    def test_claimed_location_empty_falls_back_to_other_partitions(self, fake) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "SECRET")])
        # Caller believes CUI (stale Postgres value after a partial failure).
        store._ensure_partition("CUI")
        store.update_document_payload("d1", "CUI", {"status": "approved"})
        assert fake.partitions["cls_secret"]["d1-0"]["status"] == "approved"

    def test_classification_correction_moves_rows(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI"), _point("d1", 1, "CUI")])
        store.update_document_payload(
            "d1", "CUI", {"status": "approved", "classification": "SECRET"}
        )
        assert "d1-0" not in fake.partitions["cls_cui"]
        moved = fake.partitions["cls_secret"]["d1-0"]
        assert moved["classification"] == "SECRET"
        assert moved["status"] == "approved"
        assert moved["payload"]["classification"] == "SECRET"

    def test_move_writes_target_before_deleting_source(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI")])
        fake.calls.clear()
        store.update_document_payload("d1", "CUI", {"classification": "SECRET"})
        kinds = [c[0] for c in fake.calls if c[0] in ("upsert", "delete")]
        assert kinds == ["upsert", "delete"]

    def test_source_cleanup_failure_is_logged_not_raised(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI")])
        fake.fail_delete_partitions.add("cls_cui")
        # Must not raise: the leftover source copy is never `approved`.
        store.update_document_payload("d1", "CUI", {"classification": "SECRET"})
        assert fake.partitions["cls_secret"]["d1-0"]["classification"] == "SECRET"
        # Source rows remain (delete failed) but were never payload-mutated.
        assert fake.partitions["cls_cui"]["d1-0"]["classification"] == "CUI"

    def test_target_upsert_failure_raises_with_source_untouched(self, fake) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI")])
        fake.fail_upsert_partitions.add("cls_secret")
        fake.partitions["cls_secret"] = {}
        with pytest.raises(MilvusException):
            store.update_document_payload("d1", "CUI", {"classification": "SECRET"})
        assert fake.partitions["cls_cui"]["d1-0"]["classification"] == "CUI"

    def test_retry_after_completed_move_patches_target(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        # Prior attempt already moved the rows; claimed source is empty.
        store.upsert([_point("d1", 0, "SECRET")])
        store._ensure_partition("CUI")
        store.update_document_payload(
            "d1", "CUI", {"status": "approved", "classification": "SECRET"}
        )
        assert fake.partitions["cls_secret"]["d1-0"]["status"] == "approved"


class TestScopedLifecycle:
    def test_delete_sweeps_every_partition_for_the_document(self, fake: FakeMilvusClient) -> None:
        # #547 review: destruction mirrors qdrant_store -- the classification
        # argument must NOT scope the sweep. Every partition holding rows for
        # this document_id is cleared, other documents' rows are untouched.
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI"), _point("d1", 0, "SECRET"), _point("d2", 0, "CUI")])
        store.delete_document_chunks("d1", "CUI")
        assert "d1-0" not in fake.partitions["cls_cui"]
        assert "d1-0" not in fake.partitions["cls_secret"]
        assert "d2-0" in fake.partitions["cls_cui"]

    def test_purge_reaches_the_leftover_of_a_failed_cleanup_move(self, fake) -> None:
        # #547 review reproduction: a classification-correction move whose
        # cleanup delete failed (documented logged-not-raised path) leaves an
        # inert copy in the OLD partition. A later purge/supersession delete
        # is called with the document's CURRENT classification and must still
        # erase that leftover -- purge's entire point is that the bytes are
        # actually gone.
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI")])
        fake.fail_delete_partitions.add("cls_cui")
        store.update_document_payload(
            "d1", "CUI", {"status": "approved", "classification": "SECRET"}
        )
        assert "d1-0" in fake.partitions["cls_cui"]  # the inert leftover
        fake.fail_delete_partitions.clear()  # transient failure has passed by purge time

        store.delete_document_chunks("d1", "SECRET")

        assert "d1-0" not in fake.partitions["cls_cui"]
        assert "d1-0" not in fake.partitions["cls_secret"]

    def test_delete_sweeps_the_legacy_default_partition_too(self, fake: FakeMilvusClient) -> None:
        # Pre-#546 rows live in `_default` until ensure_ready migrates them;
        # a purge that lands before that migration must reach them anyway.
        fake.upsert(
            MILVUS_COLLECTION,
            [
                {
                    "id": "old-0",
                    "dense": [0.1, 0.2],
                    "sparse": {1: 1.0},
                    "document_id": "old",
                    "status": "pending_review",
                    "classification": "CUI",
                    "releasability": ["NONE"],
                    "access_scope": ["ALL_AUTHENTICATED"],
                    "payload": {"document_id": "old", "chunk_index": 0},
                }
            ],
        )
        MilvusStore().delete_document_chunks("old", "CUI")
        assert fake.partitions["_default"] == {}

    def test_fetch_missing_partition_is_empty(self, fake: FakeMilvusClient) -> None:
        assert MilvusStore().fetch_document_chunks("d1", "NEVER_INGESTED") == []

    def test_replace_sweeps_stale_chunks_within_partition(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        store.upsert([_point("d1", 0, "CUI"), _point("d1", 1, "CUI"), _point("d1", 2, "CUI")])
        store.replace_document_chunks("d1", "CUI", [_point("d1", 0, "CUI"), _point("d1", 1, "CUI")])
        assert set(fake.partitions["cls_cui"]) == {"d1-0", "d1-1"}


class TestLegacyDefaultMigration:
    def test_default_rows_route_to_their_level_partition(self, fake: FakeMilvusClient) -> None:
        store = MilvusStore()
        # Simulate pre-#546 rows: written with no partition routing.
        fake.upsert(
            MILVUS_COLLECTION,
            [
                {
                    "id": "old-0",
                    "dense": [0.1],
                    "sparse": {1: 1.0},
                    "document_id": "old",
                    "status": "approved",
                    "classification": "CUI",
                    "releasability": ["NONE"],
                    "access_scope": ["ALL_AUTHENTICATED"],
                    "payload": {"document_id": "old", "chunk_index": 0, "classification": "CUI"},
                }
            ],
        )
        store.ensure_ready(dense_size=2, classification="CUI")
        assert fake.partitions["_default"] == {}
        assert "old-0" in fake.partitions["cls_cui"]

    def test_empty_default_is_a_noop(self, fake: FakeMilvusClient) -> None:
        MilvusStore().ensure_ready(dense_size=2, classification="CUI")
        deletes = [c for c in fake.calls if c[0] == "delete"]
        assert deletes == []

    def test_probe_stops_after_default_confirmed_empty(self, fake, monkeypatch) -> None:
        # #547 review (minor): ensure_ready runs once per ingested document
        # forever, but the `_default` probe is a one-time healing step -- once
        # confirmed empty it must not cost a Milvus query on every ingest for
        # the rest of the process's life.
        probes = []
        original_query = fake.query

        def counting_query(*args, **kwargs):
            if kwargs.get("partition_names") == [ms._DEFAULT_PARTITION]:
                probes.append(1)
            return original_query(*args, **kwargs)

        monkeypatch.setattr(fake, "query", counting_query)
        store = MilvusStore()
        store.ensure_ready(dense_size=2, classification="CUI")
        store.ensure_ready(dense_size=2, classification="CUI")
        store.ensure_ready(dense_size=2, classification="SECRET")

        assert len(probes) == 1

    def test_rows_appearing_later_heal_after_process_restart(self, fake, monkeypatch) -> None:
        # The gate is per-process by design: simulate a restart by resetting
        # the flag and confirm late-arriving `_default` rows still migrate.
        store = MilvusStore()
        store.ensure_ready(dense_size=2, classification="CUI")
        fake.upsert(
            MILVUS_COLLECTION,
            [
                {
                    "id": "late-0",
                    "dense": [0.1, 0.2],
                    "sparse": {1: 1.0},
                    "document_id": "late",
                    "status": "pending_review",
                    "classification": "CUI",
                    "releasability": ["NONE"],
                    "access_scope": ["ALL_AUTHENTICATED"],
                    "payload": {"document_id": "late", "chunk_index": 0},
                }
            ],
        )
        monkeypatch.setitem(ms._default_migration_state, "confirmed_empty", False)  # restart
        store.ensure_ready(dense_size=2, classification="CUI")

        assert fake.partitions["_default"] == {}
        assert "late-0" in fake.partitions["cls_cui"]
