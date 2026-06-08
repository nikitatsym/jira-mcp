"""Per-op schema sanity: required keys + Field descriptions land in JSON Schema."""

from __future__ import annotations

import pytest

from jira_mcp.server import _build_params_model
from jira_mcp.tools import create_issue, update_issue, transition_issue, assign_issue


class TestCreateIssueSchema:
    def test_required_set(self):
        schema = _build_params_model(create_issue).model_json_schema()
        assert set(schema["required"]) == {"project_key", "issue_type", "summary"}

    def test_parent_key_carries_description(self):
        schema = _build_params_model(create_issue).model_json_schema()
        desc = schema["properties"]["parent_key"].get("description", "")
        assert "Parent issue key" in desc
        # Wire-rename hint surfaces to the agent.
        assert "fields.parent" in desc or "customfield" in desc

    def test_due_date_mentions_wire_name(self):
        schema = _build_params_model(create_issue).model_json_schema()
        desc = schema["properties"]["due_date"].get("description", "")
        assert "duedate" in desc

    def test_fix_versions_mentions_wire_name(self):
        schema = _build_params_model(create_issue).model_json_schema()
        desc = schema["properties"]["fix_versions"].get("description", "")
        assert "fixVersions" in desc

    def test_assignee_documents_account_id(self):
        schema = _build_params_model(create_issue).model_json_schema()
        desc = schema["properties"]["assignee"].get("description", "")
        assert "accountId" in desc


class TestAssignIssueSchema:
    def test_account_id_required(self):
        schema = _build_params_model(assign_issue).model_json_schema()
        assert "account_id" in schema["required"]


class TestTransitionIssueSchema:
    def test_resolution_optional_and_documented(self):
        schema = _build_params_model(transition_issue).model_json_schema()
        assert "resolution" not in schema["required"]
        desc = schema["properties"]["resolution"].get("description", "")
        assert "Resolution name" in desc


class TestUpdateIssueSchema:
    def test_only_issue_key_required(self):
        schema = _build_params_model(update_issue).model_json_schema()
        assert schema["required"] == ["issue_key"]
