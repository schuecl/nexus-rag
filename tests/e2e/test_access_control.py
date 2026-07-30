"""Step definitions for tests/e2e/features/access_control.feature.

In-process BDD: scenarios exercise the real security functions
(common.qdrant_filters.build_access_filter, common.metadata
.validate_against_claims, common.versioning.validate_supersede_target,
common.claims.UserClaims) -- the same code paths the services call -- without
needing a live stack, so they run in ci.yml on every PR.
"""

from __future__ import annotations

import uuid

from pytest_bdd import given, parsers, scenario, then, when

from common.claims import UserClaims
from common.metadata import (
    NO_RELEASABILITY_RESTRICTION,
    DocumentMetadataIn,
    MetadataValidationError,
    access_scope_authorized,
    validate_against_claims,
)
from common.models import Document
from common.qdrant_filters import build_access_filter
from common.versioning import SupersedeValidationError, validate_supersede_target

FEATURE = "features/access_control.feature"

CLASSIFICATION_RANKS = {
    "UNCLASSIFIED": 1,
    "CUI": 2,
    "SECRET": 3,
    "TOP SECRET": 4,
}


def _at_or_below(clearance: str) -> list[str]:
    rank = CLASSIFICATION_RANKS.get(clearance)
    if rank is None:
        return []
    return [value for value, r in CLASSIFICATION_RANKS.items() if r <= rank]


# ------------------------------------------------------------------ scenarios


@scenario(FEATURE, "The retrieval filter only ever returns approved documents")
def test_filter_requires_approved():
    pass


@scenario(FEATURE, "A user never retrieves documents above their clearance")
def test_filter_classification_ceiling():
    pass


@scenario(FEATURE, "An unknown clearance admits no classification at all")
def test_filter_unknown_clearance_fail_closed():
    pass


@scenario(FEATURE, "A releasability the user does not hold is never matched")
def test_filter_releasability_exact():
    pass


@scenario(FEATURE, "Cross-org content is invisible to other orgs")
def test_filter_cross_org_isolation():
    pass


@scenario(FEATURE, "An uploader cannot tag a document above their clearance")
def test_upload_classification_ceiling():
    pass


@scenario(FEATURE, "An uploader cannot assign a releasability they do not hold")
def test_upload_releasability_holdings():
    pass


@scenario(FEATURE, "A curator's authority is scoped to their own org")
def test_curator_org_scoping():
    pass


@scenario(FEATURE, "A supersede target must be approved and within the submitter's authority")
def test_supersede_status_guard():
    pass


@scenario(FEATURE, "A supersede target in another org is rejected")
def test_supersede_org_guard():
    pass


@scenario(FEATURE, "A curator's need-to-know matches on org, group, sub, or ALL_AUTHENTICATED")
def test_curator_need_to_know_scope_matching():
    pass


# ---------------------------------------------------------------------- steps


@given(
    parsers.parse(
        'a user "{username}" with clearance "{clearance}" and '
        'releasability "{releasability}" and org "{org}"'
    ),
    target_fixture="ctx",
)
def ctx_user(username, clearance, releasability, org):
    # clearance/releasability are properties derived from rag-clearance:/
    # rag-releasability: client roles (#104, #116), so the Gherkin's values
    # have to be encoded as roles rather than passed as fields.
    roles = ["rag-query"]
    if clearance:
        roles.append(f"rag-clearance:{clearance}")
    roles += [f"rag-releasability:{v}" for v in releasability.split(",") if v]
    return {
        "claims": UserClaims(
            sub=f"{username}-sub",
            preferred_username=username,
            groups=[],
            org=org or None,
            rag_roles=roles,
        )
    }


@given(parsers.parse('a user "{username}" with roles "{roles}"'), target_fixture="ctx")
def ctx_user_with_roles(username, roles):
    return {
        "claims": UserClaims(
            sub=f"{username}-sub",
            preferred_username=username,
            rag_roles=[r for r in roles.split(",") if r],
        )
    }


@given(parsers.parse('the allowed classifications for that user are "{values}"'))
def ctx_allowed(ctx, values):
    ctx["allowed_classifications"] = [v for v in values.split(",") if v]


@given("no classifications are allowed for that user")
def ctx_no_allowed(ctx):
    ctx["allowed_classifications"] = []


@given(parsers.parse('an existing document with status "{status}" owned by org "{org}"'))
def ctx_existing_doc(ctx, status, org):
    ctx["old_doc"] = Document(
        id=uuid.uuid4(),
        filename="existing.pdf",
        uploader_sub="someone-else",
        uploader_username="someone-else",
        owner_org=org,
        classification=ctx["claims"].clearance,
        releasability=list(ctx["claims"].releasability) or [NO_RELEASABILITY_RESTRICTION],
        access_scope=[org],
        source_originator=f"{org} originator",
        doc_type="report",
        status=status,
    )


