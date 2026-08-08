"""Issue #565: security tagging on the upload form must be a deliberate act.

The upload page used to arrive pre-filled in the two places that decide who can
retrieve a document. The classification `<select>` had no placeholder, so the
browser auto-selected the first option -- the *lowest* level -- and the
releasability checkbox for the no-restriction value was rendered `checked`. The
zero-click path therefore submitted "UNCLASSIFIED, releasable to everyone".

Server-side claims validation (FR-18) stops a user tagging *above* their
authority, but nothing stops inattentive tagging *below* a document's true
sensitivity, and that is the dangerous direction -- it widens the retrieval
audience. Curation (FR-11) is the safety net; the portal should not be
manufacturing mis-tagged submissions for curators to catch.

These are source-shape assertions on the template and its script, following
test_csp_templates.py: the properties live in markup a browser applies, so
asserting the markup is what pins them without a browser in CI. The behavioural
half -- what the server does when a blank submission arrives anyway -- is
exercised through the real validation helper below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from app.routes import upload
from common.claims import UserClaims
from common.models import ClassificationLevel

APP_DIR = Path(__file__).resolve().parents[1] / "app"
UPLOAD_HTML = (APP_DIR / "templates" / "upload.html").read_text(encoding="utf-8")
UPLOAD_JS = (APP_DIR / "static" / "upload.js").read_text(encoding="utf-8")
PORTAL_CSS = (APP_DIR / "static" / "portal.css").read_text(encoding="utf-8")

UPLOADER = UserClaims(
    sub="uploader-sub",
    preferred_username="alice-ingest",
    org="USAREUR-AF",
    rag_roles=["rag-ingest", "rag-clearance:SECRET", "rag-releasability:NONE"],
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for value, rank in [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3)]:
            session.add(ClassificationLevel(value=value, rank=rank))
        session.commit()
        yield session


def _classification_select() -> str:
    match = re.search(r'<select id="classification".*?</select>', UPLOAD_HTML, re.S)
    assert match, "the classification select is gone -- these assertions are stale"
    return match.group(0)


def _option_tags(select_html: str) -> list[str]:
    # Jinja loop bodies included: the point is the order the browser sees, and
    # the placeholder must precede the loop that emits the real levels.
    return re.findall(r"<option\b[^>]*>", select_html)


def test_classification_offers_a_placeholder_first() -> None:
    first = _option_tags(_classification_select())[0]
    assert 'value=""' in first, f"first option is submittable: {first}"
    assert "selected" in first, (
        f"first option is not selected, so the browser will auto-select the "
        f"next one -- the lowest classification: {first}"
    )


def test_the_placeholder_cannot_be_submitted() -> None:
    """`disabled` keeps it unselectable; `required` makes the empty value fail."""
    first = _option_tags(_classification_select())[0]
    assert "disabled" in first, f"the placeholder is selectable as a real value: {first}"
    assert "required" in _classification_select().split(">")[0], (
        "the select lost `required`, so an empty classification would pass "
        "reportValidity() and reach the server"
    )


def test_no_real_classification_option_is_preselected() -> None:
    """Only the placeholder may carry `selected`.

    Guards the regression directly: re-adding `selected` to the loop that emits
    the vocabulary -- or dropping the placeholder so the first level inherits the
    browser's implicit selection -- puts a real level back on the zero-click path.
    """
    preselected = [tag for tag in _option_tags(_classification_select()) if "selected" in tag]
    assert len(preselected) == 1, f"expected only the placeholder selected, got {preselected}"
    assert 'value=""' in preselected[0]


def test_no_releasability_value_is_prechecked() -> None:
    match = re.search(
        r'<div class="choice-grid" id="releasabilityChoices">.*?</div>', UPLOAD_HTML, re.S
    )
    assert match, "the releasability choice grid is gone -- these assertions are stale"
    # Strip Jinja comments first: the template explains *why* nothing is checked,
    # and prose mentioning the attribute is not the attribute -- the same
    # distinction test_csp_templates.py draws for a comment mentioning onclick.
    grid = re.sub(r"\{#.*?#\}", "", match.group(0), flags=re.S)
    assert 'name="releasability"' in grid, "the checkbox group moved; update this test"
    checked_inputs = [tag for tag in re.findall(r"<input\b[^>]*>", grid) if "checked" in tag]
    assert not checked_inputs, (
        f"a releasability value is pre-checked, so the zero-click path submits a "
        f"marking the user never chose: {checked_inputs}"
    )


def test_the_at_least_one_rule_is_enforced_client_side() -> None:
    """A checkbox group cannot express "at least one" natively.

    `required` on a checkbox constrains that single box, so `reportValidity()`
    will not catch an empty group -- with nothing pre-checked that state is now
    reachable, and the submit handler has to reject it explicitly.
    """
    submit_handler = UPLOAD_JS.split('form.addEventListener("submit"', 1)
    assert len(submit_handler) == 2, "the submit handler moved; update this test"
    body = submit_handler[1]
    assert "releaseInputs.some((input) => input.checked)" in body, (
        "the submit handler does not check that a releasability box is ticked"
    )
    assert "setReleasabilityError(" in body, "no error is surfaced for an empty group"
    assert '<p class="field-error" id="releasabilityError"' in UPLOAD_HTML, (
        "the error element the handler writes into does not exist"
    )


def test_the_invalid_group_has_a_visible_affordance() -> None:
    """The browser draws none for a JS-validated checkbox group."""
    assert ".choice-grid.invalid .choice span" in PORTAL_CSS, (
        "the invalid releasability group is styled no differently, so the error "
        "text is the only signal"
    )


def test_clearing_the_form_also_clears_the_releasability_error() -> None:
    clear_form = UPLOAD_JS.split("const clearForm = () => {", 1)
    assert len(clear_form) == 2, "clearForm moved; update this test"
    assert "setReleasabilityError();" in clear_form[1].split("};", 1)[0]


# --- the server backstop, for a submission that arrives blank anyway ---------


def _validate(session: Session, *, classification: str, releasability: str) -> None:
    upload._validate_metadata(
        session=session,
        user=UPLOADER,
        classification=classification,
        releasability=releasability,
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="Ops",
        doc_type="SOP",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
    )


def test_blank_classification_is_still_rejected_server_side(session: Session) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate(session, classification="", releasability='["NONE"]')
    assert exc.value.status_code == 403


def test_empty_releasability_is_still_rejected_server_side(session: Session) -> None:
    with pytest.raises(HTTPException) as exc:
        _validate(session, classification="UNCLASSIFIED", releasability="[]")
    assert exc.value.status_code == 400


def test_an_explicitly_tagged_submission_still_validates(session: Session) -> None:
    """The negative control: these guards must not block a real submission."""
    _validate(session, classification="CUI", releasability='["NONE"]')


# --- curation edit forms legitimately pre-fill; #565 must not touch them -----


@pytest.mark.parametrize("template", ["curate.html", "curate_list.html"])
def test_curation_forms_still_preselect_the_documents_current_tags(template: str) -> None:
    """Correcting a tag starts from the tag being corrected, which is not the
    same situation as a fresh upload and is explicitly out of scope for #565."""
    source = (APP_DIR / "templates" / template).read_text(encoding="utf-8")
    assert re.search(r"optionNodes\(CLASSIFICATIONS, doc\.classification\)", source), (
        f"{template} no longer pre-selects the document's classification"
    )
    assert re.search(r"multiOptionNodes\(RELEASABILITY, doc\.releasability\)", source), (
        f"{template} no longer pre-selects the document's releasability"
    )
