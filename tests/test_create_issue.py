"""Wire-shape and negative-case tests for create_issue."""

from __future__ import annotations

import json

import httpx
import pytest

from jira_mcp.tools import create_issue


@pytest.fixture(autouse=True)
def _disable_brief(monkeypatch):
    """Brief enforcement off so tests focus on wire shape."""
    monkeypatch.setenv("MCP_JIRA_BRIEF_MAX", "0")
    from jira_mcp.config import _reset_settings
    _reset_settings()


def _post_then_get(
    post_response: dict,
    extra_fields: dict | None = None,
    drop_keys: set | None = None,
):
    """POST returns post_response. GET echoes the sent payload's fields,
    optionally injecting extra_fields and dropping drop_keys (to simulate
    silent drops)."""
    sent_payload: dict = {}

    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/issue"):
            sent_payload.update(
                (json.loads(req.content) or {}).get("fields", {})
            )
            return httpx.Response(201, json=post_response)
        if req.method == "GET" and "/issue/" in req.url.path:
            echoed = {k: v for k, v in sent_payload.items()
                      if k not in (drop_keys or set())}
            echoed.update(extra_fields or {})
            return httpx.Response(200, json={
                "id": post_response.get("id", "10001"),
                "key": post_response.get("key", "MCPT-1"),
                "fields": echoed,
            })
        return httpx.Response(500, json={"err": "unexpected"})

    return _h


class TestPositiveWireShapes:
    def test_parent_key_to_parent_dot_key(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-3"}))
        create_issue(
            project_key="MCPT", issue_type="Task", summary="x",
            parent_key="MCPT-2",
        )
        post = next(r for r in mock_jira.requests if r.method == "POST")
        body = json.loads(post.content)
        assert body["fields"]["parent"] == {"key": "MCPT-2"}

    def test_custom_fields_flat_merge(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-4"}))
        create_issue(
            project_key="MCPT", issue_type="Task", summary="x",
            custom_fields={"customfield_10014": "EPIC-1"},
        )
        post = next(r for r in mock_jira.requests if r.method == "POST")
        body = json.loads(post.content)
        assert body["fields"]["customfield_10014"] == "EPIC-1"
        assert "custom_fields" not in body["fields"]

    def test_fix_versions_wire_shape(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-5"}))
        create_issue(
            project_key="MCPT", issue_type="Task", summary="x",
            fix_versions=["1.0"],
        )
        post = next(r for r in mock_jira.requests if r.method == "POST")
        body = json.loads(post.content)
        assert body["fields"]["fixVersions"] == [{"name": "1.0"}]
        assert "fix_versions" not in body["fields"]


class TestNegativeCases:
    def test_camelcase_parent_key_rejected_at_dispatch(self):
        """Unknown wire-name via dispatch raises field-level Pydantic error."""
        from jira_mcp.server import _coerce_call, _build_params_model
        create_issue._params_model = _build_params_model(create_issue)
        with pytest.raises(ValueError) as exc:
            _coerce_call(create_issue, {
                "project_key": "MCPT", "issue_type": "Task", "summary": "x",
                "parentKey": "MCPT-2",
            }, "CreateIssue")
        assert "parentKey" in str(exc.value)

    def test_custom_fields_summary_collision(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-1"}))
        with pytest.raises(ValueError) as exc:
            create_issue(
                project_key="MCPT", issue_type="Task", summary="A",
                custom_fields={"summary": "B"},
            )
        msg = str(exc.value)
        assert "summary" in msg
        assert "collide" in msg

    def test_custom_fields_assignee_shadow(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-1"}))
        with pytest.raises(ValueError) as exc:
            create_issue(
                project_key="MCPT", issue_type="Task", summary="x",
                custom_fields={"assignee": "abc"},
            )
        msg = str(exc.value)
        assert "assignee" in msg
        assert "dedicated" in msg


class TestVerifyAfterCreate:
    def test_silent_drop_raises(self, mock_jira):
        mock_jira.handler(_post_then_get(
            {"key": "MCPT-6"},
            drop_keys={"duedate"},
        ))
        with pytest.raises(ValueError) as exc:
            create_issue(
                project_key="MCPT", issue_type="Task", summary="x",
                due_date="2026-12-01",
            )
        assert "duedate" in str(exc.value)

    def test_fields_query_uses_csv_of_sent_keys(self, mock_jira):
        mock_jira.handler(_post_then_get({"key": "MCPT-7"}))
        create_issue(
            project_key="MCPT", issue_type="Task", summary="x",
            due_date="2026-12-01",
        )
        get_req = next(r for r in mock_jira.requests if r.method == "GET")
        fields_q = sorted(get_req.url.params.get("fields", "").split(","))
        assert fields_q == ["duedate", "summary"]


class TestBriefEnforcement:
    def test_brief_required_when_max_gt_zero(self, mock_jira, monkeypatch):
        monkeypatch.setenv("MCP_JIRA_BRIEF_MAX", "100")
        from jira_mcp.config import _reset_settings
        _reset_settings()
        with pytest.raises(ValueError) as exc:
            create_issue(
                project_key="MCPT", issue_type="Task", summary="x",
                description="no brief tag here",
            )
        assert "<brief>" in str(exc.value)
