"""Tests for assign_issue: required account_id + value-check."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from jira_mcp.server import _build_params_model
from jira_mcp.tools import assign_issue


def _put_then_get(returned_account_id: str | None):
    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and req.url.path.endswith("/assignee"):
            return httpx.Response(204)
        if req.method == "GET" and "/issue/" in req.url.path:
            assignee = (
                {"accountId": returned_account_id}
                if returned_account_id is not None else None
            )
            return httpx.Response(200, json={
                "fields": {"assignee": assignee},
            })
        return httpx.Response(500, json={"err": "unexpected"})

    return _h


class TestRequiredContract:
    """Three states of the account_id contract."""

    def test_omit_account_id_raises(self):
        model = _build_params_model(assign_issue)
        with pytest.raises(ValidationError):
            model.model_validate({"issue_key": "MCPT-1"})

    def test_none_account_id_raises(self):
        model = _build_params_model(assign_issue)
        with pytest.raises(ValidationError):
            model.model_validate({"issue_key": "MCPT-1", "account_id": None})

    def test_string_account_id_passes(self, mock_jira):
        mock_jira.handler(_put_then_get("abc123"))
        out = assign_issue(issue_key="MCPT-1", account_id="abc123")
        assert out["status"] == "ok"
        assert out["assignee"] == "abc123"


class TestPutBodyShape:
    def test_put_sends_account_id(self, mock_jira):
        mock_jira.handler(_put_then_get("abc123"))
        assign_issue(issue_key="MCPT-1", account_id="abc123")
        put = next(r for r in mock_jira.requests if r.method == "PUT")
        assert json.loads(put.content) == {"accountId": "abc123"}


class TestValueCheck:
    def test_substituted_account_id_raises(self, mock_jira):
        """Jira may swap an unknown accountId for project default — caught."""
        mock_jira.handler(_put_then_get("project-default-user"))
        with pytest.raises(ValueError) as exc:
            assign_issue(issue_key="MCPT-1", account_id="abc123")
        msg = str(exc.value)
        assert "abc123" in msg
        assert "project-default-user" in msg
