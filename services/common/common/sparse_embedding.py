"""FR-24: BM25 sparse embeddings for hybrid retrieval, generated with Qdrant's
own fastembed library (the `Qdrant/bm25` model) -- Apache-2.0, Qdrant-
maintained, satisfies C1/C2. This produces raw term-frequency sparse vectors;
the actual IDF weighting that makes this real BM25 (not just term counts) is
applied by Qdrant server-side via the sparse vector field's `Modifier.IDF`
(see qdrant_store.ensure_collection), not baked in here.

The model is instantiated lazily on first use, not at import time, so
importing this module never triggers a network call.
"""

from __future__ import annotations

import os
from functools import lru_cache

from qdrant_client.models import SparseVector

MODEL_NAME = "Qdrant/bm25"

# #210: fastembed forwards unknown kwargs straight through to
# huggingface_hub.snapshot_download, so `revision=` here pins the download
# the same way `revision=` pins sentence-transformers in reranker-service.
# Env-overridable to match RERANKER_MODEL_REVISION's pattern, but a mutable
# override still has to be an explicit, deliberate act rather than the
# silent default drift this pin exists to prevent.
MODEL_REVISION = os.environ.get("BM25_MODEL_REVISION", "e499a1f8d6bec960aab5533a0941bf914e70faf9")


@lru_cache(maxsize=1)
def _model():
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=MODEL_NAME, revision=MODEL_REVISION)


def embed_sparse(texts: list[str]) -> list[SparseVector]:
    if not texts:
        return []
    return [
        SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
        for e in _model().embed(texts)
    ]
