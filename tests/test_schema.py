"""Unit tests for `_build_schema` — JSON Schema introspection.

`operation='schema'` returns the per-op JSON Schema (`params={'op': ...}`)
or the list of op names (`params={}`). Asserted against synthetic ops so
the test doesn't bind to real jira-mcp op shapes (those arrive in PR2).
"""

from __future__ import annotations

import pytest

from jira_mcp import server
from jira_mcp.server import _build_params_model, _build_schema, _dispatch


@pytest.fixture(autouse=True)
def _isolate_state():
    saved_ops = dict(server._group_ops)
    saved_all = dict(server._all_grouped)
    server._group_ops.clear()
    server._all_grouped.clear()
    yield
    server._group_ops.clear()
    server._all_grouped.clear()
    server._group_ops.update(saved_ops)
    server._all_grouped.update(saved_all)


def _seed(synthetic_ops):
    """Install conftest's synthetic ops into the server registry."""
    for group_name, ops in synthetic_ops.items():
        server._group_ops[group_name] = {}
        for pascal, fn in ops.items():
            fn._params_model = _build_params_model(fn)
            server._group_ops[group_name][pascal] = fn
            server._all_grouped[pascal] = group_name


class TestSchemaListing:
    def test_schema_no_op_lists_names(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _build_schema("test_read", None)
        assert "operations" in out
        assert "ListThings" in out["operations"]
        assert "GetThing" in out["operations"]
        assert "hint" in out

    def test_schema_via_dispatch(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _dispatch("schema", "test_read", {})
        assert out["operations"] == sorted(["ListThings", "GetThing"])


class TestSchemaPerOp:
    def test_schema_returns_json_schema_with_required(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _build_schema("test_write", "CreateThing")
        assert "properties" in out
        assert "owner" in out["properties"]
        assert "name" in out["properties"]
        # `body`, `priority`, `archived` have defaults → not in required.
        # `owner`, `name` are required.
        assert set(out["required"]) == {"owner", "name"}

    def test_schema_carries_field_description(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _build_schema("test_write", "CreateThing")
        body = out["properties"]["body"]
        assert "Free-text body" in body.get("description", "")
        assert "<brief>" in body["description"]

    def test_schema_carries_literal_enum(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _build_schema("test_write", "CreateThing")
        priority = out["properties"]["priority"]
        # Literal["low","medium","high"] → enum in the schema.
        assert priority.get("enum") == ["low", "medium", "high"]

    def test_schema_carries_op_docstring(self, synthetic_ops):
        _seed(synthetic_ops)
        out = _build_schema("test_write", "CreateThing")
        assert "Create a thing" in out.get("description", "")

    def test_schema_unknown_op_raises_with_list(self, synthetic_ops):
        _seed(synthetic_ops)
        with pytest.raises(ValueError) as exc:
            _build_schema("test_write", "Nonexistent")
        msg = str(exc.value)
        assert "Nonexistent" in msg
        # Suggests what's available.
        assert "CreateThing" in msg or "UpdateThing" in msg
