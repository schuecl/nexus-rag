#!/usr/bin/env python3
"""Issue #210 guard: fail if a Hugging Face model reference used at runtime
loses its revision pin. Image tags get this from check_pinned_images.py
(NFR-16); this is the same idea one layer down -- a mutable model id with no
pinned revision means a compromised or silently-updated upstream repo
changes the weights deciding retrieval order or reranking, in-cluster, with
no signal.

Deliberately narrow: this greps for the two known `MODEL_REVISION` constants
rather than trying to parse arbitrary model-loading code, the same way
check_pinned_images.py only understands `image:`/`FROM` lines. A new
unpinned model load elsewhere in the codebase won't be caught by this script
-- that's a gap, not a promise, same as the sibling check's Dockerfile/Compose
scope.

Exits 1 and prints every violation if any are found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each entry: (file relative to repo root, constant name, expected sha length).
# A revision must be a full 40-char commit SHA, not a mutable ref like "main"
# or a short/abbreviated hash that could collide across models.
_CHECKS = [
    ("services/reranker-service/app/main.py", "MODEL_REVISION"),
    ("services/common/common/sparse_embedding.py", "MODEL_REVISION"),
]

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_ASSIGNMENT = re.compile(r'MODEL_REVISION\s*=\s*os\.environ\.get\(\s*"[^"]+"\s*,\s*"([^"]*)"')


def _violation_in_source(source: str, origin: str, constant: str) -> str | None:
    match = _ASSIGNMENT.search(source)
    if not match:
        return f"{origin}: no default value found for {constant}"
    default = match.group(1)
    if not _FULL_SHA.match(default):
        return f"{origin}: {constant} default {default!r} is not a full 40-char commit SHA"
    return None


def _violation(rel_path: str, constant: str) -> str | None:
    path = REPO_ROOT / rel_path
    if not path.exists():
        return f"{rel_path}: file not found"
    return _violation_in_source(path.read_text(), rel_path, constant)


def main() -> int:
    problems = [p for rel_path, constant in _CHECKS if (p := _violation(rel_path, constant))]

    if problems:
        print("Issue #210 violation: unpinned model revision(s) found:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("All model references are pinned to a full commit SHA.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
