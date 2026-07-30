"""Issue #207: the curation and notification pages must not build rows from
interpolated HTML.

Why this is a source-shape test rather than a browser test
----------------------------------------------------------
The vulnerability lived entirely in client-side JavaScript: values arrive as
JSON from `fetch()` and were written into `innerHTML`, so Jinja's autoescaping
never saw them and no server-side assertion could observe the bug. Proving the
fix properly needs a browser driving a real session, which is the gap #187
already tracks and is a much larger piece of infrastructure.

What this file does instead is pin the *shape* that made it exploitable: no
dynamic `innerHTML` assignment, and no inline event handler carrying an
interpolated value. Those two patterns are what turned an uploader-chosen
filename into script running with a curator's authority -- and they are what a
future edit would most plausibly reintroduce, because a template literal is the
shorter thing to write.

A test that asserts "the escaping helper is called on these five fields" would
pass the day a sixth field is added. Asserting the sink is absent does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

# Pages that render values originating from user input (uploader filenames,
# curator rejection reasons) fetched as JSON.
# Issue #266: curate_list.html (the curation "master list") renders the same
# uploader-controlled document fields as curate.html, plus a curator-editable
# metadata form -- same sink to avoid.
USER_DATA_PAGES = ["curate.html", "curate_list.html", "notifications.html"]

# `x.innerHTML = ''` is a clear, and the idiomatic way to empty a container --
# it can't introduce markup. Anything else assigned to innerHTML is the sink.
_INNERHTML_CLEAR = re.compile(r"innerHTML\s*=\s*(''|\"\"|``)\s*;")
_INNERHTML_ANY = re.compile(r"\.innerHTML\s*=")

# onclick="approve('${doc.id}')" -- an interpolated value inside an inline
# handler is a second parsed context, distinct from the innerHTML sink.
_INLINE_HANDLER_WITH_INTERPOLATION = re.compile(r"on[a-z]+\s*=\s*[\"'][^\"']*\$\{")


def _read(name: str) -> str:
    return (TEMPLATES / name).read_text()


@pytest.mark.parametrize("page", USER_DATA_PAGES)
class TestNoMarkupSinks:
    def test_no_dynamic_innerhtml_assignment(self, page):
        source = _read(page)
        assignments = _INNERHTML_ANY.findall(source)
        clears = _INNERHTML_CLEAR.findall(source)

        assert len(assignments) == len(clears), (
            f"{page} assigns something other than an empty string to innerHTML. "
            "Build rows with createElement/textContent instead -- a value "
            "reaching innerHTML is parsed as markup, and every value on these "
            "pages is user-controlled (issue #207)."
        )

    def test_no_interpolated_inline_event_handlers(self, page):
        source = _read(page)

        assert not _INLINE_HANDLER_WITH_INTERPOLATION.search(source), (
            f"{page} interpolates a value into an inline event handler. Use "
            "addEventListener with a closure over the value instead."
        )


class TestTheFieldsThatCarriedThePayload:
    """Named explicitly so the regression is legible: these are the values an
    uploader controls, and each one previously reached a parsed context."""

    def test_filename_is_not_interpolated_into_markup(self):
        source = _read("curate.html")

        # doc.filename comes straight from upload.py's `file.filename`.
        assert "${doc.filename}" not in source
        assert "text: doc.filename" in source, "the filename should be rendered through textContent"

    def test_access_scope_uses_the_value_property_not_an_attribute(self):
        source = _read("curate.html")

        # value="${...}" re-parses inside the attribute; .value = ... does not.
        assert 'value="${' not in source
        assert "accessScope.value = doc.access_scope.join(', ')" in source

    def test_notification_message_is_appended_as_text(self):
        source = _read("notifications.html")

        # n.message embeds the filename and the curator's rejection reason.
        assert "${n.message}" not in source
        assert "n.message" in source


class TestCorrectPagesAreUnchanged:
    """upload.html and search.html already did this correctly and are the
    reference for what the two fixed pages now do."""

    @pytest.mark.parametrize("page", ["upload.html", "search.html"])
    def test_reference_pages_have_no_markup_sink(self, page):
        source = _read(page)

        assert not _INNERHTML_ANY.search(source), f"{page} regressed"
