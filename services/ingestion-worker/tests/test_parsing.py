"""Regression coverage for issue #88: PDF/DOCX table extraction used to fall
through to plain paragraph/page text extraction, which flattens a table's
rows and columns into an unstructured word sequence -- e.g. a table with
columns Name/Role/Clearance and a row Alice/Curator/Secret came out as
"Name Role Clearance Alice Curator Secret", with no way to tell which word
belonged to which cell or row. `app.parsing` now detects tables separately
and renders them as their own markdown block instead.

Also covers issue #89: since #88, each table markdown block is its own
ParsedSection (content_type="table") rather than being joined into the
surrounding prose's section -- prose keeps content_type="text". This is what
lets chunking (app/chunking.py) tag every chunk with a single, accurate
content_type instead of guessing from mixed content.

Fixtures (`tests/fixtures/*.pdf`, `*.docx`) are small synthetic documents
committed as binary files rather than generated at test time, so this suite
doesn't need reportlab as a test dependency just to build them.
"""

from pathlib import Path

import pytest

from app.parsing import ParsingError, parse_document

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_pdf_table_extracted_as_markdown_not_flattened_into_prose():
    sections = parse_document("table.pdf", _read("table.pdf"))

    # issue #89: prose and the table are now separate sections rather than
    # one joined blob.
    assert len(sections) == 2
    prose_section, table_section = sections
    assert prose_section.content_type == "text"
    assert table_section.content_type == "table"

    # The table's own markdown block is present, with cells in their
    # original row/column positions...
    assert "| Name | Role | Clearance |" in table_section.text
    assert "| Alice | Curator | Secret |" in table_section.text
    assert "| Bob | Analyst | Top Secret |" in table_section.text

    # ...and the surrounding prose is intact and not interleaved with cell
    # text (the pre-fix behavior flattened the table into the middle of the
    # sentence stream with no separators).
    assert "Intro paragraph before the table describing context." in prose_section.text
    assert "Closing paragraph after the table with more context." in prose_section.text

    # The old bug's signature: table cells word-joined straight into prose
    # with no delimiter at all.
    assert "|" not in prose_section.text


def test_pdf_page_number_preserved_on_a_page_with_a_table():
    sections = parse_document("table.pdf", _read("table.pdf"))
    assert all(s.page_or_slide == 1 for s in sections)


def test_pdf_without_any_table_is_unaffected():
    sections = parse_document("prose_only.pdf", _read("prose_only.pdf"))
    assert len(sections) == 1
    assert sections[0].content_type == "text"
    assert "no tables at all" in sections[0].text
    assert "|" not in sections[0].text


def test_docx_table_extracted_as_markdown_in_its_own_section():
    sections = parse_document("table.docx", _read("table.docx"))

    # issue #89: "Section One" now yields three sections in document order
    # (intro prose, table, closing prose) instead of one joined blob, so
    # each carries a single accurate content_type; "Section Two" is
    # untouched prose.
    assert [(s.heading, s.content_type) for s in sections] == [
        ("Section One", "text"),
        ("Section One", "table"),
        ("Section One", "text"),
        ("Section Two", "text"),
    ]
    intro_section, table_section, closing_section, _ = sections

    assert "Intro paragraph before the table." in intro_section.text
    assert "| Name | Role | Clearance |" in table_section.text
    assert "| Alice | Curator | Secret |" in table_section.text
    assert "Closing paragraph after the table." in closing_section.text


def test_xlsx_sheet_is_tagged_as_table_content_type():
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Pricing"
    sheet.append(["Item", "Price"])
    sheet.append(["Widget", "9.99"])
    buf = io.BytesIO()
    workbook.save(buf)

    sections = parse_document("pricing.xlsx", buf.getvalue())

    assert len(sections) == 1
    assert sections[0].content_type == "table"
    assert sections[0].heading == "Pricing"


def test_txt_and_markdown_default_to_text_content_type():
    assert parse_document("notes.txt", b"just some prose")[0].content_type == "text"
    for section in parse_document("notes.md", b"# Heading\nsome prose"):
        assert section.content_type == "text"


@pytest.mark.parametrize(
    "filename,content,expected_message_fragment",
    [
        ("empty.txt", b"", "empty file"),
        ("corrupt.pdf", b"not a pdf", "corrupt PDF"),
        ("unsupported.exe", b"stuff", "unsupported file type"),
    ],
)
def test_existing_error_paths_unaffected(filename, content, expected_message_fragment):
    with pytest.raises(ParsingError, match=expected_message_fragment):
        parse_document(filename, content)


def test_table_to_markdown_drops_fully_empty_rows():
    from app.parsing import _table_to_markdown

    grid = [["a", "b"], [None, None], ["c", "d"]]
    markdown = _table_to_markdown(grid)
    assert markdown == "| a | b |\n| --- | --- |\n| c | d |"


def test_table_to_markdown_empty_grid_returns_empty_string():
    from app.parsing import _table_to_markdown

    assert _table_to_markdown([]) == ""
    assert _table_to_markdown([[None, None]]) == ""
