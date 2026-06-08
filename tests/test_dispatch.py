"""Unit tests for `_dispatch` — wrong-group / unknown-op now raise instead
of returning `{"error": ...}` dicts.

Module note on limitation: these tests pin the *function-level* contract
(raise vs return). The actual regression risk lives at the FastMCP
serialization boundary — i.e. whether FastMCP surfaces the raise as a
`tool_call_error` rather than wrapping it as a successful payload. That
layer isn't unit-testable without a full FastMCP harness; the PR3
integration tests cover it end-to-end against a real Jira tenant.
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
    def test_op_in_other_group_raises_with_pointer(self, synthetic_ops):
        _seed(synthetic_ops)
        # `TransitionThing` belongs to `test_execute`, not `test_write`.
        with pytest.raises(ValueError) as exc:
            _dispatch("TransitionThing", "test_write", {})
        msg = str(exc.value)
        assert "TransitionThing" in msg
        assert "test_execute" in msg
        assert "test_write" in msg
        # Should not be a dict (regression for the old `{"error": ...}` shape).
        assert not isinstance(exc.value.args[0], dict)


class TestDispatchUnknownOp:
    def test_unknown_op_raises_with_hint(self, synthetic_ops):
        _seed(synthetic_ops)
        with pytest.raises(ValueError) as exc:
            _dispatch("Nonexistent", "test_read", {})
        msg = str(exc.value)
        assert "Nonexistent" in msg
        assert "test_read" in msg
        # Tells the agent how to discover.
        assert "help" in msg or "schema" in msg


class TestDispatchValidArgsCallsFn:
    def test_normal_dispatch_runs(self, synthetic_ops):
        _seed(synthetic_ops)
        result = _dispatch("ListThings", "test_read", {"owner": "o", "repo": "r"})
        assert result == {"owner": "o", "repo": "r"}

    def test_unknown_param_raises_field_level(self, synthetic_ops):
        """Two error-surface changes in PR1: wrong-group/unknown-op moved
        from return-dict to raise, and validation error format moved from
        "Unknown parameters: [...]" lump to Pydantic field-level.
        """
        _seed(synthetic_ops)
        with pytest.raises(ValueError) as exc:
            _dispatch("ListThings", "test_read",
                      {"owner": "o", "repo": "r", "parent_key": "STS-1"})
        msg = str(exc.value)
        assert "parent_key" in msg
        # Field-level Pydantic message — not the old lump.
        assert "Extra inputs are not permitted" in msg
