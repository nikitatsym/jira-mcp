"""Tests for unassign_issue: dedicated null-body path."""

from __future__ import annotations

import json

import httpx
import pytest

from jira_mcp.tools import unassign_issue


def _put_then_get(returned_assignee):
    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and req.url.path.endswith("/assignee"):
            return httpx.Response(204)
        if req.method == "GET" and "/issue/" in req.url.path:
            return httpx.Response(200, json={
                "fields": {"assignee": returned_assignee},
            })
        return httpx.Response(500, json={"err": "unexpected"})

    return _h


class TestUnassign:
    def test_put_sends_null_account_id(self, mock_jira):
        mock_jira.handler(_put_then_get(None))
        unassign_issue(issue_key="MCPT-1")
        put = next(r for r in mock_jira.requests if r.method == "PUT")
        assert json.loads(put.content) == {"accountId": None}

    def test_returns_ok(self, mock_jira):
        mock_jira.handler(_put_then_get(None))
        out = unassign_issue(issue_key="MCPT-1")
        assert out["status"] == "ok"

    def test_get_uses_assignee_only(self, mock_jira):
        mock_jira.handler(_put_then_get(None))
        unassign_issue(issue_key="MCPT-1")
        get = next(r for r in mock_jira.requests if r.method == "GET")
        assert get.url.params.get("fields") == "assignee"

    def test_silent_no_op_raises(self, mock_jira):
        """If PUT silently kept the previous assignee, fail loudly."""
        mock_jira.handler(_put_then_get({"accountId": "still-here"}))
        with pytest.raises(ValueError) as exc:
            unassign_issue(issue_key="MCPT-1")
        assert "expected null" in str(exc.value)
