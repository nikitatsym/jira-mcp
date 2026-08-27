from __future__ import annotations

import httpx
import pytest

from jira_mcp import server
from jira_mcp.client import APIError


def _raise(exc: Exception):
    raise exc


def test_dispatch_returns_contextual_api_error(monkeypatch):
    monkeypatch.setattr(
        server,
        "_coerce_call",
        lambda *_args: _raise(APIError(404, "GET", "/rest/api/3/issue?token=secret", {"detail": "missing"})),
    )

    result = server._dispatch("GetIssue", "jira_read", {"issue_key": "TEST-1"})

    assert result["error"].startswith("GET /rest/api/3/issue -> 404")
    assert "secret" not in result["error"]


def test_dispatch_redacts_transport_query_values(monkeypatch):
    request = httpx.Request("GET", "https://jira.example/rest/api/3/issue?token=secret")
    monkeypatch.setattr(
        server,
        "_coerce_call",
        lambda *_args: _raise(httpx.ConnectError("connection refused", request=request)),
    )

    result = server._dispatch("GetIssue", "jira_read", {"issue_key": "TEST-1"})

    assert "Jira transport failure: GET /rest/api/3/issue: ConnectError" in result["error"]
    assert "secret" not in result["error"]
    assert "token=" not in result["error"]


def test_dispatch_returns_missing_parameter_error():
    result = server._dispatch("GetIssue", "jira_read", {})

    assert "Invalid params for GetIssue" in result["error"]
    assert "issue_key" in result["error"]


def test_dispatch_propagates_programming_error(monkeypatch):
    monkeypatch.setattr(
        server, "_coerce_call", lambda *_args: _raise(AttributeError("programming error"))
    )

    with pytest.raises(AttributeError):
        server._dispatch("GetIssue", "jira_read", {"issue_key": "TEST-1"})


def test_error_text_redacts_secret_fields():
    """Container values are redacted whole: a partial match would leave the
    tail of a list or nested dict in the reported error."""
    text = server._redact_error_text(
        {"password": ["too short", "p@ssw0rd"], "api_secret": "zzz", "detail": "keep me"}
    )

    assert "p@ssw0rd" not in text
    assert "zzz" not in text
    assert "keep me" in text
