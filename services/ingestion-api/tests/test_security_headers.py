"""Issues #444/#445: confirms the shared SecurityHeadersMiddleware is wired
into this app with the X-Frame-Options extra header #444 needs.

Introspects the real `app.user_middleware` stack rather than driving the app
through TestClient -- importing app.main is side-effect free (the FastAPI
object is built at import time either way), but running a request through it
would trigger main.py's lifespan, which opens a live NATS connection this
suite has no business standing up (same reasoning as test_login_gate.py).
"""

from __future__ import annotations

from app import main
from common.security_headers import SecurityHeadersMiddleware


def test_security_headers_middleware_is_installed():
    installed = [m for m in main.app.user_middleware if m.cls is SecurityHeadersMiddleware]

    assert len(installed) == 1


def test_x_frame_options_deny_is_passed_as_an_extra_header():
    (installed,) = [m for m in main.app.user_middleware if m.cls is SecurityHeadersMiddleware]

    assert installed.kwargs["extra_headers"] == ((b"x-frame-options", b"DENY"),)
