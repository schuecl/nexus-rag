"""FR-4: chunk parsed text respecting document structure rather than pure
fixed-token splitting -- chunking never crosses a ParsedSection boundary
(heading/page/slide), and within a section applies a sliding window with the
Section 2 starting point of ~512 tokens, ~15% overlap.

Simplification: "tokens" here are approximated by whitespace-split words, not
a model-specific tokenizer -- close enough for a target chunk size and cheap
to compute without pulling in a tokenizer dependency. Revisit if chunk sizes
need to track the embedding model's actual token count precisely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.parsing import ParsedSection

# FR-4: "configurable target chunk size and overlap" -- these were hardcoded
# constants with no way to change them short of editing code; now read from
# the environment, with the same Section 2 starting-point values as defaults.
DEFAULT_TARGET_WORDS = int(os.environ.get("CHUNK_TARGET_WORDS", 512))
DEFAULT_OVERLAP_RATIO = float(os.environ.get("CHUNK_OVERLAP_RATIO", 0.15))


@dataclass
class Chunk:
    text: str
    chunk_index: int
    heading: str | None = None
    page_or_slide: int | None = None


def chunk_sections(
    sections: list[ParsedSection],
    *,
    target_words: int = DEFAULT_TARGET_WORDS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[Chunk]:
    overlap = int(target_words * overlap_ratio)
    chunks: list[Chunk] = []

    for section in sections:
        words = section.text.split()
        if not words:
            continue

        start = 0
        while True:
            end = min(start + target_words, len(words))
            chunks.append(
                Chunk(
                    text=" ".join(words[start:end]),
                    chunk_index=len(chunks),
                    heading=section.heading,
                    page_or_slide=section.page_or_slide,
                )
            )
            if end == len(words):
                break
            start = end - overlap

    return chunks
