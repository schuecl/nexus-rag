"""Issue #424: per-identity upload quota (NFR-17 residual).

NFR-17 leaves request-rate limiting to the ingress (#209, decided) and bounds a
single request with `MAX_UPLOAD_BYTES`. Neither bounds how much one identity
submits in total, and rate limiting structurally cannot: one compliant 50MB file
every few seconds stays under any sane rate limit while filling a finite
air-gapped object store and growing the curator queue without limit.

These tests pin the two caps, the properties that make them safe to enforce
(nothing durable is written on a refusal; a batch counts every file), and the
resolution rules that decide what happens to a deployment upgrading into this
release.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, UploadFile
from sqlmodel import Session, SQLModel, create_engine, select

from app import quota
from app.routes import upload
from common.claims import UserClaims
from common.models import AuditLogEntry, ClassificationLevel, Document, PortalSettings

UPLOADER = UserClaims(
    sub="uploader-sub",
    preferred_username="alice-ingest",
    org="USAREUR-AF",
    rag_roles=["rag-ingest", "rag-clearance:SECRET", "rag-releasability:NONE"],
)
OTHER = UserClaims(
    sub="other-sub",
    preferred_username="bob-ingest",
    org="USAREUR-AF",
    rag_roles=["rag-ingest", "rag-clearance:SECRET", "rag-releasability:NONE"],
)

MB = 1024 * 1024
GIB = 1024 * 1024 * 1024


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _doc(
    user: UserClaims = UPLOADER,
    *,
    status: str = "pending_review",
    content_bytes: int | None = 1 * MB,
    created_at: datetime | None = None,
) -> Document:
    return Document(
        filename="f.pdf",
        uploader_sub=user.sub,
        uploader_username=user.preferred_username,
        owner_org=user.org or "unknown",
        classification="UNCLASSIFIED",
        releasability=["NONE"],
        access_scope=["ALL_AUTHENTICATED"],
        source_originator="Ops",
        doc_type="SOP",
        status=status,
        content_bytes=content_bytes,
        **({"created_at": created_at} if created_at else {}),
    )


def _set_limits(session: Session, *, inflight: int | None, gib: float | None) -> None:
    settings = session.get(PortalSettings, 1) or PortalSettings(id=1)
    settings.upload_quota_max_inflight = inflight
    settings.upload_quota_max_bytes_24h = None if gib is None else int(gib * GIB)
    session.add(settings)
    session.commit()


class TestLimitResolution:
    def test_no_settings_row_resolves_to_the_defaults_not_unlimited(self, session: Session) -> None:
        """A deployment upgrading into this release must gain the bound.

        Resolving a missing row to "unlimited" would leave NFR-17's gap open
        until somebody remembered to configure it, which is how this stayed open
        for as long as it did.
        """
        limits = quota.resolve_limits(session)
        assert limits.max_inflight == quota.DEFAULT_MAX_INFLIGHT
        assert limits.max_bytes_24h == quota.DEFAULT_MAX_BYTES_24H

    def test_null_columns_resolve_to_the_defaults(self, session: Session) -> None:
        _set_limits(session, inflight=None, gib=None)
        limits = quota.resolve_limits(session)
        assert limits.max_inflight == quota.DEFAULT_MAX_INFLIGHT
        assert limits.max_bytes_24h == quota.DEFAULT_MAX_BYTES_24H

    def test_configured_values_win(self, session: Session) -> None:
        _set_limits(session, inflight=3, gib=1)
        limits = quota.resolve_limits(session)
        assert (limits.max_inflight, limits.max_bytes_24h) == (3, GIB)

    def test_zero_means_unlimited(self, session: Session) -> None:
        """Explicit opt-out, for a deployment bounding this at the platform layer."""
        _set_limits(session, inflight=0, gib=0)
        for _ in range(5):
            session.add(_doc(content_bytes=10 * GIB))
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 10 * GIB, filename="f.pdf")


class TestInFlightCap:
    def test_under_the_cap_is_accepted(self, session: Session) -> None:
        _set_limits(session, inflight=3, gib=0)
        session.add(_doc())
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")

    def test_the_incoming_document_counts_against_the_cap(self, session: Session) -> None:
        """At the cap, the *next* one is refused -- the check is `used + 1 > limit`,
        not `used > limit`, or a cap of N would admit N+1."""
        _set_limits(session, inflight=2, gib=0)
        session.add(_doc())
        session.add(_doc())
        session.commit()
        with pytest.raises(HTTPException) as exc:
            quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")
        assert exc.value.status_code == 429

    @pytest.mark.parametrize("status", ["queued", "processing", "embedded", "pending_review"])
    def test_every_in_flight_status_counts(self, session: Session, status: str) -> None:
        _set_limits(session, inflight=1, gib=0)
        session.add(_doc(status=status))
        session.commit()
        with pytest.raises(HTTPException):
            quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")

    @pytest.mark.parametrize("status", ["approved", "rejected", "superseded", "failed"])
    def test_settled_documents_do_not_count(self, session: Session, status: str) -> None:
        """The cap bounds review/worker load, which a settled document no longer
        consumes -- so curating frees capacity and a bulk uploader is throttled
        rather than permanently locked out. Their *bytes* are still counted by the
        storage cap, so this is not a loophole."""
        _set_limits(session, inflight=1, gib=0)
        session.add(_doc(status=status))
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")

    def test_another_identitys_documents_do_not_count(self, session: Session) -> None:
        _set_limits(session, inflight=1, gib=0)
        session.add(_doc(OTHER))
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")


class TestByteCap:
    def test_under_the_cap_is_accepted(self, session: Session) -> None:
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=500 * MB))
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 100 * MB, filename="f.pdf")

    def test_the_incoming_size_is_included(self, session: Session) -> None:
        """600MB used + 500MB incoming exceeds 1GiB, even though 600MB alone does not."""
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=600 * MB))
        session.commit()
        with pytest.raises(HTTPException) as exc:
            quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")
        assert exc.value.status_code == 429

    def test_bytes_outside_the_window_do_not_count(self, session: Session) -> None:
        old = datetime.now(UTC) - timedelta(hours=25)
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=900 * MB, created_at=old))
        session.commit()
        quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")

    def test_bytes_inside_the_window_do_count(self, session: Session) -> None:
        recent = datetime.now(UTC) - timedelta(hours=23)
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=900 * MB, created_at=recent))
        session.commit()
        with pytest.raises(HTTPException):
            quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")

    def test_settled_documents_still_consume_the_byte_quota(self, session: Session) -> None:
        """Curation does not free storage: NFR-12 keeps the original, and purge is
        a separate deliberate act. Counting only in-flight bytes would let a
        reject-and-resubmit loop consume storage without moving the total -- the
        exact abuse pattern this cap exists to stop.
        """
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(status="rejected", content_bytes=900 * MB))
        session.commit()
        with pytest.raises(HTTPException):
            quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")

    def test_rows_predating_the_column_count_as_zero(self, session: Session) -> None:
        """`content_bytes` is null on pre-upgrade rows and SUM skips nulls. That
        under-counts historical usage rather than locking someone out over data
        the system cannot see -- the safer direction for a control returning 429.
        """
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=None))
        session.commit()
        assert quota.bytes_in_window(session, UPLOADER.sub) == 0
        quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")


class TestDenialIsRecorded:
    def test_a_denial_writes_a_committed_audit_entry(self, session: Session) -> None:
        """The 429 unwinds the request, so an uncommitted entry would be rolled
        back and the refusal would leave no trace at all.

        The rollback below is what makes this a real assertion: querying the same
        session straight after the call finds the row whether or not it was ever
        committed, because it is still pending in that session. Rolling back first
        discards anything uncommitted, so a surviving row proves durability. (An
        earlier version of this test omitted the rollback and passed happily with
        the commit deleted from quota.py.)
        """
        _set_limits(session, inflight=1, gib=0)
        session.add(_doc())
        session.commit()
        with pytest.raises(HTTPException):
            quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="secret-plans.pdf")
        session.rollback()
        entries = session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.submit.quota_denied")
        ).all()
        assert len(entries) == 1
        assert entries[0].actor_sub == UPLOADER.sub
        assert entries[0].detail["filename"] == "secret-plans.pdf"
        assert entries[0].detail["reason"] == "in_flight"

    def test_the_byte_denial_records_the_numbers_that_caused_it(self, session: Session) -> None:
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=900 * MB))
        session.commit()
        with pytest.raises(HTTPException):
            quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")
        session.rollback()
        entry = session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.submit.quota_denied")
        ).one()
        assert entry.detail["reason"] == "bytes_24h"
        assert entry.detail["used_bytes"] == 900 * MB
        assert entry.detail["incoming_bytes"] == 500 * MB
        assert entry.detail["limit_bytes"] == GIB

    def test_an_accepted_upload_writes_no_denial_entry(self, session: Session) -> None:
        _set_limits(session, inflight=5, gib=1)
        quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")
        assert session.exec(select(AuditLogEntry)).all() == []

    def test_the_message_says_which_cap_and_how_to_recover(self, session: Session) -> None:
        """A 429 that does not say why is indistinguishable from a rate limit the
        submitter can retry through -- these caps clear on curation and on the
        window rolling, which is different advice in each case."""
        _set_limits(session, inflight=1, gib=0)
        session.add(_doc())
        session.commit()
        with pytest.raises(HTTPException) as exc:
            quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")
        assert "review" in exc.value.detail.lower()

        session.exec(select(Document)).one().status = "approved"
        session.commit()
        _set_limits(session, inflight=0, gib=1)
        session.add(_doc(content_bytes=900 * MB))
        session.commit()
        with pytest.raises(HTTPException) as exc:
            quota.enforce_upload_quota(session, UPLOADER, 500 * MB, filename="f.pdf")
        assert "24 hours" in exc.value.detail


def test_counting_helpers_are_scoped_to_one_identity(session: Session) -> None:
    session.add(_doc(UPLOADER, content_bytes=10 * MB))
    session.add(_doc(OTHER, content_bytes=99 * MB))
    session.commit()
    assert quota.count_in_flight(session, UPLOADER.sub) == 1
    assert quota.bytes_in_window(session, UPLOADER.sub) == 10 * MB
    assert quota.bytes_in_window(session, OTHER.sub) == 99 * MB


# --------------------------------------------------------------------------
# Through the real ingest path: the properties that only hold end to end.
# --------------------------------------------------------------------------


class _FakeObjectStore:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    def put(self, key: str, content: bytes) -> None:
        self.puts[key] = content

    def get(self, key: str) -> bytes:
        return self.puts[key]

    def delete(self, key: str) -> None:
        self.puts.pop(key, None)


class _FakeRequest:
    class _App:
        class _State:
            jetstream = None

        state = _State()

    app = _App()


@pytest.fixture
def ingest_session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for value, rank in [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3)]:
            session.add(ClassificationLevel(value=value, rank=rank))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def store(monkeypatch):
    fake = _FakeObjectStore()
    monkeypatch.setattr(upload, "get_object_store", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_op_queue_publish(monkeypatch):
    async def _publish(_js, _value):
        return None

    monkeypatch.setattr(upload, "publish_ingestion_job", _publish)
    monkeypatch.setattr(upload, "mark_published", lambda _doc_id: None)


def _file(data: bytes, filename: str = "report.txt") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


async def _submit(session: Session, data: bytes = b"hello world", filename: str = "report.txt"):
    return await upload.submit_document(
        request=_FakeRequest(),
        file=_file(data, filename),
        classification="UNCLASSIFIED",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="Ops",
        doc_type="SOP",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
        user=UPLOADER,
        session=session,
    )


async def test_an_accepted_upload_records_its_byte_size(ingest_session: Session, store) -> None:
    """The storage cap is only enforceable if every accepted upload contributes."""
    contents = b"x" * 4096
    doc = await _submit(ingest_session, contents)
    assert doc.content_bytes == len(contents)


async def test_a_refused_upload_writes_nothing_durable(ingest_session: Session, store) -> None:
    """The whole point of checking before the object-store write.

    If the refusal happened after, a caller could exhaust storage *through* the
    control that exists to prevent it -- every rejected attempt would still have
    landed its bytes.
    """
    _set_limits(ingest_session, inflight=1, gib=0)
    await _submit(ingest_session, b"first")
    assert len(store.puts) == 1

    with pytest.raises(HTTPException) as exc:
        await _submit(ingest_session, b"second")
    assert exc.value.status_code == 429
    assert len(store.puts) == 1, "the refused upload still wrote to the object store"
    assert len(ingest_session.exec(select(Document)).all()) == 1, "a Document row was created"


async def test_a_batch_counts_every_file_not_one_request(ingest_session: Session, store) -> None:
    """#424's fourth point. The check lives in the shared `_ingest_one_file`
    helper precisely so a batch cannot bypass the quota by being one HTTP call.
    """
    _set_limits(ingest_session, inflight=2, gib=0)
    results = await upload.submit_documents_batch(
        request=_FakeRequest(),
        files=[_file(b"one", "a.txt"), _file(b"two", "b.txt"), _file(b"three", "c.txt")],
        classification="UNCLASSIFIED",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="Ops",
        doc_type="SOP",
        program_community=None,
        effective_date=None,
        user=UPLOADER,
        session=ingest_session,
    )
    accepted = [r for r in results if r.accepted]
    refused = [r for r in results if not r.accepted]
    assert len(accepted) == 2, f"cap of 2 admitted {len(accepted)}"
    assert len(refused) == 1
    assert len(store.puts) == 2, "a refused file still reached the object store"


# --------------------------------------------------------------------------
# The admin surface (#424's third point: configurable, not hardcoded).
# --------------------------------------------------------------------------


ADMIN = UserClaims(
    sub="admin-sub",
    preferred_username="dave-admin",
    org="USAREUR-AF",
    rag_roles=["rag-admin"],
)


class TestAdminEndpoint:
    def _set(self, session: Session, inflight: int, gib: float):
        from app.routes import admin

        return admin.set_upload_quota(
            admin.UploadQuotaIn(max_inflight=inflight, max_bytes_24h_gib=gib),
            user=ADMIN,
            session=session,
        )

    def test_gib_is_stored_as_bytes(self, session: Session) -> None:
        """Admins set a storage policy in GiB; the enforcement path only knows
        bytes. Converting at the boundary keeps the unit confusion in one place."""
        settings = self._set(session, 50, 2.5)
        assert settings.upload_quota_max_inflight == 50
        assert settings.upload_quota_max_bytes_24h == int(2.5 * GIB)
        assert quota.resolve_limits(session).max_bytes_24h == int(2.5 * GIB)

    def test_zero_is_accepted_as_unlimited(self, session: Session) -> None:
        settings = self._set(session, 0, 0)
        assert (settings.upload_quota_max_inflight, settings.upload_quota_max_bytes_24h) == (0, 0)

    @pytest.mark.parametrize(("inflight", "gib"), [(-1, 1), (1, -1)])
    def test_negative_values_are_rejected_not_clamped(
        self, session: Session, inflight: int, gib: float
    ) -> None:
        """Reading a fat-fingered "-1" as 0 would turn a typo into "unlimited" --
        the opposite of what the person meant, and silently."""
        with pytest.raises(HTTPException) as exc:
            self._set(session, inflight, gib)
        assert exc.value.status_code == 400

    def test_the_change_is_audited(self, session: Session) -> None:
        self._set(session, 7, 1)
        entry = session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "admin.upload_quota_set")
        ).one()
        assert entry.actor_sub == ADMIN.sub
        assert entry.detail["max_inflight"] == 7
        assert entry.detail["max_bytes_24h"] == GIB

    def test_setting_a_quota_takes_effect_immediately(self, session: Session) -> None:
        """No caching between the admin surface and enforcement -- the next
        submission reads the row."""
        self._set(session, 1, 0)
        session.add(_doc())
        session.commit()
        with pytest.raises(HTTPException) as exc:
            quota.enforce_upload_quota(session, UPLOADER, 1 * MB, filename="f.pdf")
        assert exc.value.status_code == 429


def test_the_admin_page_exposes_both_caps() -> None:
    """A cap nobody can see or change is a hardcoded cap with extra steps."""
    import pathlib as _pathlib

    admin_html = (
        _pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "admin.html"
    ).read_text(encoding="utf-8")
    assert 'id="uploadQuotaForm"' in admin_html
    assert 'name="max_inflight"' in admin_html
    assert 'name="max_bytes_24h_gib"' in admin_html
    assert "/admin/upload-quota" in admin_html


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 bytes"),
        (512, "512 bytes"),
        (10_000, "9.77 KiB"),
        (5 * MB, "5.00 MiB"),
        (2 * GIB, "2.00 GiB"),
    ],
)
def test_byte_counts_are_reported_in_a_useful_unit(value: int, expected: str) -> None:
    """Every size was formatted as GiB at first, so a 10,000-byte cap produced
    "0.00 GiB of 0.00 GiB submitted" -- a message that tells the submitter nothing
    and reads as a bug. Found by running a refusal against real Postgres, not by
    reading the code.
    """
    assert quota._human_bytes(value) == expected


def test_a_small_cap_produces_a_message_with_real_numbers(session: Session) -> None:
    _set_limits(session, inflight=0, gib=0)
    settings = session.get(PortalSettings, 1)
    settings.upload_quota_max_bytes_24h = 10_000
    session.add(settings)
    session.add(_doc(content_bytes=9_000))
    session.commit()
    with pytest.raises(HTTPException) as exc:
        quota.enforce_upload_quota(session, UPLOADER, 2_000, filename="f.pdf")
    assert "0.00 GiB" not in exc.value.detail
    assert "KiB" in exc.value.detail
