"""Issue #210: the guard that keeps reranker/BM25 model revisions pinned.

Same spirit as test_check_compose_hardening.py -- pin down that the check
actually fails on the shape it exists to reject, not just that the current
source files happen to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_pinned_models import _violation_in_source


def _source(default: str) -> str:
    return f'MODEL_REVISION = os.environ.get(\n    "SOME_MODEL_REVISION", "{default}"\n)'


class TestPinnedModelCheck:
    def test_a_full_commit_sha_passes(self):
        source = _source("c5ee24cb16019beea0893ab7796b1df96625c6b8")

        assert _violation_in_source(source, "x", "MODEL_REVISION") is None

    def test_a_missing_default_is_reported(self):
        source = 'MODEL_REVISION = os.environ.get("X")'
        problem = _violation_in_source(source, "x", "MODEL_REVISION")

        assert problem is not None
        assert "no default value" in problem

    def test_a_mutable_ref_is_rejected(self):
        problem = _violation_in_source(_source("main"), "x", "MODEL_REVISION")

        assert problem is not None
        assert "not a full 40-char commit SHA" in problem

    def test_an_abbreviated_sha_is_rejected(self):
        problem = _violation_in_source(_source("c5ee24c"), "x", "MODEL_REVISION")

        assert problem is not None
        assert "not a full 40-char commit SHA" in problem

    def test_an_empty_default_is_rejected(self):
        problem = _violation_in_source(_source(""), "x", "MODEL_REVISION")

        assert problem is not None
        assert "not a full 40-char commit SHA" in problem
