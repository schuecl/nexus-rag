"""Issue #445: confirms the shared SecurityHeadersMiddleware is wired into
this service's Starlette app (the bare instance MCPServer.streamable_http_app()
returns, not a FastAPI app -- see common/security_headers.py's docstring on
why that rules out FastAPI's `.middleware("http")` decorator here).
"""

from __future__ import annotations

from app import server
from common.security_headers import SecurityHeadersMiddleware


def test_security_headers_middleware_is_installed():
    installed = [m for m in server.app.user_middleware if m.cls is SecurityHeadersMiddleware]

    assert len(installed) == 1


def test_no_extra_headers_for_this_service():
    """#444's X-Frame-Options is ingestion-api's own concern (it serves the
    curation UI); this service has no HTML to frame."""
    (installed,) = [m for m in server.app.user_middleware if m.cls is SecurityHeadersMiddleware]

    assert installed.kwargs.get("extra_headers", ()) == ()
