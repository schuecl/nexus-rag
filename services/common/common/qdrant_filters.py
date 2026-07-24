"""Builds the mandatory, non-bypassable Qdrant payload filter from a user's
verified claims (REQUIREMENTS.md Section 6.1 / FR-26). The client never supplies
any part of this filter -- it is derived entirely server-side from `UserClaims`
plus the admin-configured classification ranking, then injected into every query
before it reaches Qdrant's HNSW search.
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from .claims import UserClaims
from .metadata import ALL_AUTHENTICATED_ACCESS_SCOPE


def build_access_filter(
    claims: UserClaims,
    *,
    allowed_classifications: list[str],
) -> Filter:
    """allowed_classifications: every Classification value at or below the
    user's `clearance`, per the admin-configured rank order (Section 6.3) --
    computed by the caller from ClassificationLevel rows, not here, since this
    module has no DB session."""

    scope_values = {ALL_AUTHENTICATED_ACCESS_SCOPE, claims.sub, *claims.groups}
    if claims.org:
        scope_values.add(claims.org)
    scope_values = list(scope_values)

    return Filter(
        must=[
            FieldCondition(key="status", match=MatchValue(value="approved")),
            FieldCondition(
                key="classification", match=MatchAny(any=allowed_classifications)
            ),
            FieldCondition(
                key="releasability", match=MatchAny(any=claims.releasability)
            ),
            FieldCondition(key="access_scope", match=MatchAny(any=scope_values)),
        ]
    )
