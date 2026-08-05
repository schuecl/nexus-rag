"""Issue #392: `nomic-embed-text` (and other asymmetric embedding models) are
trained with required task-instruction prefixes -- `search_document: ` for
indexed passages, `search_query: ` for queries -- that put document and query
vectors in deliberately different regions of the embedding space. Sending
both sides unprefixed doesn't error or look broken: the vectors are still
valid and cosine similarity still ranks, so nothing catches it. Retrieval is
just quietly worse than the model can do.

Prefixes are model-specific, so they're looked up by `EMBEDDING_MODEL` here
rather than hardcoded at either call site (ingestion-worker's `embed_texts`,
orchestration-mcp's `_embed_query`) -- those two call sites must agree on the
scheme, and a configured model this module doesn't recognize falls back to no
prefix (today's behavior) rather than guessing at one that would corrupt its
embeddings.
"""

from __future__ import annotations

# EMBEDDING_MODEL -> (document_prefix, query_prefix). Both nomic-embed-text
# v1 and v1.5 use the same task-instruction prefixes (model card).
_PREFIX_SCHEMES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "nomic-embed-text:latest": ("search_document: ", "search_query: "),
    "nomic-embed-text-v1.5": ("search_document: ", "search_query: "),
    "nomic-embed-text:v1.5": ("search_document: ", "search_query: "),
}

# Suffix folded into the #122 stamped embedding identity when a model has a
# prefix scheme. Keeps a pre-#392 corpus (points embedded without a prefix)
# distinguishable from a post-#392 one under the *same* EMBEDDING_MODEL name,
# so the #122 mismatch check refuses the mixed-space case instead of silently
# comparing prefixed queries against unprefixed passage vectors.
_PREFIXED_IDENTITY_SUFFIX = "+prefixed"


def document_prefix(model: str) -> str:
    """Task-instruction prefix to prepend to text embedded as a passage, or
    `""` if `model` has no known prefix scheme."""
    return _PREFIX_SCHEMES.get(model, ("", ""))[0]


def query_prefix(model: str) -> str:
    """Task-instruction prefix to prepend to text embedded as a query, or
    `""` if `model` has no known prefix scheme."""
    return _PREFIX_SCHEMES.get(model, ("", ""))[1]


def embedding_identity(model: str) -> str:
    """The value stamped into (ingestion) and compared against (query time)
    a chunk's `embedding_model` payload field -- issue #122's mismatch check,
    extended by #392 to also change when prefixing behavior changes under an
    unchanged model name. See module docstring for why that matters."""
    if model in _PREFIX_SCHEMES:
        return model + _PREFIXED_IDENTITY_SUFFIX
    return model