@when("the server-side access filter is built for that user", target_fixture="ctx")
def build_filter(ctx):
    allowed = ctx.get("allowed_classifications", _at_or_below(ctx["claims"].clearance))
    ctx["filter"] = build_access_filter(ctx["claims"], allowed_classifications=allowed)
    return ctx


@when(
    parsers.parse(
        'that user submits metadata with classification "{classification}" '
        'and releasability "{releasability}"'
    )
)
def submit_metadata(ctx, classification, releasability):
    metadata = DocumentMetadataIn(
        classification=classification,
        releasability=[v for v in releasability.split(",") if v],
        access_scope=[ctx["claims"].org],
        source_originator="test",
        doc_type="report",
    )
    try:
        validate_against_claims(
            metadata,
            allowed_classifications=_at_or_below(ctx["claims"].clearance),
            user_releasability=ctx["claims"].releasability,
        )
        ctx["error"] = None
    except MetadataValidationError as exc:
        ctx["error"] = exc


@when("that user names the existing document as a supersede target")
def supersede(ctx):
    try:
        validate_supersede_target(
            ctx["old_doc"],
            new_owner_org=ctx["claims"].org,
            allowed_classifications=_at_or_below(ctx["claims"].clearance),
            user_releasability=ctx["claims"].releasability,
        )
        ctx["error"] = None
    except SupersedeValidationError as exc:
        ctx["error"] = exc


# ------------------------------------------------------------------ assertions


def _condition(ctx, key):
    return {c.key: c for c in ctx["filter"].must}[key]


@then(parsers.parse('the filter requires document status "{status}"'))
def filter_requires_status(ctx, status):
    assert _condition(ctx, "status").match.value == status


@then(parsers.parse('the filter admits only classifications "{values}"'))
def filter_admits_classifications(ctx, values):
    assert _condition(ctx, "classification").match.any == [v for v in values.split(",") if v]


@then("the filter admits no classifications")
def filter_admits_no_classifications(ctx):
    assert _condition(ctx, "classification").match.any == []


@then(parsers.parse('the filter admits only releasability "{values}"'))
def filter_admits_releasability(ctx, values):
    # NONE is always admitted on top of the user's own holdings -- it is the
    # explicit "no coalition caveat" value every authenticated user may see,
    # not a caveat anyone holds (common/metadata.py).
    assert _condition(ctx, "releasability").match.any == [
        NO_RELEASABILITY_RESTRICTION,
        *(v for v in values.split(",") if v),
    ]


@then(parsers.parse('the filter does not admit releasability "{value}"'))
def filter_excludes_releasability(ctx, value):
    assert value not in _condition(ctx, "releasability").match.any


@then(parsers.parse('the filter admits access scope "{value}"'))
def filter_admits_scope(ctx, value):
    assert value in _condition(ctx, "access_scope").match.any


@then(parsers.parse('the filter does not admit access scope "{value}"'))
def filter_excludes_scope(ctx, value):
    assert value not in _condition(ctx, "access_scope").match.any


@then("the submission is rejected with a clearance error")
def submission_clearance_error(ctx):
    assert isinstance(ctx["error"], MetadataValidationError)
    assert any("cleared level" in e for e in ctx["error"].errors)


@then("the submission is rejected with a releasability error")
def submission_releasability_error(ctx):
    assert isinstance(ctx["error"], MetadataValidationError)
    assert any("not held by the submitter" in e for e in ctx["error"].errors)


@then(parsers.parse('that user can curate org "{org}"'))
def user_can_curate(ctx, org):
    assert ctx["claims"].can_curate_org(org)


@then(parsers.parse('that user cannot curate org "{org}"'))
def user_cannot_curate(ctx, org):
    assert not ctx["claims"].can_curate_org(org)


@then(parsers.parse('that user\'s need-to-know matches an access scope of "{value}"'))
def need_to_know_matches(ctx, value):
    # Issue #277 (gap G1): the same predicate app/routes/curate.py's
    # scope-preference grace period uses to decide queue visibility for a
    # pending document -- pinned here as a security invariant independent of
    # any DB-backed grace-period timing (covered separately in
    # services/ingestion-api/tests/test_curate_documents.py).
    claims = ctx["claims"]
    assert access_scope_authorized([value], sub=claims.sub, groups=claims.groups, org=claims.org)


@then(parsers.parse('that user\'s need-to-know does not match an access scope of "{value}"'))
def need_to_know_does_not_match(ctx, value):
    claims = ctx["claims"]
    assert not access_scope_authorized(
        [value], sub=claims.sub, groups=claims.groups, org=claims.org
    )


@then("the supersede is rejected with a status error")
def supersede_status_error(ctx):
    assert isinstance(ctx["error"], SupersedeValidationError)
    assert any("not 'approved'" in e for e in ctx["error"].errors)


@then("the supersede is rejected with an org error")
def supersede_org_error(ctx):
    assert isinstance(ctx["error"], SupersedeValidationError)
    assert any("different org" in e for e in ctx["error"].errors)
