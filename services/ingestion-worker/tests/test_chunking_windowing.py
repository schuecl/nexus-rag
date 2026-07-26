"""Unit tests for the ingestion-worker's chunking (FR-4): section-boundary
respect, target size, and overlap -- the parameters retrieval quality depends
on most directly.
"""

from __future__ import annotations

import itertools

from app.chunking import chunk_sections
from app.parsing import ParsedSection


def _section(words: int, **kwargs) -> ParsedSection:
    return ParsedSection(text=" ".join(f"w{i}" for i in range(words)), **kwargs)


class TestChunkSections:
    def test_short_section_yields_single_chunk(self):
        chunks = chunk_sections([_section(100, heading="Intro")], target_words=512)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].heading == "Intro"
        assert len(chunks[0].text.split()) == 100

    def test_long_section_splits_at_target_with_overlap(self):
        chunks = chunk_sections([_section(1200)], target_words=512, overlap_ratio=0.15)
        assert len(chunks) == 3  # 512 + 512 + 328 (trailing window)
        for chunk in chunks[:-1]:
            assert len(chunk.text.split()) == 512
        # 15% overlap: each chunk after the first starts by repeating the tail
        # of the previous one.
        overlap = int(512 * 0.15)
        for prev, curr in itertools.pairwise(chunks):
            assert curr.text.split()[:overlap] == prev.text.split()[-overlap:]

    def test_never_crosses_section_boundaries(self):
        sections = [
            _section(600, heading="A", page_or_slide=1),
            _section(600, heading="B", page_or_slide=2),
        ]
        chunks = chunk_sections(sections, target_words=512)
        headings = {c.heading for c in chunks}
        assert headings == {"A", "B"}
        for chunk in chunks:
            words = chunk.text.split()
            # Words are w0..w599 per section; a cross-boundary chunk would mix
            # headings, which the per-chunk heading tag makes detectable.
            assert all(w.startswith("w") for w in words)
        assert all(c.page_or_slide == 1 for c in chunks if c.heading == "A")
        assert all(c.page_or_slide == 2 for c in chunks if c.heading == "B")

    def test_empty_sections_skipped(self):
        sections = [ParsedSection(text="   "), _section(50)]
        chunks = chunk_sections(sections, target_words=512)
        assert len(chunks) == 1

    def test_no_sections_no_chunks(self):
        assert chunk_sections([], target_words=512) == []

    def test_chunk_indices_are_sequential(self):
        chunks = chunk_sections([_section(2000)], target_words=512)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_overlap_ratio_zero_disables_overlap(self):
        chunks = chunk_sections([_section(1024)], target_words=512, overlap_ratio=0.0)
        assert len(chunks) == 2
        assert chunks[1].text.split()[0] != chunks[0].text.split()[-1]

    def test_exact_multiple_of_target(self):
        # With overlap, an exact multiple still yields a short trailing chunk
        # (overlap tail + the final uncovered words) -- asserted so a change
        # here is deliberate.
        chunks = chunk_sections([_section(1024)], target_words=512)
        assert [len(c.text.split()) for c in chunks] == [512, 512, 152]
