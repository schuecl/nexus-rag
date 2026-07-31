"""Builds the mandatory, non-bypassable Qdrant payload filter from a user's
verified claims (REQUIREMENTS.md Section 6.1 / FR-26). The client never supplies
any part of this filter -- it is derived entirely server-side from `UserClaims`
plus the admin-configured classification ranking, then injected into every query
before it reaches Qdrant's HNSW search.
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from .claims import UserClaims
from .metadata import NO_RELEASABILITY_RESTRICTION, access_scope_values


def build_access_filter(
    claims: UserClaims,
    *,
    allowed_classifications: list[str],
) -> Filter:
    """allowed_classifications: every Classification value at or below the
    user's `clearance`, per the admin-configured rank order (Section 6.3) --
    computed by the caller from ClassificationLevel rows, not here, since this
    module has no DB session."""

    scope_values = access_scope_values(sub=claims.sub, groups=claims.groups, org=claims.org)

    return Filter(
        must=[
            FieldCondition(key="status", match=MatchValue(value="approved")),
            FieldCondition(key="classification", match=MatchAny(any=allowed_classifications)),
            FieldCondition(
                key="releasability",
                match=MatchAny(any=[NO_RELEASABILITY_RESTRICTION, *claims.releasability]),
            ),
            FieldCondition(key="access_scope", match=MatchAny(any=sorted(scope_values))),
        ]
    )
