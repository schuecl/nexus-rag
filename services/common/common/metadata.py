"""Section 6.3 metadata schema: the fields a document carries, and validation of
the subset an uploader is allowed to submit given their claims (FR-18)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# FR-23: reserved access-scope value that waives Org/Group/User scoping for
# every authenticated user, without touching Classification/Releasability.
# Named ALL_AUTHENTICATED rather than PUBLIC (P1, REQUIREMENTS.md Section 11)
# so it can't be misread as "publicly releasable"/unclassified -- it's still
# gated by Classification/Releasability like anything else, just not by org/
# group/user membership.
ALL_AUTHENTICATED_ACCESS_SCOPE = "ALL_AUTHENTICATED"


class DocumentMetadataIn(BaseModel):
    """What an uploader submits at ingest time (FR-2). Classification/Releasability
    are constrained server-side against the caller's claims by validate_against_claims
    below -- this model alone does not enforce that, since it has no claims context."""

    classification: str
    # FR-20/Section 6.3: exactly one Releasability value per document (no
    # multi-select) -- unlike access_scope below, which is "one or more".
    releasability: str = Field(min_length=1)
    access_scope: list[str] = Field(min_length=1)
    source_originator: str
    doc_type: str
    program_community: str | None = None
    effective_date: str | None = None
    # FR-7: optional -- marks this submission as a new version of an existing
    # approved document. The target's existence, status, org, and classification
    # are all re-checked server-side against the submitter's claims in
    # app/routes/upload.py, which has DB access this pydantic-only model doesn't.
    supersedes_document_id: str | None = None

    @field_validator("access_scope")
    @classmethod
    def all_authenticated_is_exclusive_of_nothing(cls, v: list[str]) -> list[str]:
        # ALL_AUTHENTICATED only waives Org/Group/User scoping; it's still a
        # valid value alongside explicit orgs/groups if an uploader wants both
        # recorded.
        return v


class MetadataValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_against_claims(
    metadata: DocumentMetadataIn,
    *,
    allowed_classifications: list[str],
    user_releasability: list[str],
) -> None:
    """Server-side enforcement of FR-18: an uploader may only assign a
    Classification at or below their clearance, and a Releasability value they
    themselves hold -- never just hidden in the UI, always re-checked here."""
    errors = []
    if metadata.classification not in allowed_classifications:
        errors.append(
            f"classification '{metadata.classification}' is above the submitter's "
            "cleared level"
        )
    if metadata.releasability not in user_releasability:
        errors.append(
            f"releasability value '{metadata.releasability}' is not held by the submitter"
        )
    if errors:
        raise MetadataValidationError(errors)
