"""Issue #211: which uploads the pipeline will accept, and whether the bytes
match the name.

`POST /documents` validated size and nothing else, so any blob reached the
worker's parsers, and `parse_document` dispatches on the *filename extension* --
which the uploader chooses. A caller could therefore pick which parser ran on
their bytes independently of what those bytes actually were.

The allowlist lives here, in `common`, rather than in either service, because
the two need to agree and there is no mechanism that would notice if they
drifted: `ingestion-api` must reject at upload (so FR-8 gives the uploader a
synchronous, actionable error) while `ingestion-worker` dispatches on the same
set minutes later. One list, imported by both.

What this deliberately is not
-----------------------------
This is not antivirus and not a content scanner. It answers two narrow
questions -- "is there a parser for this extension" and "do the leading bytes
agree with it" -- which is what stops a caller aiming arbitrary bytes at a
parser that was never chosen for them. Deep validation of a well-formed-but-
malicious document is what the curator review gate (FR-11) and the resource
bounds (#208) exist for.
"""

from __future__ import annotations

from pathlib import Path

# Magic-byte prefixes per extension. `None` means the format has no reliable
# signature (plain text and its markup dialects), which is handled by the
# binary-content check below rather than by pretending a signature exists.
#
# Kept in step with parse_document()'s dispatch in
# services/ingestion-worker/app/parsing.py -- test_file_types.py asserts the
# two agree, so adding a parser there without adding it here fails CI.
SUPPORTED_TYPES: dict[str, tuple[bytes, ...] | None] = {
    ".txt": None,
    ".md": None,
    ".markdown": None,
    ".html": None,
    ".htm": None,
    ".pdf": (b"%PDF-",),
    # OOXML formats are ZIP containers. b"PK\x05\x06" and b"PK\x07\x08" are the
    # empty-archive and spanned-archive markers; a real .docx always starts
    # with a local file header, but accepting all three avoids rejecting an
    # oddly-produced-but-valid file on a technicality the parser would handle.
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}

# Enough to cover every signature above with room to spare.
SNIFF_BYTES = 8

# Signatures for formats this pipeline does NOT accept, checked so that a
# rejected upload can say what it looks like instead of only what it isn't.
# An uploader who renamed a .doc to .docx gets told that, rather than a
# generic mismatch they then have to guess at.
_KNOWN_FOREIGN: tuple[tuple[bytes, str], ...] = (
    (b"\xd0\xcf\x11\xe0", "a legacy OLE2 document (.doc/.xls/.ppt)"),
    (b"\x7fELF", "an ELF executable"),
    (b"MZ", "a Windows executable"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"\x89PNG", "a PNG image"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"Rar!", "a RAR archive"),
    (b"7z\xbc\xaf", "a 7-Zip archive"),
)


class UnsupportedUpload(ValueError):
    """The upload's extension has no parser, or its bytes disagree with it."""


def supported_extensions() -> list[str]:
    """Sorted, for error messages and docs."""
    return sorted(SUPPORTED_TYPES)


def _describe(head: bytes) -> str | None:
    for signature, description in _KNOWN_FOREIGN:
        if head.startswith(signature):
            return description
    for extension, signatures in SUPPORTED_TYPES.items():
        if signatures and any(head.startswith(s) for s in signatures):
            return f"a {extension} file"
    return None


def validate_upload(filename: str, head: bytes) -> None:
    """Raise UnsupportedUpload unless `filename`'s extension has a parser and
    `head` (the leading SNIFF_BYTES of the file) agrees with it.

    Both directions matter. An unsupported extension is the obvious case; a
    supported extension whose bytes are something else is the one that let a
    caller aim a parser at input it was not chosen for.
    """
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_TYPES:
        raise UnsupportedUpload(
            f"unsupported file type {extension or filename!r}; "
            f"accepted: {', '.join(supported_extensions())}"
        )

    signatures = SUPPORTED_TYPES[extension]
    if signatures is not None:
        if not any(head.startswith(s) for s in signatures):
            actual = _describe(head)
            detail = f" -- it looks like {actual}" if actual else ""
            raise UnsupportedUpload(f"file is named {extension} but its contents are not{detail}")
        return

    # Text formats have no signature of their own, so the check is inverted:
    # reject content that is recognisably something else.
    #
    # Two tests, because neither is sufficient alone. A known signature is the
    # precise one. The NUL-byte scan is the catch-all for binary formats not
    # in _KNOWN_FOREIGN -- no UTF-8 text contains a NUL.
    #
    # The NUL scan alone is genuinely not enough, which a live test caught:
    # PNG's 8-byte header (\x89PNG\r\n\x1a\n) has no NUL in it at all -- the
    # first ones appear at byte 8, in the IHDR length -- so a PNG renamed to
    # .txt sailed through a NUL-only check.
    actual = _describe(head)
    if actual is not None:
        raise UnsupportedUpload(f"file is named {extension} but it looks like {actual}")
    if b"\x00" in head:
        raise UnsupportedUpload(f"file is named {extension} but contains binary data")
