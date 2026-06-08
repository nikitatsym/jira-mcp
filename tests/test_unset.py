"""Unit tests for `_UNSET` plumbing — registry sentinel + end-to-end through
`_build_params_model` + `_coerce_call`. `_body()` lands in PR2 with a
companion test file.
"""

from __future__ import annotations

import json

from jira_mcp.registry import _UNSET, _Unset
from jira_mcp.server import _build_params_model, _coerce_call


class TestUnsetSingleton:
    def test_singleton_identity(self):
        assert _Unset() is _UNSET
        assert _Unset() is _Unset()

    def test_falsy(self):
        assert not bool(_UNSET)

    def test_repr(self):
        assert repr(_UNSET) == "_UNSET"


class TestUnsetThroughParamsModel:
    """A Pydantic model built from a `default = _UNSET` signature preserves
    omission through `model_dump(exclude_unset=True)`.
    """

    def _wired(self, fn):
        fn._params_model = _build_params_model(fn)
        return fn

    def test_omitted_means_unset_at_call_site(self):
        def fn(issue_key: str, assignee: str = _UNSET):
            """Test."""
            return assignee

        self._wired(fn)
        # Caller omits the field → fn sees its own _UNSET default.
        assert _coerce_call(fn, {"issue_key": "STS-1"}, "Fn") is _UNSET

    def test_explicit_value_passes_through(self):
        def fn(issue_key: str, assignee: str = _UNSET):
            """Test."""
            return assignee

        self._wired(fn)
        assert _coerce_call(
            fn, {"issue_key": "STS-1", "assignee": "ari"}, "Fn",
        ) == "ari"

    def test_help_does_not_leak_pydantic_undefined(self):
        """JSON Schema must not carry PydanticUndefined for `_UNSET`-defaulted
        fields — leaking it would be a discovery bug for agents.
        """
        def fn(issue_key: str, assignee: str = _UNSET):
            """Test."""

        model = _build_params_model(fn)
        schema = model.model_json_schema()
        assert "assignee" not in schema.get("required", [])
        dumped = json.dumps(schema, default=str)
        assert "PydanticUndefined" not in dumped
