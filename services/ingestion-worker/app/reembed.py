"""Issue #362: the re-embedding path #122/PR #130 shipped detection for but
not a fix -- an operator's remedy for a stale-embedding-model collection was,
until this module, a full manual re-ingest of every affected document.

Deliberately an offline, operator-triggered mode (`python -m app.reembed`),
not something ``rag_search``'s mismatch check or the JetStream consumer
triggers automatically: re-embedding a whole classification's worth of
documents is a slow, potentially expensive maintenance action (every
document's original is re-fetched, re-parsed, re-chunked, re-embedded), and
kicking it off from a read-path or a queue consumer would tangle a
maintenance operation with the invariants those two paths are built around
(read-only vs. write, request-scoped vs. long-running). An operator runs
this deliberately, the same way `scripts/calibrate_tagging_advisory.py` and
the Postgres one-shots (`grant-service-privileges`, `lock-down-db-grants`)
are deliberately separate from the request-serving paths.

Scope: every ``documents`` row for the given classification(s) whose status
is ``approved`` or ``pending_review`` -- the only two states with chunks that
matter for retrieval or an eventual curation decision. ``rejected``/
``superseded``/``failed`` documents have chunks FR-26 already excludes (or,
for superseded, chunks already deleted -- FR-7), so re-embedding them would
burn compute for no retrieval-visible benefit.

Idempotent and resumable by construction, not by a separate checkpoint
mechanism: each document is skipped if its existing first chunk already
carries the currently-configured `EMBEDDING_MODEL` (unless `force=True`), so
a run interrupted partway through -- or re-run after fixing an unrelated
per-document failure -- picks up only what still needs it, the same
skip-if-current idea `rag_search`'s own mismatch check uses.

Deliberately NOT re-run here: the curation advisories `processing.py` computes
at first ingestion (tagging/content/PII/precedent/LLM-suggestion advisories).
Those are artifacts of the *original* ingestion decision -- much of what this
runs against is already `approved` -- and re-running them against a
re-parse would silently second-guess a curator's already-made decision. This
module only touches what retrieval actually serves: parsed text, chunk
boundaries, and vectors.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.captioning import caption_images, captioning_enabled
from app.chunking import chunk_sections
from app.embedding import EMBEDDING_MODEL, EmbeddingError, embed_texts
from app.parsing import OcrStatus, ParsingError, parse_document
from common.db import get_engine, init_db
from common.log_safety import log_safe
from common.models import AuditLogEntry, Document
from common.object_store import get_object_store
from common.qdrant_store import EMBEDDING_MODEL_KEY
from common.sparse_embedding import embed_sparse
from common.vector_store import ChunkPoint, get_store

logger = logging.getLogger("ingestion-worker.reembed")

# The two Document statuses with chunks that matter for retrieval or a
# still-pending curation decision -- see module docstring's Scope section.
_REEMBEDDABLE_STATUSES = ("approved", "pending_review")


@dataclass
class ReembedReport:
    """What one `reembed_classifications` call actually did, returned rather
    than only logged so a caller (the CLI below, or a test) can assert on it
    without scraping log output."""

    processed: list[str] = field(default_factory=list)
    skipped_already_current: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


class _PermanentReembedError(Exception):
    """Mirrors processing.py's permanent-vs-transient split, but there is no
    JetStream redelivery here to hand a transient failure to -- so this
    module doesn't distinguish them the way `_process_document` does. Every
    per-document failure is caught, logged, and counted; the operator decides
    whether to re-run (which is always safe -- see module docstring)."""


async def _reembed_one_document(document_id: uuid.UUID, classification: str) -> None:
    """Re-parse, re-chunk, and re-embed a single document's original bytes,
    then replace its chunks in place. Raises on any failure; the caller
    catches, logs, and continues with the rest of the batch."""
    with Session(get_engine()) as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise _PermanentReembedError(f"document {document_id} no longer exists")
        if doc.original_object_key is None:
            raise _PermanentReembedError("original was purged; nothing to re-embed from")

        contents = get_object_store().get(doc.original_object_key)
        if doc.content_sha256 is not None:
            actual = hashlib.sha256(contents).hexdigest()
            if actual != doc.content_sha256:
                raise _PermanentReembedError(
                    f"content integrity check failed: expected sha256 "
                    f"{doc.content_sha256}, got {actual}"
                )

        sections = parse_document(doc.filename, contents, OcrStatus())
        if captioning_enabled():
            sections = sections + await caption_images(doc.filename, contents)
        chunks = chunk_sections(sections)
        if not chunks:
            raise _PermanentReembedError("re-parse produced no extractable text")

        dense_vectors = await embed_texts([c.text for c in chunks])
        sparse_vectors = embed_sparse([c.text for c in chunks])

        points = [
            ChunkPoint(
                # Same deterministic scheme as ingestion (processing.py) --
                # this is what lets replace_document_chunks overwrite a
                # still-current chunk index in place instead of creating a
                # second, duplicate point for it.
                id=str(uuid.uuid5(doc.id, f"chunk:{chunk.chunk_index}")),
                dense=dense,
                sparse=sparse,
                payload={
                    "document_id": str(doc.id),
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.text,
                    "heading": chunk.heading,
                    "page_or_slide": chunk.page_or_slide,
                    "content_type": chunk.content_type,
                    EMBEDDING_MODEL_KEY: EMBEDDING_MODEL,
                    "embedded_at": datetime.now(UTC).isoformat(),
                    "filename": doc.filename,
                    "doc_type": doc.doc_type,
                    "source_originator": doc.source_originator,
                    "classification": doc.classification,
                    "releasability": doc.releasability,
                    "access_scope": doc.access_scope,
                    # Preserved exactly, never re-derived -- a re-embed must
                    # not change what curation decision a chunk carries.
                    "status": doc.status,
                },
            )
            for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True)
        ]

        get_store().replace_document_chunks(
            str(doc.id), classification=classification, points=points
        )

        doc.chunk_count = len(points)
        doc.updated_at = datetime.now(UTC)
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub="system",
                actor_username="reembed-cli",
                action="document.reembedded",
                target_id=str(doc.id),
                detail={
                    "classification": classification,
                    "embedding_model": EMBEDDING_MODEL,
                    "chunk_count": len(points),
                },
            )
        )
        session.commit()


def _needs_reembedding(document_id: uuid.UUID, classification: str) -> bool:
    """Peeks at one existing chunk rather than re-deriving a per-classification
    mismatch from the vector store: `stored_embedding_model()` (issue #122)
    answers "does the whole store agree with EMBEDDING_MODEL", which is the
    right question for rag_search's gate but the wrong one here -- this needs
    "does *this document's* chunks", so a batch spanning several
    classifications skips exactly the documents that don't need it,
    regardless of what any other collection looks like."""
    existing = get_store().fetch_document_chunks(str(document_id), classification)
    if not existing:
        return True
    return existing[0].get(EMBEDDING_MODEL_KEY) != EMBEDDING_MODEL


async def _reembed_classifications_async(
    classifications: list[str] | None, *, force: bool, dry_run: bool
) -> ReembedReport:
    report = ReembedReport()
    with Session(get_engine()) as session:
        query = select(Document.id, Document.classification).where(
            Document.status.in_(_REEMBEDDABLE_STATUSES)  # type: ignore[attr-defined]
        )
        if classifications is not None:
            query = query.where(Document.classification.in_(classifications))  # type: ignore[attr-defined]
        rows = session.exec(query).all()

    for document_id, classification in rows:
        label = f"{document_id} ({classification})"
        if not force and not _needs_reembedding(document_id, classification):
            logger.info("skipping %s: already current", log_safe(label))
            report.skipped_already_current.append(label)
            continue
        if dry_run:
            logger.info("dry run: would re-embed %s", log_safe(label))
            report.processed.append(label)
            continue
        try:
            await _reembed_one_document(document_id, classification)
            logger.info("re-embedded %s", log_safe(label))
            report.processed.append(label)
        except (ParsingError, EmbeddingError, _PermanentReembedError) as exc:
            logger.error("failed to re-embed %s: %s", log_safe(label), log_safe(exc))
            report.failed[label] = str(exc)
        except Exception as exc:
            logger.exception("unexpected error re-embedding %s", log_safe(label))
            report.failed[label] = f"{type(exc).__name__}: {exc}"

    return report


def reembed_classifications(
    classifications: list[str] | None, *, force: bool = False, dry_run: bool = False
) -> ReembedReport:
    """Re-embed every `approved`/`pending_review` document in the given
    classification(s), or every classification with such a document if
    `classifications` is None.

    `force` re-embeds even documents whose stored chunks already carry the
    current EMBEDDING_MODEL. `dry_run` reports what would be processed
    without touching the object store, the embedding service, or the vector
    store -- safe to run to size a maintenance window before committing to
    one.

    Synchronous on purpose (one `asyncio.run` for the whole batch, not one
    per document/per embedding call) -- this is the CLI's and tests' entry
    point, and neither wants to manage an event loop itself.
    """
    return asyncio.run(
        _reembed_classifications_async(classifications, force=force, dry_run=dry_run)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-embed approved/pending_review documents (issue #362). "
        "Run inside the ingestion-worker container/image, e.g. "
        "`docker compose run --rm ingestion-worker python -m app.reembed CUI`."
    )
    parser.add_argument(
        "classification",
        nargs="*",
        help="Classification value(s) to re-embed. Omit to cover every "
        "classification with an approved/pending_review document.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed even documents whose stored chunks already match the "
        "configured EMBEDDING_MODEL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be re-embedded without touching anything.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()

    report = reembed_classifications(
        args.classification or None, force=args.force, dry_run=args.dry_run
    )

    verb = "would re-embed" if args.dry_run else "re-embedded"
    print(f"{verb}: {len(report.processed)}")
    print(f"skipped (already current): {len(report.skipped_already_current)}")
    if report.failed:
        print(f"failed: {len(report.failed)}")
        for label, reason in report.failed.items():
            print(f"  {label}: {reason}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
