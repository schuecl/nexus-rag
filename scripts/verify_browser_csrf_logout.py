"""Browser-level verification of CSRF double-submit and logout, against a real
Chromium instance driving the real `docker compose up` stack -- the live-
verification gap tracked by issue #187 (split from #77).

`TestClient` coverage (services/ingestion-api/tests) already exercises the
server-side logic: mismatched/missing X-CSRF-Token rejected, matching header
passes, bearer-token callers unaffected, logout clears both cookies. What it
cannot exercise is the browser mechanics that logic depends on -- whether the
double-submit cookie's HttpOnly/SameSite attributes actually behave as
intended in a real browser, whether the page's own JS
(base.html's authHeaders()) correctly reads and echoes the CSRF cookie, and
whether the full Keycloak RP-initiated logout redirect chain
(id_token_hint + post_logout_redirect_uri) round-trips through a real browser
session rather than just returning the right target URL as JSON.

Needs the dev CA trust + `/etc/hosts` alias for `keycloak` (docs/dev-setup.md
-- browser OIDC login requires the same host name Keycloak stamps into its
discovery metadata) and a Chromium binary. Not part of the repo's fast
unit/BDD gate (no `playwright` dependency there); run on demand:

    playwright install --with-deps chromium
    pip install playwright httpx
    INGESTION_API_URL=http://localhost:8001 python3 verify_browser_csrf_logout.py

OIDC_REDIRECT_URI is a fixed value baked into ingestion-api's own config
(app/routes/auth.py), not derived from the request -- so this has to run
against the same host:port a real developer's browser would (the Compose
file's host-mapped 127.0.0.1:8001/8443), not the compose-network hostnames
the other scripts/ helpers use.
"""

from __future__ import annotations

import os
import sys
import uuid

from playwright.sync_api import BrowserContext, sync_playwright

INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://localhost:8001")
USERNAME = os.environ.get("VERIFY_USERNAME", "bob-query")
# nosec B105: seeded dev realm password (infra/keycloak/realm-export), not a real credential.
PASSWORD = "devpass123"  # nosec B105
SESSION_COOKIE = "nexus_rag_session"
CSRF_COOKIE = "nexus_rag_csrf"
CSRF_HEADER = "X-CSRF-Token"
NAV_TIMEOUT_MS = 20_000


def log(msg: str) -> None:
    print(f"  {msg}")


def cookie(context: BrowserContext, name: str) -> dict | None:
    return next((c for c in context.cookies() if c["name"] == name), None)


def login(page) -> None:
    page.goto(INGESTION_API_URL + "/", timeout=NAV_TIMEOUT_MS)
    page.click("#loginButton")
    page.wait_for_selector("#username", timeout=NAV_TIMEOUT_MS)
    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#kc-login")
    page.wait_for_selector(".user-summary strong", timeout=NAV_TIMEOUT_MS)
    seen = page.locator(".user-summary strong").inner_text()
    if seen != USERNAME:
        raise RuntimeError(f"logged in as {seen!r}, expected {USERNAME!r}")


def check_cookie_attributes(context: BrowserContext, page, failures: list[str]) -> None:
    session = cookie(context, SESSION_COOKIE)
    csrf = cookie(context, CSRF_COOKIE)
    if session is None or not session["httpOnly"]:
        failures.append("session cookie missing or not HttpOnly in a real browser")
    else:
        log("Session cookie is HttpOnly, as configured.")
    if csrf is None or csrf["httpOnly"]:
        failures.append("CSRF cookie missing or unexpectedly HttpOnly (JS must be able to read it)")
    else:
        log("CSRF cookie is readable (not HttpOnly), as configured.")

    js_readable = page.evaluate("document.cookie")
    if csrf is not None and CSRF_COOKIE not in js_readable:
        failures.append("page JS cannot read the CSRF cookie via document.cookie")
    if SESSION_COOKIE in js_readable:
        failures.append("page JS can read the session cookie -- HttpOnly not actually enforced")
    if not failures:
        log("base.html's authHeaders() can read exactly the cookie it's supposed to.")


