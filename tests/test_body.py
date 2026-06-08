"""Unit tests for _body, _fields, _prepare_issue_fields."""

from __future__ import annotations

import pytest

from jira_mcp.prepare import _body, _fields, _prepare_issue_fields
from jira_mcp.registry import _UNSET


class TestBody:
    def test_drops_unset(self):
        assert _body({"a": 1, "b": _UNSET, "c": 3}) == {"a": 1, "c": 3}

    def test_drops_none_by_default(self):
        assert _body({"a": 1, "b": None, "c": 3}) == {"a": 1, "c": 3}

    def test_keeps_none_for_listed(self):
        out = _body({"a": 1, "assignee": None}, keep_null=("assignee",))
        assert out == {"a": 1, "assignee": None}

    def test_keep_null_still_drops_unset(self):
        out = _body({"assignee": _UNSET, "duedate": None},
                    keep_null=("assignee", "duedate"))
        assert out == {"duedate": None}

    def test_exclude(self):
        out = _body({"issue_key": "X", "summary": "y"}, exclude=("issue_key",))
        assert out == {"summary": "y"}

    def test_rename(self):
        out = _body({"due_date": "2026-01-01"},
                    rename={"due_date": "duedate"})
        assert out == {"duedate": "2026-01-01"}

    def test_keep_null_uses_python_names(self):
        """keep_null is matched on the Python name (pre-rename)."""
        out = _body(
            {"due_date": None},
            rename={"due_date": "duedate"},
            keep_null=("due_date",),
        )
        assert out == {"duedate": None}


class TestFields:
    def test_wraps_in_fields_envelope(self):
        assert _fields({"summary": "x"}) == {"fields": {"summary": "x"}}


class TestPrepareIssueFields:
    def test_description_to_adf(self):
        out = _prepare_issue_fields({"description": "hello"})
        assert out["description"]["type"] == "doc"
        # ADF paragraph with the text
        assert any(
            n.get("type") == "paragraph"
            for n in out["description"]["content"]
        )

    def test_environment_to_adf(self):
        out = _prepare_issue_fields({"environment": "prod"})
        assert out["environment"]["type"] == "doc"

    def test_assignee_to_account_id_wrapper(self):
        out = _prepare_issue_fields({"assignee": "abc123"})
        assert out["assignee"] == {"accountId": "abc123"}

    def test_assignee_none_passes_through(self):
        """keep_null → None stays None so the wire sees JSON null."""
        out = _prepare_issue_fields({"assignee": None})
        assert out["assignee"] is None

    def test_priority_to_name_wrapper(self):
        out = _prepare_issue_fields({"priority": "High"})
        assert out["priority"] == {"name": "High"}

    def test_parent_to_key_wrapper(self):
        out = _prepare_issue_fields({"parent": "STS-238"})
        assert out["parent"] == {"key": "STS-238"}

    def test_components_to_named_list(self):
        out = _prepare_issue_fields({"components": ["api", "ui"]})
        assert out["components"] == [{"name": "api"}, {"name": "ui"}]

    def test_fix_versions_to_named_list(self):
        out = _prepare_issue_fields({"fixVersions": ["1.0", "2.0"]})
        assert out["fixVersions"] == [{"name": "1.0"}, {"name": "2.0"}]

    def test_labels_passthrough(self):
        out = _prepare_issue_fields({"labels": ["a", "b"]})
        assert out["labels"] == ["a", "b"]

    def test_duedate_passthrough(self):
        out = _prepare_issue_fields({"duedate": "2026-12-01"})
        assert out["duedate"] == "2026-12-01"


class TestCustomFieldsMerge:
    def test_flat_merge(self):
        out = _prepare_issue_fields({
            "summary": "x",
            "custom_fields": {"customfield_10014": "EPIC-1"},
        })
        assert out["customfield_10014"] == "EPIC-1"
        assert "custom_fields" not in out

    def test_collision_with_named_param(self):
        with pytest.raises(ValueError) as exc:
            _prepare_issue_fields({
                "summary": "A",
                "custom_fields": {"summary": "B"},
            })
        assert "collide" in str(exc.value)
        assert "summary" in str(exc.value)

    def test_shadow_wire_name(self):
        with pytest.raises(ValueError) as exc:
            _prepare_issue_fields({
                "custom_fields": {"assignee": "abc"},
            })
        assert "assignee" in str(exc.value)
        assert "dedicated" in str(exc.value)

    def test_shadow_wire_name_parent(self):
        with pytest.raises(ValueError) as exc:
            _prepare_issue_fields({
                "custom_fields": {"parent": {"key": "X-1"}},
            })
        assert "parent" in str(exc.value)

    def test_empty_custom_fields_ok(self):
        out = _prepare_issue_fields({"summary": "x", "custom_fields": {}})
        assert out == {"summary": "x"}

    def test_custom_fields_must_be_dict(self):
        with pytest.raises(ValueError):
            _prepare_issue_fields({"custom_fields": ["a", "b"]})
