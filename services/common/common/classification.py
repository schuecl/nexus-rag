"""Shared helper for resolving "at or below the user's cleared level" (FR-18,
FR-26) against the admin-configurable ranked list (C9). Used by both
ingestion-api (to constrain the tagging dropdown/enforcement) and
orchestration-mcp (to build the query-time filter) so the comparison logic
lives in exactly one place."""

from __future__ import annotations

from sqlmodel import Session, select

from .models import ClassificationLevel


def allowed_classifications(session: Session, clearance: str) -> list[str]:
    user_level = session.exec(
        select(ClassificationLevel).where(ClassificationLevel.value == clearance)
    ).first()
    if user_level is None:
        return []
    rows = session.exec(
        select(ClassificationLevel)
        .where(ClassificationLevel.rank <= user_level.rank)
        .where(ClassificationLevel.active == True)  # noqa: E712
    ).all()
    return [row.value for row in rows]
