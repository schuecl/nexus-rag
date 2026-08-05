"""Issue #443: pins the two template-level properties a CSP with a
script-src nonce depends on, the same way test_hardening_215.py pins other
easy-to-silently-reintroduce properties by source shape rather than by
behavior a browser test would be needed to observe (see that file's own
docstring, and test_template_xss.py's for the same reasoning applied to the
#207 innerHTML fix).

1. No inline HTML event-handler attribute (onclick="..."): CSP's script-src
   nonce covers <script> elements, not attribute-based handlers -- a future
   `onclick="doThing()"` would silently fail in a browser enforcing this
   policy (fails open from the app's perspective: the button just does
   nothing), not raise anywhere a test framework would catch it.
2. Every literal `<script>` tag (i.e. not `<script src=...>`, which is
   already covered by script-src 'self') carries `nonce="{{ csp_nonce }}"` --
   an inline block without it is silently blocked the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

# Matches only inside a real opening tag (`<` immediately followed by a
# letter), so an explanatory code comment that happens to mention
# `onclick="..."` in prose -- e.g. curate.html's note on why a listener is
# used instead -- isn't mistaken for the attribute itself. HTML comments
# (`<!--`) and script bodies never match `<letter...` at their start.
_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")
_ON_ATTR = re.compile(r'\son[a-z]+="')
_INLINE_SCRIPT_TAG = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>")


def test_no_template_uses_an_inline_event_handler_attribute():
    offenders = []
    for path in TEMPLATES.glob("*.html"):
        for tag_match in _TAG.finditer(path.read_text()):
            tag = tag_match.group(0)
            if _ON_ATTR.search(tag):
                offenders.append(f"{path.name}: {tag.strip()[:80]}")

    assert offenders == []


def test_every_inline_script_tag_carries_the_csp_nonce():
    offenders = []
    for path in TEMPLATES.glob("*.html"):
        for tag in _INLINE_SCRIPT_TAG.findall(path.read_text()):
            if 'nonce="{{ csp_nonce }}"' not in tag:
                offenders.append(f"{path.name}: {tag}")

    assert offenders == []
