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

from functools import lru_cache

from qdrant_client.models import SparseVector

MODEL_NAME = "Qdrant/bm25"


@lru_cache(maxsize=1)
def _model():
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(model_name=MODEL_NAME)


def embed_sparse(texts: list[str]) -> list[SparseVector]:
    if not texts:
        return []
    return [
        SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
        for e in _model().embed(texts)
    ]
