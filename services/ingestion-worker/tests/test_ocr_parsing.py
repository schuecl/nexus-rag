"""Issue #241: OCR for image uploads and scanned (image-only) PDF pages.

No tesseract binary anywhere in these tests: `app.ocr`'s two entry points are
monkeypatched for parse-level behavior, and `ocr.py` itself is exercised
against a stubbed `pytesseract` module. The failure-honesty split under test:
an image upload with no recognizable text is a ParsingError (OCR is that
format's only content path), while a scanned PDF page degrades to a logged
skip (those pages contribute nothing today, so a broken OCR must not fail
what used to succeed).
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

from app import ocr, parsing
from app.chunking import chunk_sections
from app.parsing import OcrStatus, ParsedSection, ParsingError, parse_document

FIXTURES = Path(__file__).parent / "fixtures"


def _noise_png(width: int = 256, height: int = 160, seed: int = 0) -> bytes:
    """Deterministic noise: solid colors compress to nearly nothing and real
    scanners never produce them; noise also defeats any size-based filter."""
    import random

    from PIL import Image

    rng = random.Random(seed)
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(width * height)
        ]
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _image_only_pdf(pages: int = 1) -> bytes:
    """A 'scanned' PDF: image pages, no text layer -- Pillow's PDF writer."""
    from PIL import Image

    images = [Image.open(io.BytesIO(_noise_png(seed=i))).convert("RGB") for i in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def _mixed_pdf() -> bytes:
    """Page 1: real text layer (committed fixture); page 2: image-only."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO((FIXTURES / "prose_only.pdf").read_bytes())))
    writer.append(PdfReader(io.BytesIO(_image_only_pdf())))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def ocr_ready(monkeypatch):
    """OCR 'available' and recognizing fixed text, without any tesseract."""
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda data: "RECOGNIZED memo text line.")


@pytest.fixture
def ocr_absent(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: False)


# --- standalone image uploads -------------------------------------------------


def test_image_upload_becomes_one_ocr_section(ocr_ready):
    sections = parse_document("scan.png", _noise_png())
    assert len(sections) == 1
    assert sections[0].content_type == "ocr"
    assert sections[0].text == "RECOGNIZED memo text line."


def test_image_with_no_recognizable_text_fails_actionably(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda data: "")
    with pytest.raises(ParsingError, match="no readable text"):
        parse_document("scan.jpg", _noise_png())


def test_image_upload_without_ocr_fails_not_silently_succeeds(ocr_absent):
    # For this format OCR is the only content path: an empty success would be
    # a lie, so this is the fail-honestly side of the split.
    with pytest.raises(ParsingError, match="OCR is unavailable"):
        parse_document("scan.png", _noise_png())


# --- scanned-PDF fallback -----------------------------------------------------


def test_scanned_pdf_pages_ocr_with_page_attribution(ocr_ready):
    sections = parse_document("scanned.pdf", _image_only_pdf(pages=2))
    assert [s.content_type for s in sections] == ["ocr", "ocr"]
    assert [s.page_or_slide for s in sections] == [1, 2]
    assert all("RECOGNIZED" in s.text for s in sections)


def test_mixed_pdf_keeps_text_layer_and_ocrs_only_the_scanned_page(ocr_ready, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        ocr, "ocr_image", lambda data: calls.append(1) or "RECOGNIZED memo text line."
    )
    sections = parse_document("mixed.pdf", _mixed_pdf())
    # Page 1's real text layer is untouched prose; page 2 is OCR.
    assert [s.content_type for s in sections] == ["text", "ocr"]
    assert sections[0].page_or_slide == 1
    assert sections[1].page_or_slide == 2
    # Exactly one OCR call: the text page never reached the fallback.
    assert len(calls) == 1


def test_text_pdf_parses_identically_with_ocr_unavailable(ocr_absent):
    # The fallback must not perturb a PDF with a text layer in any way.
    sections = parse_document("prose_only.pdf", (FIXTURES / "prose_only.pdf").read_bytes())
    assert sections
    assert all(s.content_type in ("text", "table") for s in sections)


def test_scanned_pdf_without_ocr_degrades_to_empty_not_error(ocr_absent):
    # The degrade side of the split: same observable outcome as before #241
    # (no sections -> the worker's existing "no extractable text" failure),
    # never a new exception type.
    assert parse_document("scanned.pdf", _image_only_pdf()) == []


def test_ocr_budget_bounds_a_many_page_scan(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    calls: list[int] = []
    monkeypatch.setattr(ocr, "ocr_image", lambda data: calls.append(1) or "RECOGNIZED text.")
    monkeypatch.setattr(parsing, "_MAX_OCR_IMAGES", 2)
    sections = parse_document("scanned.pdf", _image_only_pdf(pages=4))
    assert len(calls) == 2
    assert len(sections) == 2  # pages past the budget are skipped, loudly logged


# --- OcrStatus: issue #306 gap 3 ------------------------------------------------


def test_ocr_status_untouched_when_ocr_unneeded(ocr_ready):
    status = OcrStatus()
    parse_document("scanned.pdf", _image_only_pdf(pages=2), status)
    assert status.any_skipped is False
    assert status.skipped_pages == 0


def test_ocr_status_records_unavailable_engine(ocr_absent):
    status = OcrStatus()
    parse_document("scanned.pdf", _image_only_pdf(pages=2), status)
    assert status.any_skipped is True
    assert status.skipped_pages == 2
    assert status.reasons == {"ocr_unavailable"}


def test_ocr_status_records_budget_exhaustion(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda data: "RECOGNIZED text.")
    monkeypatch.setattr(parsing, "_MAX_OCR_IMAGES", 2)
    status = OcrStatus()
    parse_document("scanned.pdf", _image_only_pdf(pages=4), status)
    assert status.skipped_pages == 2
    assert status.reasons == {"budget_exhausted"}


def test_ocr_status_records_no_text_recognized(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda data: "")
    status = OcrStatus()
    sections = parse_document("scanned.pdf", _image_only_pdf(pages=1), status)
    assert sections == []
    assert status.skipped_pages == 1
    assert status.reasons == {"no_text_recognized"}


def test_ocr_status_not_recorded_for_text_pdf():
    # A page with a real text layer never reaches the OCR fallback at all --
    # OcrStatus must stay clean, not report a coverage gap that doesn't exist.
    status = OcrStatus()
    parse_document("prose_only.pdf", (FIXTURES / "prose_only.pdf").read_bytes(), status)
    assert status.any_skipped is False


def test_ocr_status_defaults_to_none_without_breaking_parse(ocr_absent):
    # Callers that don't care (the vast majority of this file's tests) can
    # omit ocr_status entirely.
    assert parse_document("scanned.pdf", _image_only_pdf()) == []


# --- chunking: "ocr" is prose, not atomic ------------------------------------


def test_ocr_sections_flow_through_the_sliding_window():
    long_scan = ParsedSection(text="word " * 1200, content_type="ocr", page_or_slide=3)
    chunks = chunk_sections([long_scan], target_words=512, overlap_ratio=0.15)
    # A 1200-word scanned page must split like prose (the atomic path would
    # recreate #114's oversized-chunk failure), keeping type and attribution.
    assert len(chunks) > 1
    assert all(c.content_type == "ocr" for c in chunks)
    assert all(c.page_or_slide == 3 for c in chunks)


def test_tables_remain_atomic():
    table = ParsedSection(text="| a | b |\n| --- | --- |\n| 1 | 2 |", content_type="table")
    assert len(chunk_sections([table])) == 1


# --- ocr.py itself ------------------------------------------------------------


def _stub_pytesseract(monkeypatch, image_to_string):
    module = types.ModuleType("pytesseract")
    module.image_to_string = image_to_string
    module.get_tesseract_version = lambda: "5.0-stub"
    monkeypatch.setitem(sys.modules, "pytesseract", module)


def test_ocr_image_recognizes_via_pytesseract(monkeypatch):
    _stub_pytesseract(monkeypatch, lambda image, lang: "  Recognized page text.  \n")
    assert ocr.ocr_image(_noise_png()) == "Recognized page text."


def test_ocr_image_skips_glyph_sized_images(monkeypatch):
    called: list[int] = []
    _stub_pytesseract(monkeypatch, lambda image, lang: called.append(1) or "x")
    assert ocr.ocr_image(_noise_png(width=16, height=16)) == ""
    assert not called  # filtered before tesseract was ever invoked


def test_ocr_image_degrades_on_undecodable_bytes(monkeypatch):
    _stub_pytesseract(monkeypatch, lambda image, lang: "never reached")
    assert ocr.ocr_image(b"not an image at all") == ""


def test_ocr_image_degrades_when_tesseract_raises(monkeypatch):
    def boom(image, lang):
        raise RuntimeError("tesseract exploded")

    _stub_pytesseract(monkeypatch, boom)
    assert ocr.ocr_image(_noise_png()) == ""


def test_ocr_available_reflects_probe_and_is_cached(monkeypatch):
    probes: list[int] = []

    def version():
        probes.append(1)
        raise OSError("no tesseract binary")

    module = types.ModuleType("pytesseract")
    module.get_tesseract_version = version
    monkeypatch.setitem(sys.modules, "pytesseract", module)

    ocr.ocr_available.cache_clear()
    try:
        assert ocr.ocr_available() is False
        assert ocr.ocr_available() is False
        assert len(probes) == 1  # cached: the binary cannot appear mid-process
    finally:
        ocr.ocr_available.cache_clear()
