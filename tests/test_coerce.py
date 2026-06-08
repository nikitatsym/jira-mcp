"""Unit tests for `_build_params_model` — Pydantic params model construction.

Focused tests for the validator path. Synthetic, no env required.
"""

from __future__ import annotations

from typing import Optional

import pytest

from jira_mcp.server import _build_params_model, _coerce_call, _format_validation_error
from pydantic import ValidationError


class TestStringBoolCoercion:
    """MCP clients sometimes pass `"true"`/`"false"` as JSON strings rather
    than booleans. `_build_params_model` coerces those via a field validator.
    """

    def _model(self, fn):
        return _build_params_model(fn)

    def test_string_true_coerced(self):
        def fn(flag: bool):
            """Test."""
            return flag

        model = self._model(fn)
        assert model.model_validate({"flag": "true"}).flag is True
        assert model.model_validate({"flag": "True"}).flag is True
        assert model.model_validate({"flag": "yes"}).flag is True
        assert model.model_validate({"flag": "1"}).flag is True

    def test_string_false_coerced(self):
        def fn(flag: bool):
            """Test."""
            return flag

        model = self._model(fn)
        assert model.model_validate({"flag": "false"}).flag is False
        assert model.model_validate({"flag": "False"}).flag is False
        assert model.model_validate({"flag": "no"}).flag is False
        assert model.model_validate({"flag": "0"}).flag is False

    def test_bool_passthrough(self):
        def fn(flag: bool):
            """Test."""
            return flag

        model = self._model(fn)
        assert model.model_validate({"flag": True}).flag is True
        assert model.model_validate({"flag": False}).flag is False

    def test_string_in_non_bool_field_not_coerced(self):
        """Coercion targets bool-typed fields only."""
        def fn(label: str):
            """Test."""
            return label

        model = self._model(fn)
        assert model.model_validate({"label": "true"}).label == "true"

    def test_optional_bool_coerced(self):
        def fn(flag: Optional[bool] = None):
            """Test."""
            return flag

        model = self._model(fn)
        assert model.model_validate({"flag": "true"}).flag is True
        assert model.model_validate({"flag": "false"}).flag is False
        assert model.model_validate({"flag": None}).flag is None
        assert model.model_validate({}).flag is None

    def test_unknown_string_left_alone(self):
        def fn(flag: bool):
            """Test."""
            return flag

        model = self._model(fn)
        with pytest.raises(ValidationError):
            model.model_validate({"flag": "maybe"})


class TestExtraForbid:
    """Existing behaviour preserved — unknown keys fail with a field-level
    Pydantic error, not the old "Unknown parameters: [...]" lump message.
    """

    def test_unknown_key_rejected(self):
        def fn(a: int):
            """Test."""
            return a

        model = _build_params_model(fn)
        with pytest.raises(ValidationError):
            model.model_validate({"a": 1, "z": 99})

    def test_unknown_key_message_field_level(self):
        """The lump replaced: error message now names the offending field."""
        def fn(a: int):
            """Test."""
            return a

        model = _build_params_model(fn)
        try:
            model.model_validate({"a": 1, "parent_key": "STS-238"})
        except ValidationError as e:
            msg = _format_validation_error(e, "Fn")
            assert "parent_key" in msg
            assert "Extra inputs are not permitted" in msg
        else:
            pytest.fail("expected ValidationError")


class TestMissingRequired:
    """A missing required param yields a field-level "Field required" error,
    not a generic message.
    """

    def test_missing_required(self):
        def fn(a: int, b: str):
            """Test."""

        fn._params_model = _build_params_model(fn)
        with pytest.raises(ValueError) as exc:
            _coerce_call(fn, {"a": 1}, "Fn")
        msg = str(exc.value)
        assert "b" in msg
        assert "Field required" in msg


class TestTypeMismatch:
    """Wrong-type values produce a field-level message naming the bad value."""

    def test_wrong_type(self):
        def fn(labels: list[int]):
            """Test."""

        fn._params_model = _build_params_model(fn)
        with pytest.raises(ValueError) as exc:
            _coerce_call(fn, {"labels": ["frontend"]}, "Fn")
        msg = str(exc.value)
        assert "labels.0" in msg
