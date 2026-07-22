"""Section 6.3 metadata schema: the fields a document carries, and validation of
the subset an uploader is allowed to submit given their claims (FR-18)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

PUBLIC_ACCESS_SCOPE = "PUBLIC"


class DocumentMetadataIn(BaseModel):
    """What an uploader submits at ingest time (FR-2). Classification/Releasability
    are constrained server-side against the caller's claims by validate_against_claims
    below -- this model alone does not enforce that, since it has no claims context."""

    classification: str
    releasability: list[str] = Field(min_length=1)
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
    def public_is_exclusive_of_nothing(cls, v: list[str]) -> list[str]:
        # PUBLIC only waives Org/Group/User scoping; it's still a valid value
        # alongside explicit orgs/groups if an uploader wants both recorded.
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
    Classification at or below their clearance, and Releasability values they
    themselves hold -- never just hidden in the UI, always re-checked here."""
    errors = []
    if metadata.classification not in allowed_classifications:
        errors.append(
            f"classification '{metadata.classification}' is above the submitter's "
            "cleared level"
        )
    disallowed = set(metadata.releasability) - set(user_releasability)
    if disallowed:
        errors.append(
            f"releasability value(s) {sorted(disallowed)} are not held by the submitter"
        )
    if errors:
        raise MetadataValidationError(errors)
