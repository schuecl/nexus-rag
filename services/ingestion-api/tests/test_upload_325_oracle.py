"""Issue #325: validate_supersede_target (common/versioning.py), called from
POST /documents (app/routes/upload.py) whenever a submission sets
supersedes_document_id, used to accumulate *all* violations -- status,
cross-org, classification, releasability -- into one combined 403 message.
Existence is already gated by a clean 404 (upload.py checks that before
calling validate_supersede_target at all), but an out-of-org caller with no
authority over the target document at all could still learn its exact
status, that its classification exceeds their clearance, and that its
releasability exceeds their own, all from a single request -- a lower bar
than #215/#322, which both required at least a rag-curate:<org> role
somewhere.

These tests pin that a cross-org target now collapses to the single
"different org" error, matching common/versioning.py's
test_cross_org_target_short_circuits_other_checks, while a same-org
uploader still gets every applicable error accumulated (unchanged
behavior, useful for fixing multiple problems at once).
"""

from __future__ import annotations

import io

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.datastructures import UploadFile

from app.routes import upload
from common.claims import UserClaims
from common.models import ClassificationLevel, Document

UPLOADER = UserClaims(
    sub="uploader-sub",
    preferred_username="alice-ingest",
    org="USAREUR-AF",
    rag_roles=["rag-ingest", "rag-clearance:CUI", "rag-releasability:NONE"],
)


class _FakeObjectStore:
    def put(self, key: str, content: bytes) -> None:
        pass


class _FakeRequest:
    class _App:
        class _State:
            jetstream = None

        state = _State()

    app = _App()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for value, rank in [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3), ("TOP SECRET", 4)]:
            session.add(ClassificationLevel(value=value, rank=rank))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture(autouse=True)
def _stub_object_store(monkeypatch):
    monkeypatch.setattr(upload, "get_object_store", _FakeObjectStore)


def _upload_file(data: bytes = b"content") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="report.txt", size=len(data))


async def _submit(session, *, supersedes_document_id):
    return await upload.submit_document(
        request=_FakeRequest(),
        file=_upload_file(),
        classification="CUI",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="USAREUR-AF",
        doc_type="report",
        program_community=None,
        effective_date=None,
        supersedes_document_id=supersedes_document_id,
        user=UPLOADER,
        session=session,
        _csrf=None,
    )


class TestSupersedeTargetDoesNotLeakAcrossOrgBoundary:
    async def test_cross_org_target_gets_single_generic_error(self, session):
        old_doc = Document(
            filename="old.pdf",
            uploader_sub="someone-else",
            uploader_username="someone-else",
            owner_org="Signal-Corps",
            classification="SECRET",
            releasability=["NOFORN"],
            access_scope=["Signal-Corps"],
            source_originator="Signal-Corps",
            doc_type="report",
            status="pending_review",
        )
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        with pytest.raises(Exception) as excinfo:
            await _submit(session, supersedes_document_id=str(old_doc.id))

        assert excinfo.value.status_code == 403  # type: ignore[attr-defined]
        detail = excinfo.value.detail  # type: ignore[attr-defined]
        # Not the old status ("pending_review"), not the classification/
        # releasability mismatch -- only that it belongs to a different org.
        assert detail == "target document belongs to a different org"

    async def test_same_org_target_still_accumulates(self, session):
        old_doc = Document(
            filename="old.pdf",
            uploader_sub="someone-else",
            uploader_username="someone-else",
            owner_org="USAREUR-AF",
            classification="SECRET",
            releasability=["NOFORN"],
            access_scope=["USAREUR-AF"],
            source_originator="USAREUR-AF",
            doc_type="report",
            status="pending_review",
        )
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        with pytest.raises(Exception) as excinfo:
            await _submit(session, supersedes_document_id=str(old_doc.id))

        assert excinfo.value.status_code == 403  # type: ignore[attr-defined]
        detail = excinfo.value.detail  # type: ignore[attr-defined]
        assert "not 'approved'" in detail
        assert "above the submitter's cleared level" in detail
        assert "does not hold" in detail
