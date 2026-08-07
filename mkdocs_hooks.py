"""Build-time glue for the docs site (issue #561).

Two jobs, both existing so that NO committed markdown ever needs editing for
the site's sake -- the repo files stay the single source of truth and the
site is a rendering of them:

1. **Root-file injection.** ARCHITECTURE.md, REQUIREMENTS.md, CHANGELOG.md,
   CONTRIBUTING.md and SECURITY.md live at the repo root, outside
   ``docs_dir``, where MkDocs cannot see them. Each has a one-line stub page
   under ``docs/``; ``on_page_markdown`` swaps the stub's content for the
   root file, read byte-identical at build time. (pymdownx.snippets cannot do
   this: snippet expansion runs during markdown conversion, *after* this
   hook, so snippet-included text would escape the link rewriting below.)

2. **Link rewriting.** The repo's docs link to each other in GitHub-browse
   terms: ``../REQUIREMENTS.md`` (up and out of docs_dir), ``docs/testing.md``
   (root files pointing down into docs/), ``../scripts/evaluate_retrieval.py``
   (into source code). All three classes break under MkDocs. Every markdown
   link is resolved against the page's location *in the repo*, then:

   - resolves under ``docs/``      -> page-relative link (MkDocs validates it)
   - resolves to an injected root file -> that file's stub page
   - resolves to any other file that exists in the repo -> ``repo_url`` blob
     link (source code is browsed on the forge, not rendered in the site)
   - http(s)/mailto/pure-#anchor    -> untouched
   - anything unresolvable          -> untouched, so ``mkdocs build --strict``
     flags it instead of this hook silently eating a typo

   ``#anchor`` suffixes are preserved as-is: python-markdown's ``toc`` slugs
   match GitHub's for every anchor currently used in the corpus (verified at
   introduction; a future divergence shows up as a --strict warning).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# stub page (src_uri under docs/) -> repo-root file injected into it
STUBS = {
    "architecture-overview.md": "ARCHITECTURE.md",
    "requirements.md": "REQUIREMENTS.md",
    "changelog.md": "CHANGELOG.md",
    "contributing.md": "CONTRIBUTING.md",
    "security.md": "SECURITY.md",
}
ROOT_FILE_TO_STUB = {v: k for k, v in STUBS.items()}

# [text](target "optional title") -- target captured up to whitespace or ')'
_LINK_RE = re.compile(r"(\[[^\]]*\]\()([^)\s]+)([^)]*\))")
_EXTERNAL = ("http://", "https://", "mailto:", "#")


def _split_anchor(target: str) -> tuple[str, str]:
    if "#" in target:
        path, anchor = target.split("#", 1)
        return path, "#" + anchor
    return target, ""


def on_page_markdown(markdown: str, page, config, files) -> str:
    src_uri = page.file.src_uri  # e.g. "nist-ai-rmf/README.md"

    if src_uri in STUBS:
        markdown = (ROOT / STUBS[src_uri]).read_text(encoding="utf-8")
        # Links inside a root file are written relative to the repo root.
        page_repo_dir = ""
    else:
        page_repo_dir = posixpath.join("docs", posixpath.dirname(src_uri))

    page_site_dir = posixpath.dirname(src_uri)

    def _rewrite(match: re.Match) -> str:
        raw_target = match.group(2)
        if raw_target.startswith(_EXTERNAL) or not raw_target:
            return match.group(0)
        path_part, anchor = _split_anchor(raw_target)
        if not path_part:  # pure in-page anchor
            return match.group(0)

        repo_path = posixpath.normpath(posixpath.join(page_repo_dir, path_part))
        if repo_path.startswith(".."):
            return match.group(0)

        if repo_path.startswith("docs/"):
            new_target = posixpath.relpath(repo_path[len("docs/") :], page_site_dir or ".")
        elif repo_path in ROOT_FILE_TO_STUB:
            new_target = posixpath.relpath(ROOT_FILE_TO_STUB[repo_path], page_site_dir or ".")
        elif (ROOT / repo_path).exists():
            new_target = f"{config['repo_url']}/blob/main/{repo_path}"
        else:
            return match.group(0)

        return match.group(1) + new_target + anchor + match.group(3)

    return _LINK_RE.sub(_rewrite, markdown)
