"""Issue #211: uploads are checked against a parser allowlist, in both
directions.

The interesting half is the second one. An unsupported extension is the
obvious case and would have been caught at parse time anyway (as a `failed`
document). A *supported* extension whose bytes are something else is the case
that mattered: `parse_document` dispatches on the filename, which the uploader
chooses, so a caller could aim any parser at any bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.file_types import (
    SNIFF_BYTES,
    SUPPORTED_TYPES,
    UnsupportedUpload,
    supported_extensions,
    validate_upload,
)

PDF = b"%PDF-1.7"
ZIP = b"PK\x03\x04\x14\x00\x00\x00"
TEXT = b"# A hea"
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ELF = b"\x7fELF\x02\x01\x01\x00"


class TestAcceptsWhatThePipelineCanParse:
    @pytest.mark.parametrize(
        ("filename", "head"),
        [
            ("policy.pdf", PDF),
            ("policy.docx", ZIP),
            ("deck.pptx", ZIP),
            ("sheet.xlsx", ZIP),
            ("notes.txt", TEXT),
            ("notes.md", TEXT),
            ("notes.markdown", TEXT),
            ("page.html", b"<!DOCTYP"),
            ("page.htm", b"<html>\n\n"),
        ],
    )
    def test_valid_uploads_pass(self, filename, head):
        validate_upload(filename, head)

    def test_extension_matching_is_case_insensitive(self):
        # Windows clients routinely send .PDF.
        validate_upload("POLICY.PDF", PDF)


class TestRejectsUnsupportedExtensions:
    @pytest.mark.parametrize("filename", ["archive.zip", "image.bmp", "run.exe", "noextension"])
    def test_unsupported_extension_is_rejected(self, filename):
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload(filename, TEXT)

        # The message must tell the uploader what would work.
        assert ".pdf" in str(exc.value)


class TestRejectsMismatchedContent:
    """The half that matters: the extension picks the parser, so bytes that
    disagree with it are a caller choosing a parser for input it wasn't
    written for."""

    def test_executable_renamed_to_pdf(self):
        with pytest.raises(UnsupportedUpload, match="not"):
            validate_upload("payload.pdf", ELF)

    def test_legacy_doc_renamed_to_docx(self):
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("old.docx", OLE2)

        # Naming what it actually looks like turns "rejected" into something
        # the uploader can act on -- this is the common honest mistake.
        assert "OLE2" in str(exc.value)

    def test_recognised_binary_renamed_to_txt_is_named(self):
        """Text formats have no signature of their own, so the check is
        inverted. A recognised signature gives the precise message."""
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("notes.txt", ELF)

        assert "ELF" in str(exc.value)

    def test_unrecognised_binary_falls_back_to_the_nul_scan(self):
        """The catch-all for binary formats not in _KNOWN_FOREIGN. No UTF-8
        text contains a NUL, so this stays a safe test even though it can't
        say what the file actually is."""
        with pytest.raises(UnsupportedUpload, match="binary"):
            validate_upload("notes.txt", b"\xab\xcd\x00\x01\x02\x03\x04\x05")

    def test_pdf_renamed_to_txt_is_named_in_the_error(self):
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("notes.txt", PDF)

        assert ".pdf" in str(exc.value)

    def test_png_renamed_to_txt(self):
        """Regression, found by testing this against the live stack: PNG's
        8-byte header contains no NUL at all -- the first ones are at byte 8,
        in the IHDR length -- so the original NUL-only check accepted it.
        A signature match is now tried before the NUL scan for exactly this."""
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("image.txt", b"\x89PNG\r\n\x1a\n")

        # Since #241 PNG is a *supported* type, so the mismatch is named as
        # ".png" (rename it and it parses) rather than as a foreign format.
        assert ".png" in str(exc.value)

    @pytest.mark.parametrize(
        ("head", "expected"),
        [
            # #241: PNG/JPEG are supported types now, so a mismatch names the
            # extension that WOULD parse rather than calling them foreign.
            (b"\x89PNG\r\n\x1a\n", ".png"),
            (b"\xff\xd8\xff\xe0\x00\x10JF", ".jp"),
            (b"Rar!\x1a\x07\x00\x00", "RAR"),
            (b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "gzip"),
        ],
    )
    def test_known_binary_formats_are_named_not_just_rejected(self, head, expected):
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("notes.md", head)

        assert expected in str(exc.value)

    # --- issue #241: image formats are content now ---------------------------

    @pytest.mark.parametrize(
        ("filename", "head"),
        [
            ("scan.png", b"\x89PNG\r\n\x1a\n"),
            ("scan.jpg", b"\xff\xd8\xff\xe0\x00\x10JF"),
            ("scan.jpeg", b"\xff\xd8\xff\xe1\x00\x18Ex"),
            ("scan.tif", b"II*\x00\x08\x00\x00\x00"),
            ("scan.tiff", b"MM\x00*\x00\x00\x00\x08"),
        ],
    )
    def test_image_uploads_accepted(self, filename, head):
        validate_upload(filename, head)

    def test_image_name_with_foreign_bytes_still_rejected(self):
        # The mismatch direction #211 exists for: a .png that is really a PDF
        # must not reach the OCR path.
        with pytest.raises(UnsupportedUpload) as exc:
            validate_upload("scan.png", b"%PDF-1.7\n")

        assert ".pdf" in str(exc.value)

    def test_text_that_merely_looks_unusual_is_still_accepted(self):
        # No NUL byte -- UTF-8 prose in any language is fine, and a check that
        # rejected it would be worse than no check.
        validate_upload("notes.txt", "café ☕".encode()[:SNIFF_BYTES])


class TestTheAllowlistMatchesTheParsers:
    """The two lists live in different services. Nothing but this test would
    notice them drifting apart."""

    def test_every_parser_extension_is_in_the_allowlist(self):
        parsing = (
            Path(__file__).resolve().parents[3]
            / "services"
            / "ingestion-worker"
            / "app"
            / "parsing.py"
        )
        body = parsing.read_text().split("def parse_document")[1].split("def _parse_txt")[0]

        dispatched = set(re.findall(r'ext (?:==|in \()\s*"(\.[a-z]+)"', body))
        dispatched |= set(re.findall(r'"(\.[a-z]+)",?\s*\)', body))

        missing = dispatched - set(SUPPORTED_TYPES)
        assert not missing, (
            f"parse_document handles {sorted(missing)} but validate_upload would "
            f"reject them -- the upload gate and the parser dispatch must agree"
        )

    def test_supported_extensions_is_sorted_and_non_empty(self):
        assert supported_extensions() == sorted(SUPPORTED_TYPES)
        assert supported_extensions()
