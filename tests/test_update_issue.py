"""Tests for update_issue: resolution rejection + returnIssue=true path."""

from __future__ import annotations

import json

import httpx
import pytest

from jira_mcp.tools import update_issue


@pytest.fixture(autouse=True)
def _disable_brief(monkeypatch):
    monkeypatch.setenv("MCP_JIRA_BRIEF_MAX", "0")
    from jira_mcp.config import _reset_settings
    _reset_settings()


def _put_returns_issuebean(fields_returned: dict):
    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and "/issue/" in req.url.path:
            return httpx.Response(200, json={
                "id": "10001",
                "key": "MCPT-1",
                "fields": fields_returned,
            })
        return httpx.Response(500, json={"err": "unexpected"})

    return _h


class TestResolutionRejection:
    def test_resolution_in_custom_fields_rejected(self, mock_jira):
        with pytest.raises(ValueError) as exc:
            update_issue(
                issue_key="MCPT-1",
                custom_fields={"resolution": {"name": "Done"}},
            )
        msg = str(exc.value)
        assert "TransitionIssue" in msg or "transition_issue" in msg

    def test_plain_fields_pass(self, mock_jira):
        mock_jira.handler(_put_returns_issuebean({"summary": "new"}))
        update_issue(issue_key="MCPT-1", summary="new")


class TestReturnIssueTrue:
    def test_uses_return_issue_query(self, mock_jira):
        mock_jira.handler(_put_returns_issuebean({"summary": "new"}))
        update_issue(issue_key="MCPT-1", summary="new")
        put = next(r for r in mock_jira.requests if r.method == "PUT")
        assert put.url.params.get("returnIssue") == "true"

    def test_only_one_request(self, mock_jira):
        """update_issue should NOT do a follow-up GET — PUT carries the issue."""
        mock_jira.handler(_put_returns_issuebean({"summary": "new"}))
        update_issue(issue_key="MCPT-1", summary="new")
        assert len(mock_jira.requests) == 1
        assert mock_jira.requests[0].method == "PUT"

    def test_silent_drop_raises(self, mock_jira):
        """Sent summary but response doesn't include it."""
        mock_jira.handler(_put_returns_issuebean({"other": "x"}))
        with pytest.raises(ValueError) as exc:
            update_issue(issue_key="MCPT-1", summary="new")
        assert "summary" in str(exc.value)

    def test_no_fields_to_update_skips_call(self, mock_jira):
        out = update_issue(issue_key="MCPT-1")
        assert out["status"] == "ok"
        # No HTTP made.
        assert mock_jira.requests == []
