"""Coverage for issue #392: nomic-embed-text's required search_document:/
search_query: task prefixes, and their fold-in to the #122 stamped embedding
identity.
"""

from __future__ import annotations

from common.embedding_prefixes import document_prefix, embedding_identity, query_prefix


class TestPrefixLookup:
    def test_nomic_document_prefix(self):
        assert document_prefix("nomic-embed-text") == "search_document: "

    def test_nomic_query_prefix(self):
        assert query_prefix("nomic-embed-text") == "search_query: "

    def test_document_and_query_prefixes_differ(self):
        """The whole point of the scheme -- same call, different region of
        the embedding space depending on which side it's used for."""
        assert document_prefix("nomic-embed-text") != query_prefix("nomic-embed-text")

    def test_unrecognized_model_gets_no_prefix(self):
        """A configured EMBEDDING_MODEL this module doesn't recognize must
        fall back to today's behavior (no prefix), not guess at nomic's."""
        assert document_prefix("all-minilm") == ""
        assert query_prefix("all-minilm") == ""

    def test_nomic_v1_5_tag_variants_share_the_scheme(self):
        for name in ("nomic-embed-text:latest", "nomic-embed-text-v1.5", "nomic-embed-text:v1.5"):
            assert document_prefix(name) == "search_document: "
            assert query_prefix(name) == "search_query: "


class TestEmbeddingIdentity:
    def test_prefixed_model_identity_differs_from_bare_name(self):
        """The stamp/compare value must change when prefixing behavior
        changes under an unchanged model name, so a pre-#392 corpus is
        distinguishable from a post-#392 one."""
        assert embedding_identity("nomic-embed-text") != "nomic-embed-text"

    def test_unrecognized_model_identity_is_unchanged(self):
        assert embedding_identity("all-minilm") == "all-minilm"

    def test_identity_is_stable(self):
        assert embedding_identity("nomic-embed-text") == embedding_identity("nomic-embed-text")