def check_csrf_double_submit(context: BrowserContext, csrf_value: str, failures: list[str]) -> None:
    # POST /notifications/{id}/read: verify_csrf runs before the route body,
    # so a valid token past the gate reaches "no such notification" (404) --
    # a clean differential from the gate's own 403, without needing a real
    # document upload's tagging/classification validation just to reach a
    # state-changing route.
    probe_url = INGESTION_API_URL + f"/notifications/{uuid.uuid4()}/read"

    resp = context.request.post(probe_url)
    if resp.status != 403:
        failures.append(f"missing X-CSRF-Token: expected 403, got {resp.status}")
    else:
        log("Missing X-CSRF-Token correctly rejected (403).")

    resp = context.request.post(probe_url, headers={CSRF_HEADER: "not-the-real-token"})
    if resp.status != 403:
        failures.append(f"mismatched X-CSRF-Token: expected 403, got {resp.status}")
    else:
        log("Mismatched X-CSRF-Token correctly rejected (403).")

    resp = context.request.post(probe_url, headers={CSRF_HEADER: csrf_value})
    if resp.status != 404:
        failures.append(f"matching X-CSRF-Token: expected 404 past the gate, got {resp.status}")
    else:
        log("Matching X-CSRF-Token correctly passed the gate (404 past it, as expected).")


def check_logout_and_relogin(context: BrowserContext, page, failures: list[str]) -> None:
    page.click("button.account-action")
    page.wait_for_selector("#loginButton", timeout=NAV_TIMEOUT_MS)
    log("Logout navigated all the way back to the anonymous landing page.")

    if cookie(context, SESSION_COOKIE) is not None or cookie(context, CSRF_COOKIE) is not None:
        failures.append("session/CSRF cookie still present in the browser after logout")
    else:
        log("Both cookies cleared from the browser after logout.")

    resp = context.request.get(INGESTION_API_URL + "/notifications")
    if resp.status != 401:
        failures.append(f"authenticated API call after logout: expected 401, got {resp.status}")
    else:
        log("A subsequent authenticated request correctly fails (401) after logout.")

    # The real assertion behind issue #254: does the Keycloak SSO session
    # actually end, or does logging back in silently re-authenticate the same
    # user off a still-live SSO cookie? Only a real Keycloak redirect can
    # answer this -- re-prompting for credentials is the only observable
    # difference between the two.
    page.click("#loginButton")
    page.wait_for_selector("#username", timeout=NAV_TIMEOUT_MS)
    log("Logging back in reached Keycloak's real credential prompt (not a silent SSO bounce).")
    login_again = page.locator("#username")
    if login_again.input_value() not in ("", None):
        failures.append("Keycloak pre-filled the username -- looks like a silent SSO bounce")

    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    page.click("#kc-login")
    page.wait_for_selector(".user-summary strong", timeout=NAV_TIMEOUT_MS)
    log("Re-login with fresh credentials succeeds; a real re-prompt, not a bypass.")


def main() -> int:
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # ignore_https_errors: the dev CA (infra/certs/ca.crt) issues
        # Keycloak's cert; this test cares about cookie/redirect mechanics,
        # not about re-proving TLS chain validation dev-setup.md's manual CA
        # trust step already covers for a real developer's browser.
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        log("Logging in via the real Keycloak Authorization Code + PKCE redirect...")
        login(page)

        log("Checking cookie attributes as a real browser sees them...")
        check_cookie_attributes(context, page, failures)

        csrf = cookie(context, CSRF_COOKIE)
        if csrf is not None:
            log("Probing CSRF double-submit enforcement...")
            check_csrf_double_submit(context, csrf["value"], failures)
        else:
            failures.append("no CSRF cookie available -- skipped double-submit checks")

        log("Logging out and confirming the Keycloak SSO session actually ends...")
        check_logout_and_relogin(context, page, failures)

        browser.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1
    print("CSRF double-submit and logout/re-login behave correctly in a real browser.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
