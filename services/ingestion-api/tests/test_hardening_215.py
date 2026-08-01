"""Issue #215: four small hardening gaps, each pinned by the property that
made it a gap rather than by its implementation.

None of these was exploitable on its own. They are grouped because they share
a shape: each was a place where the codebase's own convention was applied
everywhere except one spot.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.routes import auth, curate

ROUTES = Path(__file__).resolve().parents[1] / "app" / "routes"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


class TestCurationExistenceOracle:
    """A curator scoped to one org could distinguish "no such document" from
    "someone else's document", and learn the owning org's name from the 403
    message -- for a document they have no authority over."""

    def test_unauthorized_org_is_indistinguishable_from_a_missing_document(self):
        source = inspect.getsource(curate._check_curator_authority)

        assert "HTTP_404_NOT_FOUND" in source, (
            "a caller who may not curate this document should not be able to tell it exists"
        )
        assert "document not found" in source

    def test_the_owning_org_is_not_named_in_the_response(self):
        source = inspect.getsource(curate._check_curator_authority)

        # The old message interpolated doc.owner_org, handing an org name to
        # someone with no authority over it.
        assert "doc.owner_org}" not in source


class TestApproveRaceLock:
    """Two curators acting simultaneously could both pass the pending_review
    check. #164 introduced the row-lock mechanism for the worker's processing
    lease; this applies it to the second place that needs it."""

    def test_load_pending_can_take_a_row_lock(self):
        source = inspect.getsource(curate._load_pending)

        assert "with_for_update=lock" in source

    def test_both_decision_paths_take_the_lock(self):
        source = (ROUTES / "curate.py").read_text()

        # approve() and reject() are both state-changing decisions.
        assert source.count("_load_pending(session, doc_id, user, lock=True)") == 2

    def test_read_only_paths_do_not_lock(self):
        """Locking the queue view would serialise every curator's page load
        against every decision in flight."""
        source = inspect.getsource(curate._load_pending)

        assert "lock: bool = False" in source, "locking must be opt-in, not the default"


class TestLogoutIsAStateChange:
    def test_logout_is_a_post_with_csrf(self):
        source = (ROUTES / "auth.py").read_text()

        assert '@router.post("/logout")' in source
        assert '@router.get("/logout")' not in source
        assert "_csrf: None = Depends(verify_csrf)" in inspect.getsource(auth.logout)

    def test_response_is_json_not_a_redirect(self):
        """Issue #254: a 303 here let base.html's fetch() follow the
        RP-initiated-logout redirect itself, which silently failed to end the
        Keycloak SSO session -- see test_logout_254.py. The client now
        navigates there itself, so this route must hand back the target
        rather than redirect to it."""
        source = inspect.getsource(auth.logout)

        assert "JSONResponse" in source
        assert "RedirectResponse" not in source

    def test_the_nav_no_longer_links_to_it(self):
        source = (TEMPLATES / "base.html").read_text()

        assert 'href="/auth/logout"' not in source
        assert "async function logout()" in source
        assert "method: 'POST'" in source
