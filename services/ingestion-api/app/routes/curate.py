"""FR-10..FR-16: curation queue, scoped to the orgs a curator holds authority
for (FR-12), with approval capped by org (FR-14.2) and by clearance/releasability
(FR-14.1, mirroring FR-18's uploader-side check).

Issue #273: that same clearance/releasability authority is also enforced at
*list* time (list_queue/list_documents below), not just at approve/reject/edit
-- a curator who lacks clearance for a document, or lacks one of its
releasability values, must not see that it exists at all, purely by virtue of
sharing an org with it.

Issue #277 (gap G1): org/clearance/releasability alone don't check a pending
document's `access_scope` (need-to-know) -- a Signal-Corps-scoped document
was fully readable by any same-org curator with the clearance and caveats,
whether or not they're in Signal-Corps, even though the FR-26 retrieval
filter would deny that same person the approved chunks. `access_scope` is
now checked the same way clearance and releasability already are: a hard
requirement for reading (and therefore approving/rejecting) a *pending*
document, with no fallback and no grace period. A document with no
same-org curator whose sub/groups/org happen to match its access_scope
simply has no one who can review it -- that is an admin/user-provisioning
problem (assign the right group, or fix the tag) rather than something this
module works around by widening access on a timer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, select

from app.deps import (
    allowed_classifications,
    require_curator,
    require_curator_or_purge,
    verify_csrf,
)
from common.claims import UserClaims
from common.db import get_session
from common.metadata import access_scope_authorized, releasability_authorized
from common.models import AuditLogEntry, Document, Notification
from common.vector_store import get_store

logger = logging.getLogger("ingestion-api")

router = APIRouter(prefix="/curate", tags=["curation"])


def _visible_to_curator(doc: Document, user: UserClaims) -> bool:
    """Issue #277: on top of the existing org/clearance/releasability
    narrowing, a *pending* document is only visible to a curator whose
    sub/groups/org match its access_scope -- unconditionally, not just
    while some grace period runs. Anything not pending_review is unaffected:
    once a document is decided, need-to-know no longer gates the curation
    list the same way (see docs/roles-and-permissions.md's data-visibility
    matrix)."""
    if doc.status != "pending_review":
        return True
    return access_scope_authorized(doc.access_scope, sub=user.sub, groups=user.groups, org=user.org)


@router.get("/queue")
def list_queue(
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
) -> Sequence[Document]:
    # Issue #273: org membership alone used to be the whole scope -- a
    # curator saw every pending document their org owned, including ones
    # tagged above their own clearance or carrying a releasability caveat
    # they don't hold. Classification is filtered in SQL (allowed_classifications
    # is cheap -- one query, reused for every row); releasability is a
    # per-value check (releasability_authorized) with no clean SQL
    # equivalent against the JSON column, so it's applied in Python after the
    # DB has already done the org/classification narrowing.
    allowed = allowed_classifications(session, user.clearance)
    docs = session.exec(
        select(Document)
        .where(Document.status == "pending_review")
        .where(Document.owner_org.in_(user.curatable_orgs))  # type: ignore[attr-defined]
        .where(Document.classification.in_(allowed))  # type: ignore[attr-defined]
    ).all()
    return [
        d
        for d in docs
        if releasability_authorized(d.releasability, user.releasability)
        and _visible_to_curator(d, user)
    ]


# Issue #266: statuses a document can be edited from through the curation
# "List" dashboard below. Deliberately excludes the in-flight pipeline states
# (queued/processing/embedded -- the worker owns the row until it reaches
# pending_review) and the terminal/destroyed ones (superseded/purging/purged),
# where editing metadata would either race the worker or mutate a record that
# is supposed to be frozen or already gone.
EDITABLE_STATUSES = {"pending_review", "approved", "rejected"}


@router.get("/documents")
def list_documents(
    status_filter: str | None = Query(None, alias="status"),
    classification: str | None = None,
    q: str | None = Query(
        None,
        description="Case-insensitive search across filename, "
        "source/originator, document type, and uploader username",
    ),
    user: UserClaims = Depends(require_curator_or_purge),
    session: Session = Depends(get_session),
) -> Sequence[Document]:
    """Issue #266: the "master list" -- every document (any status) the
    caller holds authority over, not just the pending_review queue
    /curate/queue above returns.

    Two distinct authorities can reach this, scoped differently (issue #279,
    gap G3): a curator (any rag-curate:<org> role) gets exactly the existing
    scoping -- owner_org in curatable_orgs, classification at or below
    clearance, every releasability value held (issue #273), and, for rows
    still pending_review, access_scope membership (issue #277). A rag-purge
    holder with *no* curatable_orgs gets an unscoped list instead, matching
    require_purge's own unscoped destruction authority (app/routes/upload.py)
    -- they can already purge any document by id; this just lets them find
    it. status_filter/classification/q only ever narrow whichever set that
    caller already gets, never widen it. A caller holding both roles gets the
    curator-scoped view -- narrower, and the one they're already used to.
    """
    if user.curatable_orgs:
        allowed = allowed_classifications(session, user.clearance)
        stmt = (
            select(Document)
            .where(Document.owner_org.in_(user.curatable_orgs))  # type: ignore[attr-defined]
            .where(Document.classification.in_(allowed))  # type: ignore[attr-defined]
        )
    else:
        stmt = select(Document)
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    if classification:
        stmt = stmt.where(Document.classification == classification)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(
            or_(
                Document.filename.ilike(needle),  # type: ignore[attr-defined]
                Document.source_originator.ilike(needle),  # type: ignore[attr-defined]
                Document.doc_type.ilike(needle),  # type: ignore[attr-defined]
                Document.uploader_username.ilike(needle),  # type: ignore[attr-defined]
            )
        )
    docs = session.exec(stmt.order_by(Document.updated_at.desc())).all()  # type: ignore[attr-defined]
    if not user.curatable_orgs:
        return docs
    return [
        d
        for d in docs
        if releasability_authorized(d.releasability, user.releasability)
        and _visible_to_curator(d, user)
    ]


class DocumentEdit(BaseModel):
    classification: str | None = None
    # FR-20/Section 6.3: "one or more" cardinality, same as DocumentMetadataIn
    # at upload time -- an edit is not the place to relax that constraint, so
    # an explicit empty list is rejected (422) rather than silently orphaning
    # the document (no releasability caveat/no access scope at all).
    releasability: list[str] | None = Field(default=None, min_length=1)
    access_scope: list[str] | None = Field(default=None, min_length=1)
    source_originator: str | None = None
    doc_type: str | None = None
    program_community: str | None = None
    effective_date: str | None = None


@router.patch("/documents/{doc_id}")
def edit_metadata(
    doc_id: uuid.UUID,
    body: DocumentEdit,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    """Issue #266/#268: lets a curator correct a document's metadata after it
    has already cleared curation, without going through supersession -- the
    gap the issue calls out ("no mechanism to affect the metadata of an
    ingested file after ingestion"). Authority to *edit* a document at all is
    org authority only (`user.can_curate_org`) -- unlike approve()/reject(),
    this deliberately does not also require the curator to hold the
    document's *current* classification/releasability, since a curator
    editing e.g. only `doc_type` on a document tagged above their own
    clearance isn't granting or exercising any access.

    What the edit sets classification/releasability *to* is a different
    question (FR-14.1): if the resulting values are within the caller's own
    clearance/releasability, the edit is applied as approved/rejected/
    whatever the document's status already was. If they're not, the edit is
    still applied -- a curator without the right authority isn't the one who
    gets to decide those values are fine -- but the document is demoted back
    to `pending_review` so a curator who does hold that authority has to sign
    off on it before it's retrievable again (see _authorized_for_tags below).
    This replaced a hard 403 that made *any* edit of a document tagged above
    the caller's clearance fail outright, including edits that never touched
    classification/releasability at all.

    Deletion is deliberately NOT handled here -- that is the separate,
    rag-purge-gated DELETE /documents/{id} in app/routes/upload.py
    (common/purge.py). Purge is intentionally not curator authority (see
    app/deps.require_purge), so the curation list page calls that existing
    endpoint directly rather than this module growing a second, weaker path
    to the same destructive action.
    """
    doc = session.get(Document, doc_id, with_for_update=True)
    if doc is None or not user.can_curate_org(doc.owner_org):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"document is '{doc.status}' and cannot be edited from the curation list -- "
            f"only {', '.join(sorted(EDITABLE_STATUSES))} documents can be",
        )

    # #229: which collection this document's chunks are currently stamped
    # with -- kept as its own variable, not just read back out of `before`,
    # since a classification correction moves the chunks to a different
    # collection rather than writing in place, and update_document_payload
    # needs to be told where they're coming from.
    original_classification: str = doc.classification
    before: dict[str, str | list[str]] = {
        "classification": doc.classification,
        "releasability": list(doc.releasability),
        "access_scope": list(doc.access_scope),
        "status": doc.status,
    }
    changed_qdrant: dict[str, str | list[str]] = {}

    if body.classification is not None and body.classification != doc.classification:
        doc.classification = body.classification
        changed_qdrant["classification"] = doc.classification
    if body.releasability is not None and body.releasability != doc.releasability:
        doc.releasability = body.releasability
        changed_qdrant["releasability"] = doc.releasability
    if body.access_scope is not None and body.access_scope != doc.access_scope:
        doc.access_scope = body.access_scope
        changed_qdrant["access_scope"] = doc.access_scope
    if body.source_originator is not None:
        doc.source_originator = body.source_originator
    if body.doc_type is not None:
        doc.doc_type = body.doc_type
    if body.program_community is not None:
        doc.program_community = body.program_community or None
    if body.effective_date is not None:
        doc.effective_date = body.effective_date or None

    # Issue #268: if the *edited* classification/releasability land outside
    # the caller's own authority, don't 403 -- apply the edit but send the
    # document back to pending_review so a curator who does hold that
    # authority has to sign off on it. approve()/reject() still hard-block on
    # authority (that's the act of granting/publishing access in the first
    # place); this is correcting metadata on something already published.
    demoted = False
    tags_changed = "classification" in changed_qdrant or "releasability" in changed_qdrant
    if (
        tags_changed
        and doc.status != "pending_review"
        and not _authorized_for_tags(user, doc.classification, doc.releasability, session)
    ):
        doc.status = "pending_review"
        doc.reviewed_by_sub = None
        doc.reviewed_at = None
        # Deliberately NOT clearing first_approved_at: the document *was*
        # retrievable until this demotion, and that fact is exactly what the
        # #286 chat-plane purge signal needs to survive this branch.
        changed_qdrant["status"] = doc.status
        demoted = True

    doc.updated_at = datetime.now(UTC)

    if not changed_qdrant:
        # Nothing that Qdrant also holds a copy of changed -- e.g. only
        # source_originator/doc_type/program_community/effective_date edited.
        # No access-control-relevant write, so no NFR-13 revert dance needed.
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.metadata_edit",
                target_id=str(doc.id),
                detail={"fields": list(body.model_dump(exclude_none=True))},
            )
        )
        session.commit()
        session.refresh(doc)
        return doc

    # #229: which collection this document's chunks are stamped with *before*
    # this call -- if changed_qdrant corrects classification, QdrantStore
    # moves them to the target collection rather than writing in place. The
    # revert below has to target where they end up, not where they started.
    resulting_classification: str = doc.classification
    store = get_store()
    store.update_document_payload(str(doc.id), original_classification, changed_qdrant)
    # NFR-13: same reasoning as approve()/reject() -- best-effort revert the
    # Qdrant write if the Postgres commit doesn't durably land, so the two
    # stores don't end up disagreeing about this document's access-control
    # fields.
    try:
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.metadata_edit",
                target_id=str(doc.id),
                detail={
                    "fields": list(body.model_dump(exclude_none=True)),
                    "demoted_to_pending_review": demoted,
                },
            )
        )
        session.commit()
    except Exception:
        try:
            store.update_document_payload(
                str(doc.id), resulting_classification, {k: before[k] for k in changed_qdrant}
            )
        except Exception:
            logger.exception(
                "metadata edit of document %s failed and the Qdrant revert also "
                "failed -- its payload may not match Postgres; needs manual "
                "reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc


def _load_pending(
    session: Session, doc_id: uuid.UUID, user: UserClaims, *, lock: bool = False
) -> Document:
    # Issue #215: `lock=True` takes a row lock (SELECT ... FOR UPDATE) so two
    # curators acting on the same document can't both pass the
    # `pending_review` check and both proceed. Same mechanism #164 introduced
    # for the worker's processing lease. Not taken on read-only paths, which
    # would otherwise serialise the queue view against every decision.
    #
    # Issue #322: existence and org authority are checked, in that order,
    # *before* the status check below -- the same order get_content already
    # uses. Checking status first let a curator with authority over *some*
    # org, but not this document's, learn that an out-of-org id exists and
    # its exact non-pending status via the 409 message, the same class of
    # oracle #215 closed for the 403-vs-404 case.
    doc = session.get(Document, doc_id, with_for_update=lock)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    _check_curator_authority(user, doc, session)
    if doc.status != "pending_review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"document is already {doc.status}")
    return doc


def _authorized_for_tags(
    user: UserClaims, classification: str, releasability: list[str], session: Session
) -> bool:
    """FR-14.1: whether `user` personally holds clearance for `classification`
    and every `releasability` value, mirroring validate_against_claims'
    uploader-side check (common/metadata.py) exactly. A pure predicate rather
    than _check_curator_authority's raise -- edit_metadata (issue #268) wants
    to decide *what to do* when this is False (demote to pending_review)
    rather than reject the request outright."""
    allowed = allowed_classifications(session, user.clearance)
    if classification not in allowed:
        return False
    return releasability_authorized(releasability, user.releasability)


def _check_curator_authority(user: UserClaims, doc: Document, session: Session) -> None:
    if not user.can_curate_org(doc.owner_org):
        # Issue #215: 404, not 403. A 403 naming the owning org told a curator
        # scoped to one org that a given document id exists and which org owns
        # it -- an existence oracle, and one that leaks an org name to someone
        # with no authority over it. To a caller who may not curate this
        # document, it is indistinguishable from a document that isn't there,
        # which is exactly what they should be able to observe.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    allowed = allowed_classifications(session, user.clearance)
    if doc.classification not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot approve a document above your own cleared level",
        )
    # FR-14.1: "by clearance/releasability, same as FR-18" -- a curator who
    # doesn't hold one of the document's releasability values can't publish
    # it, mirroring validate_against_claims' uploader-side check exactly.
    if not releasability_authorized(doc.releasability, user.releasability):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"cannot approve a document with releasability values {doc.releasability}, "
            "one or more of which you do not hold",
        )
    # Issue #277 (gap G1): need-to-know, same as _visible_to_curator applies
    # to queue listing -- but this is the actual access-control point, not
    # just discoverability. Only gates a document still pending_review: once
    # decided, access_scope no longer bears on a curator's authority to act
    # on it (e.g. approving a new version that supersedes an already-approved
    # document whose access_scope they don't happen to match).
    if doc.status == "pending_review" and not access_scope_authorized(
        doc.access_scope, sub=user.sub, groups=user.groups, org=user.org
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot approve or reject a document outside your access scope",
        )


class ChunkContent(BaseModel):
    """Issue #284: the fields a curator needs to actually read a chunk,
    deliberately not the raw Qdrant payload dict -- pydantic drops any field
    that isn't declared here, so the response can't grow an unintended leak
    just because a later change adds something new to the payload (e.g. FR-26
    fields like access_scope, already visible to this curator via the
    Document row, aren't repeated here)."""

    chunk_index: int
    heading: str | None = None
    page_or_slide: int | None = None
    content_type: str = "text"
    text: str


@router.get("/{doc_id}/content")
def get_content(
    doc_id: uuid.UUID,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
) -> list[ChunkContent]:
    """Issue #284: the parsed chunk text a curator is being asked to approve
    -- read back from the vector store (whichever backend is configured,
    #160), which is what retrieval will actually serve if this document is
    approved, rather than a second re-render of the original upload.

    Scoped to `pending_review` documents only: reviewing content before a
    decision is exactly the gap this closes, and once a document is decided
    it has already left the queue (list_queue) -- there is no ongoing reason
    for this route to serve it. Reuses `_load_pending`, which checks
    existence, then org/clearance/releasability/access_scope authority, then
    status, in that order (issue #322) -- an out-of-org caller gets 404
    before anything about the document's existence or status is revealed.
    """
    doc = _load_pending(session, doc_id, user)
    chunks = get_store().fetch_document_chunks(str(doc.id), doc.classification)
    return [ChunkContent(**chunk) for chunk in chunks]


def _validate_supersede(user: UserClaims, new_doc: Document, session: Session) -> Document:
    """FR-7: everything that can fail about the version swap, checked *before*
    any mutation (Postgres or Qdrant) happens for either document -- so a
    rejected approval attempt never leaves the new document's chunks flipped
    to `approved` in Qdrant while Postgres still says `pending_review`.

    Re-validates the old document independently rather than trusting the
    submission-time check in app/routes/upload.py: its status could have
    changed since (someone else superseded or otherwise touched it), and a
    curator's authority over the *new* doc's (possibly corrected) tags
    doesn't imply authority over the old doc's -- a version can legitimately
    change classification, so both have to be checked.

    Issue #326: existence, then authority, then status, in that order --
    matching _load_pending (#322). Checking status first let a curator with
    authority over the *new* doc but not the *old* one (lower clearance than
    the original uploader, whose clearance is what submission-time validation
    checked) learn old_doc's exact non-approved status via the 409 message,
    before their lack of authority over old_doc was ever established.
    """
    old_doc = session.get(Document, new_doc.supersedes_document_id)
    if old_doc is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the document this submission supersedes no longer exists"
        )
    _check_curator_authority(user, old_doc, session)
    if old_doc.status != "approved":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the document this submission supersedes is now '{old_doc.status}', not "
            "'approved' -- resolve manually before approving this version",
        )
    return old_doc


def _tagging_advisory_outcome(doc: Document) -> dict | None:
    """Issue #306 gap 1: link the curator's decision back to the ingestion-time
    marking-mismatch advisory (issue #138), precedent advisory (issue #307
    Phase 2), LLM classification suggestion (issue #308 Phase 3), content
    advisory (issue #284 item 2), and sensitive-data-pattern advisory (issue
    #342 regex Phase 1 / #343 LLM-assisted Phase 2), if any was flagged for
    this document. Without this,
    recovering "did the curator agree with the flag" means diffing the
    `document.tagging_advisory` and `document.approve`/`document.reject`
    audit rows by document ID after the fact -- embedding the flagged values
    and the curator's final tags directly in the decision's own audit entry
    makes that row self-contained instead.

    Deliberately reads only `doc.tagging_advisory` (a Document column
    ingestion-api already has SELECT on), not the audit_log table itself --
    issue #278's per-service DB grants make audit_log INSERT-only for every
    application role, including this one (see
    infra/postgres/apply-service-grants.sh), specifically so no route needs
    read access to the trail. A `select(AuditLogEntry)` lookup here would
    violate that boundary and fail against the real grant (caught live: it
    raised `psycopg.errors.InsufficientPrivilege` against the dev stack).

    Returns None when nothing was flagged by either advisory (the common
    case), so approve/reject audit entries stay unchanged for documents
    neither one had a finding for.

    Issue #309 (Phase 4): also embeds `assigned_classification` (the tag in
    place *before* this decision, per Phase 1's own to_dict()) and an
    explicit `marking_mismatch_flagged` boolean. Neither is used by anything
    in this module -- they exist so an offline reader of these audit entries
    (scripts/calibrate_tagging_advisory.py) can tell a genuine Phase 1
    under-classification flag apart from Phase 1 having merely *detected* a
    marking at or below the assigned level (which also leaves
    `flagged_classification` non-None), and can rank-compare the curator's
    final decision against what was flagged without needing a second query.

    Issue #345: also embeds counts/kinds for a sensitive-data-pattern finding
    (`pii_advisory.findings` from #342's regex pass, `pii_advisory.llm_findings`
    from #343's LLM-assisted pass), same "kinds only, never the finding's own
    evidence" posture as `content_advisory_kinds` below. Unlike the
    classification-tag suggesters above, a PII finding carries no
    `disagrees_with_assigned`-style target to rank-compare against, so there is
    no `*_flagged` boolean or accept/override verdict computed here --
    `calibrate_tagging_advisory.py` derives its own "did the curator visibly
    act on this" signal from `assigned_classification`/`final_classification`
    plus which decision action produced this entry, since that's read off the
    audit row itself, not something this function can see.

    Issue #380: when #378's LLM verification ran for this document, also
    embeds `pii_regex_llm_verified_count`/`pii_regex_llm_likely_false_positive_count`
    -- counts only, never a verdict's own rationale text, same posture as
    everything else here. Omitted entirely when verification never ran (not
    zeroed), so the calibration script can tell "no verdicts" apart from
    "verdicts unanimously said genuine" without a third sentinel state.
    """
    advisory = doc.tagging_advisory or {}
    precedent = advisory.get("precedent") or {}
    llm_suggestion = advisory.get("llm_suggestion") or {}
    content_findings = (advisory.get("content_advisory") or {}).get("findings") or []
    pii_advisory = advisory.get("pii_advisory") or {}
    pii_regex_findings = pii_advisory.get("findings") or []
    pii_llm_findings = pii_advisory.get("llm_findings") or []
    flagged = bool(advisory.get("under_classified") or advisory.get("unassigned_caveats"))
    flagged_precedent = precedent.get("disagrees_with_assigned")
    flagged_llm = llm_suggestion.get("disagrees_with_assigned")
    flagged_content = bool(content_findings)
    flagged_pii_regex = bool(pii_regex_findings)
    flagged_pii_llm = bool(pii_llm_findings)
    if not (
        flagged
        or flagged_precedent
        or flagged_llm
        or flagged_content
        or flagged_pii_regex
        or flagged_pii_llm
    ):
        return None
    outcome = {
        "assigned_classification": advisory.get("assigned_classification"),
        "marking_mismatch_flagged": flagged,
        "flagged_classification": advisory.get("detected_classification"),
        "flagged_caveats": advisory.get("unassigned_caveats"),
        "final_classification": doc.classification,
        "final_releasability": doc.releasability,
    }
    if flagged_precedent:
        outcome["precedent_classification"] = precedent.get("top_classification")
        outcome["precedent_similar_count"] = precedent.get("similar_count")
    if flagged_llm:
        outcome["llm_suggested_classification"] = llm_suggestion.get("suggested_classification")
        outcome["llm_suggested_doc_type"] = llm_suggestion.get("suggested_doc_type")
        outcome["llm_confidence"] = llm_suggestion.get("confidence")
    if flagged_content:
        # Kinds only, not the full findings (which carry context excerpts of
        # the document's own text) -- this audit entry records that the
        # curator was shown a hidden-instruction flag, not a second copy of
        # the flagged content itself.
        outcome["content_advisory_kinds"] = sorted({f["kind"] for f in content_findings})
    if flagged_pii_regex:
        # Same "kinds/count only, never the finding's own context excerpt"
        # posture as content_advisory_kinds above.
        outcome["pii_regex_kinds"] = sorted({f["kind"] for f in pii_regex_findings})
        outcome["pii_regex_count"] = len(pii_regex_findings)
        # Issue #380 (calibration follow-on to #378): counts only, never a
        # verdict's own rationale text -- same posture as every other field
        # here. Omitted entirely (not zero) when verification never ran for
        # this document, so `calibrate_tagging_advisory.py` can tell "no
        # verdicts" apart from "verdicts said nothing was a false positive"
        # without a third state.
        verdicts = [f.get("llm_verdict") for f in pii_regex_findings if f.get("llm_verdict")]
        if verdicts:
            outcome["pii_regex_llm_verified_count"] = len(verdicts)
            outcome["pii_regex_llm_likely_false_positive_count"] = sum(
                1 for v in verdicts if v.get("likely_false_positive")
            )
    if flagged_pii_llm:
        # Same posture -- kinds only, never the model's own rationale text.
        outcome["pii_llm_kinds"] = sorted({f["kind"] for f in pii_llm_findings})
        outcome["pii_llm_count"] = len(pii_llm_findings)
    return outcome


def _execute_supersede(
    user: UserClaims, old_doc: Document, new_doc: Document, session: Session
) -> None:
    """The actual swap -- only called after _validate_supersede has already
    passed, so this is expected not to fail."""
    # #160: through the backend seam (Qdrant by default, Milvus opt-in).
    # #229: old_doc's classification is never corrected here, so it still
    # names the collection its chunks live in.
    get_store().delete_document_chunks(str(old_doc.id), old_doc.classification)
    old_doc.status = "superseded"
    old_doc.updated_at = datetime.now(UTC)
    session.add(old_doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.supersede",
            target_id=str(old_doc.id),
            detail={"superseded_by_document_id": str(new_doc.id)},
        )
    )


class Corrections(BaseModel):
    classification: str | None = None
    releasability: list[str] | None = None  # FR-20/Section 6.3: one or more values
    access_scope: list[str] | None = None


@router.post("/{doc_id}/approve")
def approve(
    doc_id: uuid.UUID,
    corrections: Corrections | None = None,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    doc = _load_pending(session, doc_id, user, lock=True)

    # #229: which collection this document's chunks are stamped with *before*
    # any correction below -- a classification correction moves them to a
    # different collection rather than writing in place, and this is what
    # locates the current one.
    original_classification = doc.classification

    if corrections:
        if corrections.classification:
            doc.classification = corrections.classification
        if corrections.releasability is not None:
            doc.releasability = corrections.releasability
        if corrections.access_scope is not None:
            doc.access_scope = corrections.access_scope
        # Re-check authority against the corrected classification, not just the original.
        _check_curator_authority(user, doc, session)

    # FR-7: validate the whole supersede chain *before* touching Qdrant or
    # Postgres for either document -- everything below this point is expected
    # to succeed, so a failure here can't leave the new document half-approved.
    old_doc = _validate_supersede(user, doc, session) if doc.supersedes_document_id else None

    doc.status = "approved"
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(UTC)
    # #286: set once, at the *first* approval only, and never cleared -- the
    # edit route's #268 demotion wipes reviewed_at, and this field is what
    # lets a later purge still know the document was once FR-26-retrievable
    # (see common/models.py). A re-approval after demotion keeps the original
    # timestamp: the chat-plane sweep window starts at the earliest exposure.
    if doc.first_approved_at is None:
        doc.first_approved_at = doc.reviewed_at

    qdrant_fields: dict[str, str | list[str]] = {"status": doc.status}
    if corrections:
        if corrections.classification:
            qdrant_fields["classification"] = doc.classification
        if corrections.releasability is not None:
            qdrant_fields["releasability"] = doc.releasability
        if corrections.access_scope is not None:
            qdrant_fields["access_scope"] = doc.access_scope
    # #229: the collection the chunks end up in after this call -- the target
    # of a classification correction, or unchanged if there wasn't one. This
    # is what a revert below must target, since by the time a revert could run
    # the chunks already live here, not at original_classification.
    resulting_classification = doc.classification
    store = get_store()
    store.update_document_payload(str(doc.id), original_classification, qdrant_fields)

    # NFR-13: Qdrant now already shows the new document as `approved`, but
    # nothing is durable yet -- Postgres (the system of record for the
    # curation queue and the uploader-facing status) only reflects that once
    # session.commit() below succeeds. If anything in this block raises (a DB
    # error, the old document's Qdrant delete failing, etc.), the Postgres
    # transaction rolls back on its own (get_session's context manager), but
    # the Qdrant write above doesn't -- so best-effort revert it too, keeping
    # both stores agreeing again on `pending_review` rather than leaving a
    # document Postgres still lists as pending already showing as approved
    # (and therefore retrievable, FR-11/FR-26) in Qdrant.
    try:
        if old_doc is not None:
            _execute_supersede(user, old_doc, doc, session)

        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.approve",
                target_id=str(doc.id),
                detail={
                    "corrections": corrections.model_dump() if corrections else None,
                    "tagging_advisory": _tagging_advisory_outcome(doc),
                    # #285: binds this decision to the exact bytes the curator
                    # was shown, not just the document id.
                    "content_sha256": doc.content_sha256,
                },
            )
        )
        # FR-15: notify the uploader of the decision.
        session.add(
            Notification(
                recipient_sub=doc.uploader_sub,
                document_id=doc.id,
                decision="approved",
                message=f"Your document '{doc.filename}' was approved.",
            )
        )
        session.commit()
    except Exception:
        try:
            store.update_document_payload(
                str(doc.id), resulting_classification, {"status": "pending_review"}
            )
        except Exception:
            logger.exception(
                "approval of document %s failed and the Qdrant status revert also "
                "failed -- its chunks may still show status=approved despite Postgres "
                "still saying pending_review; needs manual reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc


class Rejection(BaseModel):
    reason: str


@router.post("/{doc_id}/reject")
def reject(
    doc_id: uuid.UUID,
    body: Rejection,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    doc = _load_pending(session, doc_id, user, lock=True)

    doc.status = "rejected"
    doc.rejection_reason = body.reason
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(UTC)
    store = get_store()
    store.update_document_payload(str(doc.id), doc.classification, {"status": doc.status})
    # NFR-13: same reasoning as approve() above -- revert the Qdrant write if
    # the Postgres commit doesn't durably land, so the two stores don't end up
    # disagreeing about whether this document is still pending.
    try:
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.reject",
                target_id=str(doc.id),
                detail={
                    "reason": body.reason,
                    "tagging_advisory": _tagging_advisory_outcome(doc),
                    "content_sha256": doc.content_sha256,
                },
            )
        )
        # FR-15: notify the uploader of the decision, with the stated reason.
        session.add(
            Notification(
                recipient_sub=doc.uploader_sub,
                document_id=doc.id,
                decision="rejected",
                message=f"Your document '{doc.filename}' was rejected: {body.reason}",
            )
        )
        session.commit()
    except Exception:
        try:
            store.update_document_payload(
                str(doc.id), doc.classification, {"status": "pending_review"}
            )
        except Exception:
            logger.exception(
                "rejection of document %s failed and the Qdrant status revert also "
                "failed -- its chunks may still show status=rejected despite Postgres "
                "still saying pending_review; needs manual reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc
