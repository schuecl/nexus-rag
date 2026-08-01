"""FR-7: authorization checks for marking one submission as a new version of
an existing document. Supersession deletes the old document's Qdrant chunks
at curator-approval time (app/routes/curate.py), so both the submitter
(referencing an existing document by id) and the approving curator need their
own claims re-checked against the *old* document specifically -- the new
doc's tags don't necessarily match the old doc's (a version can legitimately
change classification), so authority over one doesn't imply authority over
the other.
"""

from __future__ import annotations

from .metadata import releasability_authorized
from .models import Document


class SupersedeValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_supersede_target(
    old_doc: Document,
    *,
    new_owner_org: str,
    allowed_classifications: list[str],
    user_releasability: list[str],
) -> None:
    """Guards against using supersedes_document_id to tamper with content the
    caller couldn't otherwise see or act on -- without this, any rag-ingest
    user could reference an arbitrary document id and, once a curator
    approves the submission, silently delete that document's vectors.

    Issue #325: org authority is checked -- and, if it fails, raised on --
    before status/classification/releasability are evaluated at all. Those
    three describe security-relevant detail about a document (its exact
    status, that its classification exceeds the caller's clearance, that its
    releasability exceeds the caller's) that only makes sense to disclose to
    a caller with some standing over the document in the first place; a
    same-org uploader still gets everything wrong accumulated together in one
    response (useful for fixing multiple problems at once), same as before.
    """
    if old_doc.owner_org != new_owner_org:
        raise SupersedeValidationError(["target document belongs to a different org"])

    errors = []
    if old_doc.status != "approved":
        errors.append(
            f"target document is '{old_doc.status}', not 'approved' -- only an "
            "approved document can be superseded"
        )
    if old_doc.classification not in allowed_classifications:
        errors.append("target document's classification is above the submitter's cleared level")
    if not releasability_authorized(old_doc.releasability, user_releasability):
        errors.append(
            "submitter does not hold one or more of the target document's releasability values"
        )
    if errors:
        raise SupersedeValidationError(errors)
