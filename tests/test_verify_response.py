"""Unit tests for _verify_response and _verify_top_level."""

from __future__ import annotations

import pytest

from jira_mcp.prepare import _verify_response, _verify_top_level


class TestVerifyResponseHappyPath:
    def test_all_keys_present(self):
        _verify_response({"summary": "x"}, {"fields": {"summary": "x"}})

    def test_extra_received_keys_ignored(self):
        _verify_response(
            {"summary": "x"},
            {"fields": {"summary": "x", "extra": 1}},
        )

    def test_skip_set_honored(self):
        _verify_response(
            {"summary": "x", "project": "X"},
            {"fields": {"summary": "x"}},
            skip={"project"},
        )

    def test_empty_sent_is_noop(self):
        _verify_response({}, None)
        _verify_response({}, {"weird": "shape"})


class TestVerifyResponseFailures:
    def test_silent_drop(self):
        with pytest.raises(ValueError) as exc:
            _verify_response(
                {"summary": "x", "duedate": "2026-12-01"},
                {"fields": {"summary": "x"}},
            )
        assert "duedate" in str(exc.value)
        assert "silently dropped" in str(exc.value)

    def test_empty_dict_raises(self):
        """Empty dict triggers the empty-or-non-dict branch (truthiness)."""
        with pytest.raises(ValueError) as exc:
            _verify_response({"summary": "x"}, {})
        assert "empty" in str(exc.value)

    def test_missing_fields_key_raises(self):
        with pytest.raises(ValueError) as exc:
            _verify_response({"summary": "x"}, {"id": "1", "key": "MCPT-1"})
        msg = str(exc.value)
        assert "fields" in msg

    def test_none_received_raises(self):
        with pytest.raises(ValueError) as exc:
            _verify_response({"summary": "x"}, None)
        assert "empty" in str(exc.value) or "did not include" in str(exc.value)

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError):
            _verify_response({"summary": "x"}, {"fields": {}})


class TestVerifyTopLevel:
    def test_happy(self):
        _verify_top_level(
            {"body": "x", "visibility": {"type": "role"}},
            {"id": "1", "body": "x", "visibility": {"type": "role"}},
        )

    def test_skip_body(self):
        _verify_top_level(
            {"body": "plain text", "visibility": {"type": "role"}},
            {"id": "1", "body": {"adf": "..."}, "visibility": {"type": "role"}},
            skip={"body"},
        )

    def test_silent_drop_visibility(self):
        with pytest.raises(ValueError) as exc:
            _verify_top_level(
                {"visibility": {"type": "role"}},
                {"id": "1"},
            )
        assert "visibility" in str(exc.value)

    def test_empty_sent_is_noop(self):
        _verify_top_level({}, None)

    def test_none_received_raises(self):
        with pytest.raises(ValueError):
            _verify_top_level({"body": "x"}, None)
