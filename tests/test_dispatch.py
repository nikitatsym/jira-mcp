"""Unit tests for `_dispatch` structured expected-failure results.

These exercise the shared MCP dispatch boundary directly so validation and
upstream errors remain useful tool data instead of collapsing into generic
execution failures.
"""

from __future__ import annotations

import pytest

from jira_mcp import server
from jira_mcp.server import _build_params_model, _dispatch


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
    for group_name, ops in synthetic_ops.items():
        server._group_ops[group_name] = {}
        for pascal, fn in ops.items():
            fn._params_model = _build_params_model(fn)
            server._group_ops[group_name][pascal] = fn
            server._all_grouped[pascal] = group_name


class TestDispatchWrongGroup:
    def test_op_in_other_group_returns_pointer(self, synthetic_ops):
        _seed(synthetic_ops)
        # `TransitionThing` belongs to `test_execute`, not `test_write`.
        result = _dispatch("TransitionThing", "test_write", {})
        msg = result["error"]
        assert "TransitionThing" in msg
        assert "test_execute" in msg
        assert "test_write" in msg


class TestDispatchUnknownOp:
    def test_unknown_op_returns_hint(self, synthetic_ops):
        _seed(synthetic_ops)
        result = _dispatch("Nonexistent", "test_read", {})
        msg = result["error"]
        assert "Nonexistent" in msg
        assert "test_read" in msg
        # Tells the agent how to discover.
        assert "help" in msg or "schema" in msg


class TestDispatchValidArgsCallsFn:
    def test_normal_dispatch_runs(self, synthetic_ops):
        _seed(synthetic_ops)
        result = _dispatch("ListThings", "test_read", {"owner": "o", "repo": "r"})
        assert result == {"owner": "o", "repo": "r"}

    def test_unknown_param_returns_field_level_error(self, synthetic_ops):
        _seed(synthetic_ops)
        result = _dispatch(
            "ListThings",
            "test_read",
            {"owner": "o", "repo": "r", "parent_key": "STS-1"},
        )
        msg = result["error"]
        assert "parent_key" in msg
        # Field-level Pydantic message — not the old lump.
        assert "Extra inputs are not permitted" in msg
