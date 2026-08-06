"""Issue #496: this route's whole reason to exist is proxying the caller's
own query to orchestration-mcp's /debug/rag_search under their own token
(app/routes/search.py's docstring, ARCHITECTURE.md §4.4). #125/#214 kept
query text out of the audit log and out of that route's URL specifically
because a question asked of a classified corpus is itself sensitive -- this
proxy was undoing that on every call by sending the query as a URL param
instead of a JSON body, landing it in every proxy/ingress log between this
service and orchestration-mcp.
"""

from __future__ import annotations

import httpx

from app.routes import search


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"results": []}


def test_query_goes_in_the_json_body_not_the_url(monkeypatch):
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(httpx, "post", _fake_post)

    search.search_query(query="a sensitive question", top_k=5, token="tok")  # type: ignore[call-arg]

    assert "query=" not in captured["url"]
    assert captured.get("params") is None
    assert captured["json"] == {"query": "a sensitive question", "top_k": 5}


def test_the_bearer_token_is_still_forwarded_unchanged(monkeypatch):
    captured: dict = {}

    def _fake_post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(httpx, "post", _fake_post)

    search.search_query(query="q", top_k=5, token="the-callers-own-token")  # type: ignore[call-arg]

    assert captured["headers"]["Authorization"] == "Bearer the-callers-own-token"
