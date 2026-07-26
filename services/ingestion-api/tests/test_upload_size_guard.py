"""Coverage for issue #107: the FR-9/NFR-7 size guard bounds memory rather
than being decided after the whole upload is already materialised.

Scope worth stating up front, because it's easy to read more into these than
they prove: by the time any handler runs, Starlette's multipart parser has
already consumed the request body and spooled it (past 1MB, to a temp file).
_read_bounded therefore bounds what *ingestion-api* holds in memory -- it
cannot bound bytes transferred or written to disk. That layer is the
ingress annotation (helm/nexus-rag/values.yaml), which no unit test here
exercises.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.routes import upload


def _upload(data: bytes, *, size: int | None = None) -> UploadFile:
    """An UploadFile over an in-memory buffer. `size` is settable separately
    so the size-unknown path can be exercised -- Starlette's parser always
    populates it, but _read_bounded must not depend on that."""
    f = UploadFile(file=io.BytesIO(data), filename="doc.txt", size=size)
    if size is None:
        f.size = None
    return f


class TestReadBounded:
    async def test_reads_a_file_within_the_limit_unchanged(self):
        data = b"a" * 2048

        assert await upload._read_bounded(_upload(data, size=len(data)), 4096) == data

    async def test_file_at_exactly_the_limit_is_accepted(self):
        """Off-by-one guard: the limit is inclusive, matching the previous
        `len(contents) > MAX_UPLOAD_BYTES` semantics exactly."""
        data = b"a" * 4096

        assert await upload._read_bounded(_upload(data, size=len(data)), 4096) == data

    async def test_declared_size_over_the_limit_is_rejected(self):
        data = b"a" * 8192

        with pytest.raises(HTTPException) as exc:
            await upload._read_bounded(_upload(data, size=len(data)), 4096)

        assert exc.value.status_code == 413

    async def test_oversized_file_is_rejected_even_when_size_is_unknown(self):
        """The declared-size check is a fast path, not the guard. If size
        isn't populated, the chunked read still has to stop."""
        data = b"a" * 8192

        with pytest.raises(HTTPException) as exc:
            await upload._read_bounded(_upload(data, size=None), 4096)

        assert exc.value.status_code == 413

    async def test_read_stops_early_instead_of_consuming_the_whole_upload(
        self, monkeypatch
    ):
        """The point of the change: the old code did one read() of everything
        and *then* measured it. This asserts we stop partway through rather
        than materialising a body far larger than the limit.

        The chunk size is shrunk for the test so the property is observable
        without allocating megabytes -- at the real 1MB chunk, any body small
        enough to be cheap in a unit test fits in a single read."""
        monkeypatch.setattr(upload, "_READ_CHUNK_BYTES", 1024)
        limit = 4096
        body = b"a" * (limit * 50)
        consumed = 0

        class _CountingFile(io.BytesIO):
            def read(self, n=-1):
                nonlocal consumed
                data = super().read(n)
                consumed += len(data)
                return data

        f = UploadFile(file=_CountingFile(body), filename="big.txt")
        f.size = None  # force the chunked path

        with pytest.raises(HTTPException):
            await upload._read_bounded(f, limit)

        # Peak is bounded by limit + one chunk, not by what was sent.
        assert consumed <= limit + 1024
        assert consumed < len(body)

    async def test_empty_file_reads_as_empty_not_an_error(self):
        """_read_bounded doesn't own the empty-file rule -- submit_document
        raises 400 for that separately, and conflating the two would turn an
        empty upload into a 413."""
        assert await upload._read_bounded(_upload(b"", size=0), 4096) == b""
