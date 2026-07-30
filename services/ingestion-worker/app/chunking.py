"""FR-4: chunk parsed text respecting document structure rather than pure
fixed-token splitting -- chunking never crosses a ParsedSection boundary
(heading/page/slide), and within a section applies a sliding window with the
Section 2 starting point of ~512 tokens, ~15% overlap.

Simplification: "tokens" here are approximated by whitespace-split words, not
a model-specific tokenizer -- close enough for a target chunk size and cheap
to compute without pulling in a tokenizer dependency. Revisit if chunk sizes
need to track the embedding model's actual token count precisely.

issue #90: a section's content_type marks whether its text is an atomic
block that must never be cut by the sliding window -- "table" (a markdown
table from `_table_to_markdown`, or a spreadsheet sheet; see parsing.py) and
"image" (a figure caption, app/captioning.py; captions are short, so
atomicity costs nothing and keeps one caption one chunk). A
table section is emitted as a single chunk even when it's longer than
target_words -- splitting it mid-row would scatter a row's fields across two
separate embedded chunks, which is worse than one oversized chunk.

A real CIS benchmark PDF surfaced the limit of that tradeoff: a single table
section rendered to ~4000 tokens, over the embedding backend's hard batch-size
ceiling, so the whole document failed with no way to embed that chunk at all.
Past a much larger row-group threshold (target_words is still the right size
for prose, but far too small a trigger for this), an oversized table is now
split into several chunks by whole rows -- each repeating the header/separator
line so it stays independently readable -- rather than left as one
unembeddable blob. A row is still never split mid-row.

The same PDF also broke the word-count approximation itself, in a "text"
section: its Table of Contents is full of PDF dot-leader lines ("Overview
.......................... 7"), and a run of a hundred-plus dots with no
spaces is exactly *one* whitespace-split "word" -- so a 185-word chunk looked
tiny, yet tokenized to ~4000 real tokens, the same failure as the oversized
table but hitting prose instead. Dot leaders (and any other degenerate run of
one repeated character) carry no retrieval-relevant content anyway, so
`_collapse_repeated_chars` below flattens any such run to 3 characters before
sizing runs on the text at all, rather than trying to out-guess the
word-count heuristic for content that's really just visual padding.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from app.parsing import ParsedSection

# FR-4: "configurable target chunk size and overlap" -- these were hardcoded
# constants with no way to change them short of editing code; now read from
# the environment, with the same Section 2 starting-point values as defaults.
DEFAULT_TARGET_WORDS = int(os.environ.get("CHUNK_TARGET_WORDS", "512"))
DEFAULT_OVERLAP_RATIO = float(os.environ.get("CHUNK_OVERLAP_RATIO", "0.15"))
# A table section is kept atomic up to this size -- much larger than
# target_words, since a table is only split as a last resort. Default chosen
# to stay well under a typical embedding backend's batch-size ceiling (e.g.
# Ollama's default 2048-token physical batch) even accounting for
# ID/number-heavy cell text tokenizing worse than the word-count approximation.
DEFAULT_TABLE_MAX_WORDS = int(os.environ.get("CHUNK_TABLE_MAX_WORDS", "700"))

# A run of the same non-whitespace character repeated 4+ times in a row --
# almost always a PDF dot-leader or similar visual filler, never real prose
# (English words essentially never repeat a character 4+ times running).
_REPEATED_CHAR_RUN = re.compile(r"(\S)\1{3,}")

# Content types whose sections are never cut by the sliding window: "table"
# (issue #90) and "image" (a #92 figure caption, short by construction).
# Deliberately NOT "ocr" -- see the membership check below.
_ATOMIC_CONTENT_TYPES = frozenset({"table", "image"})


def _collapse_repeated_chars(text: str) -> str:
    return _REPEATED_CHAR_RUN.sub(lambda m: m.group(1) * 3, text)


@dataclass
class Chunk:
    text: str
    chunk_index: int
    heading: str | None = None
    page_or_slide: int | None = None
    # issue #89: inherited straight from the ParsedSection this chunk was cut
    # from ("text" or "table") -- chunking never crosses a section boundary,
    # so a chunk's content_type is always exactly its source section's, no
    # per-chunk detection needed.
    content_type: str = "text"


def _split_table_rows(text: str, max_words: int) -> list[str]:
    """Split an oversized markdown table into row-group chunks, repeating the
    header/separator line on each so every chunk stays independently
    readable. Never splits a row itself -- a single row wider than max_words
    just becomes its own oversized chunk, no worse than the pre-split status
    quo for that one row."""
    lines = text.split("\n")
    if len(lines) < 3:
        return [text]

    header, separator, *rows = lines
    prefix_words = len(header.split()) + len(separator.split())

    groups: list[list[str]] = []
    current: list[str] = []
    current_words = prefix_words
    for row in rows:
        row_words = len(row.split())
        if current and current_words + row_words > max_words:
            groups.append(current)
            current = []
            current_words = prefix_words
        current.append(row)
        current_words += row_words
    if current:
        groups.append(current)

    return ["\n".join([header, separator, *group]) for group in groups]


def chunk_sections(
    sections: list[ParsedSection],
    *,
    target_words: int = DEFAULT_TARGET_WORDS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    table_max_words: int = DEFAULT_TABLE_MAX_WORDS,
) -> list[Chunk]:
    overlap = int(target_words * overlap_ratio)
    chunks: list[Chunk] = []

    for section in sections:
        text = _collapse_repeated_chars(section.text.strip())
        if not text:
            continue

        # issue #90: atomic sections are kept whole, regardless of
        # target_words -- the sliding window below only ever runs on prose,
        # so it can no longer land a cut inside a table row. Past
        # table_max_words -- a much higher bar than target_words, since this
        # is a last resort -- an oversized table is split by whole rows
        # instead, so it can still be embedded at all.
        #
        # issue #241: membership is an explicit set, not `!= "text"`, because
        # not every non-prose content_type is atomic: "ocr" (a scanned page's
        # recognized text, parsing.py) *is* prose and must flow through the
        # sliding window below -- a 500-word scanned page as one atomic chunk
        # would recreate exactly the oversized-chunk failure #114 fixed.
        if section.content_type in _ATOMIC_CONTENT_TYPES:
            table_texts = (
                _split_table_rows(text, table_max_words)
                if section.content_type == "table" and len(text.split()) > table_max_words
                else [text]
            )
            for table_text in table_texts:
                chunks.append(
                    Chunk(
                        text=table_text,
                        chunk_index=len(chunks),
                        heading=section.heading,
                        page_or_slide=section.page_or_slide,
                        content_type=section.content_type,
                    )
                )
            continue

        words = text.split()
        start = 0
        while True:
            end = min(start + target_words, len(words))
            chunks.append(
                Chunk(
                    text=" ".join(words[start:end]),
                    chunk_index=len(chunks),
                    heading=section.heading,
                    page_or_slide=section.page_or_slide,
                    content_type=section.content_type,
                )
            )
            if end == len(words):
                break
            start = end - overlap

    return chunks
