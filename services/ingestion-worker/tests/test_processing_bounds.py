"""Issue #208: a document cannot occupy a worker indefinitely.

Two bounds, deliberately complementary:

- MAX_PDF_PAGES rejects one specific amplification shape (a small file
  declaring an enormous page count) precisely and cheaply, before pdfplumber
  does any per-page geometry work, so the uploader gets an actionable FR-8
  message.
- PROCESSING_TIMEOUT_SECONDS is the catch-all for everything else: a single
  pathological page, a slow embedding pass, a format nobody anticipated.

The timeout is treated as a *permanent* failure. That is the load-bearing
decision here and the one worth pinning: the same bytes on the same hardware
take the same time on redelivery, so retrying spends the budget again and
delays the outcome the uploader is waiting for.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import parsing, processing


def _source(module) -> str:
    with open(module.__file__.replace(".pyc", ".py")) as handle:
        return handle.read()


class TestPdfPageBound:
    def test_the_limit_is_above_any_real_document(self):
        # Tight enough to protect, loose enough not to reject real corpora --
        # the largest DoD policy PDFs run to the low hundreds of pages.
        assert parsing.MAX_PDF_PAGES >= 1000

    def test_the_guard_runs_before_pdfplumber_opens_the_file(self):
        """The whole point is not doing per-page work to discover there is too
        much per-page work. Asserted on source order because the alternative is
        constructing a genuinely malicious PDF in a unit test."""
        body = _source(parsing).split("def _parse_pdf")[1]

        guard = body.index("MAX_PDF_PAGES")
        open_call = body.index("pdfplumber.open")

        assert guard < open_call, "page-count check must precede pdfplumber.open"


class TestProcessingTimeout:
    def test_budget_is_below_the_ack_wait(self):
        """If the budget exceeded ack_wait, JetStream would start a second
        attempt while the first was still inside its own budget -- two workers
        grinding the same input, which is the situation this exists to avoid.
        """
        assert processing.PROCESSING_TIMEOUT_SECONDS < processing.ACK_WAIT_SECONDS

    def test_timeout_is_classified_as_permanent_not_transient(self):
        """TimeoutError must sit in the same except clause as
        ParsingError/EmbeddingError. In the transient branch instead, a
        too-slow document would be redelivered until MAX_DELIVERY_ATTEMPTS,
        spending the budget every time and delaying the FR-8 outcome."""
        assert "except (ParsingError, EmbeddingError, TimeoutError) as exc:" in _source(processing)

    async def test_asyncio_timeout_actually_fires_on_a_blocking_stage(self):
        """The reason parse/chunk moved to asyncio.to_thread: a synchronous
        call on the event loop has no await point, so a timeout around it can
        never fire. This asserts the mechanism, not the production wiring.
        """

        def _slow_sync_parse():
            time.sleep(5)
            return "never"

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await asyncio.to_thread(_slow_sync_parse)

    def test_cpu_bound_stages_are_off_the_event_loop(self):
        """Blocking the loop stops /health answering during a long parse
        (#109's whole point) as well as defeating the timeout."""
        source = _source(processing)

        assert "await asyncio.to_thread(parse_document" in source
        assert "await asyncio.to_thread(chunk_sections" in source
