"""FR-3: parse each supported format into clean text, preserving structural
signal (headings, page/slide numbers) for chunking (FR-4) and citation
(FR-27). FR-9: corrupt/password-protected files raise ParsingError, which the
upload route turns into a clear 4xx instead of a 500 or a silently-empty doc.

Pragmatic choice for this pass: lightweight pure-Python per-format libraries
(pypdf/python-docx/python-pptx/openpyxl/BeautifulSoup) rather than the
heavier Docling/Unstructured candidates from REQUIREMENTS.md Section 7.4 --
those remain worth evaluating for layout-aware table extraction later, but
add a large model-download footprint this dev pass doesn't need.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path


class ParsingError(Exception):
    pass


# NFR-7: .docx/.pptx/.xlsx are ZIP archives under the hood, and none of
# python-docx/python-pptx/openpyxl guard against a zip bomb -- a small,
# maliciously-crafted archive can decompress to gigabytes and exhaust worker
# memory long before MAX_UPLOAD_BYTES (services/ingestion-api/app/routes/
# upload.py) ever sees anything, since that only bounds the *compressed*
# upload size. Checked against the raw zip before handing it to any of those
# libraries.
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200MB decompressed, well over any real OOXML doc
MAX_ZIP_COMPRESSION_RATIO = 200  # legitimate OOXML XML parts rarely exceed the low double digits


def _check_zip_bomb(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if info.compress_size and info.file_size / info.compress_size > (
                    MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise ParsingError(
                        f"archive entry '{info.filename}' has a compression ratio consistent "
                        "with a zip bomb"
                    )
                if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ParsingError(
                        "archive would decompress to over "
                        f"{MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)}MB, consistent with a "
                        "zip bomb"
                    )
    except zipfile.BadZipFile as exc:
        raise ParsingError(f"corrupt archive: {exc}") from exc



@dataclass
class ParsedSection:
    """One structural unit of a document -- a heading's worth of text, a PDF
    page, a slide, a spreadsheet sheet. Chunking (FR-4) never splits across
    section boundaries, only within them."""

    text: str
    heading: str | None = None
    page_or_slide: int | None = None


def parse_document(filename: str, content: bytes) -> list[ParsedSection]:
    if not content:
        raise ParsingError("empty file")

    ext = Path(filename).suffix.lower()
    try:
        if ext in (".txt",):
            return _parse_txt(content)
        if ext in (".md", ".markdown"):
            return _parse_markdown(content)
        if ext in (".html", ".htm"):
            return _parse_html(content)
        if ext == ".pdf":
            return _parse_pdf(content)
        if ext == ".docx":
            _check_zip_bomb(content)
            return _parse_docx(content)
        if ext == ".pptx":
            _check_zip_bomb(content)
            return _parse_pptx(content)
        if ext == ".xlsx":
            _check_zip_bomb(content)
            return _parse_xlsx(content)
    except ParsingError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize any library-specific failure
        raise ParsingError(f"failed to parse {ext or 'file'}: {exc}") from exc

    raise ParsingError(f"unsupported file type: {ext or filename}")


def _parse_txt(content: bytes) -> list[ParsedSection]:
    text = content.decode("utf-8", errors="replace")
    return [ParsedSection(text=text)]


def _parse_markdown(content: bytes) -> list[ParsedSection]:
    text = content.decode("utf-8", errors="replace")
    sections: list[ParsedSection] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def flush():
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(ParsedSection(text=body, heading=current_heading))

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            current_heading = line.lstrip("#").strip() or None
            current_lines = []
        else:
            current_lines.append(line)
    flush()

    return sections or [ParsedSection(text=text)]


def _parse_html(content: bytes) -> list[ParsedSection]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    headings = soup.find_all(["h1", "h2", "h3"])
    if not headings:
        text = soup.get_text(separator="\n", strip=True)
        return [ParsedSection(text=text)] if text else []

    sections: list[ParsedSection] = []
    for heading in headings:
        heading_text = heading.get_text(strip=True)
        body_parts = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ("h1", "h2", "h3"):
                break
            body_parts.append(sibling.get_text(separator="\n", strip=True))
        body = "\n".join(p for p in body_parts if p)
        if body:
            sections.append(ParsedSection(text=body, heading=heading_text))
    return sections


def _parse_pdf(content: bytes) -> list[ParsedSection]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
    except PdfReadError as exc:
        raise ParsingError(f"corrupt PDF: {exc}") from exc

    if reader.is_encrypted:
        # Try an empty password (common for "restricted" rather than truly
        # encrypted PDFs); anything else is reported as password-protected.
        if reader.decrypt("") == 0:
            raise ParsingError("password-protected PDF")

    sections = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(ParsedSection(text=text, page_or_slide=i + 1))
    return sections


def _parse_docx(content: bytes) -> list[ParsedSection]:
    import docx

    document = docx.Document(io.BytesIO(content))
    sections: list[ParsedSection] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def flush():
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(ParsedSection(text=body, heading=current_heading))

    for paragraph in document.paragraphs:
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            flush()
            current_heading = paragraph.text.strip() or None
            current_lines = []
        elif paragraph.text.strip():
            current_lines.append(paragraph.text)
    flush()

    return sections


def _parse_pptx(content: bytes) -> list[ParsedSection]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(content))
    sections = []
    for i, slide in enumerate(presentation.slides):
        title_shape = slide.shapes.title
        title = title_shape.text.strip() if title_shape and title_shape.text else None
        texts = []
        for shape in slide.shapes:
            if shape is title_shape:
                continue  # already captured as the section heading
            if shape.has_text_frame and shape.text.strip():
                texts.append(shape.text.strip())
        # Fall back to the title as the body when a slide has no other text
        # (e.g. a section-divider slide) so it isn't dropped from the corpus.
        body = "\n".join(texts) or title or ""
        if body:
            sections.append(ParsedSection(text=body, heading=title, page_or_slide=i + 1))
    return sections


def _parse_xlsx(content: bytes) -> list[ParsedSection]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sections = []
    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                rows.append(" | ".join(cells))
        body = "\n".join(rows)
        if body:
            sections.append(ParsedSection(text=body, heading=sheet.title))
    return sections
