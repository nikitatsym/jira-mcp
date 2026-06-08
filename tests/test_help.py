"""Unit tests for `_build_help` — render, search filter, cross-group hint.

Exercises the help renderer against the synthetic ops in conftest, not the
real jira-mcp ops (whose annotations land in PR2). The four shape forms
(`T`, `T?`, `T | None`, `T? | None`) are covered via dedicated helper fns.
"""

from __future__ import annotations

from typing import Annotated, Optional

import pytest
from pydantic import Field

from jira_mcp import server
from jira_mcp.server import _build_help, _build_params_model


@pytest.fixture(autouse=True)
def _isolate_state():
    """Snapshot and restore module-level help state so tests don't leak."""
    saved_ops = dict(server._group_ops)
    saved_all = dict(server._all_grouped)
    server._group_ops.clear()
    server._all_grouped.clear()
    yield
    server._group_ops.clear()
    server._all_grouped.clear()
    server._group_ops.update(saved_ops)
    server._all_grouped.update(saved_all)


def _seed(group_to_fns: dict[str, dict]) -> None:
    """Install ops into the help registry, building Pydantic models on the fly."""
    for group_name, fns in group_to_fns.items():
        server._group_ops[group_name] = {}
        for pascal, fn in fns.items():
            fn._params_model = _build_params_model(fn)
            server._group_ops[group_name][pascal] = fn
            server._all_grouped[pascal] = group_name


# ── Shape-rendering fixtures (the four spec forms) ──────────────────────────


def required_only(owner: str):
    """T — required."""


def optional_default(owner: str, page: int = 1):
    """T? — optional with non-None default."""


def nullable_required(assignee: str | None):
    """T | None — nullable required."""


def optional_nullable(assignee: str | None = None):
    """T? | None — both."""


def with_description(
    owner: str,
    body: Annotated[
        Optional[str],
        Field(description="Body markdown. Must contain <brief>summary</brief>."),
    ] = None,
):
    """With per-param Field(description=...)."""


def search_alpha(owner: str):
    """List alpha entities."""


def search_bravo(owner: str):
    """List bravo entities."""


# ── Test classes ────────────────────────────────────────────────────────────


class TestHelpFlat:
    def test_full_listing_no_args(self):
        _seed({"g_read": {"RequiredOnly": required_only}})
        out = _build_help("g_read")
        assert "1 operations available" in out
        assert "RequiredOnly(owner: str) — T — required." in out

    def test_help_shows_docstring_body(self):
        def fn(owner: str):
            """Head line.

            Body line one.
            Body line two.
            """

        _seed({"g_read": {"Fn": fn}})
        out = _build_help("g_read")
        assert "Fn(owner: str) — Head line." in out
        assert "    Body line one." in out
        assert "    Body line two." in out

    def test_help_shows_per_param_description(self):
        _seed({"g_write": {"WithDescription": with_description}})
        out = _build_help("g_write")
        assert "    body: Body markdown." in out


class TestHelpShapeRenderings:
    """The four forms from the v2 spec, exercised individually."""

    def test_required_t(self):
        _seed({"g": {"RequiredOnly": required_only}})
        out = _build_help("g")
        # `T` — no `?`, no default.
        assert "owner: str" in out
        assert "owner?:" not in out

    def test_optional_t_with_default(self):
        _seed({"g": {"OptionalDefault": optional_default}})
        out = _build_help("g")
        # `T?` — with `?` and non-None default rendered.
        assert "page?: int=1" in out

    def test_nullable_required(self):
        _seed({"g": {"NullableRequired": nullable_required}})
        out = _build_help("g")
        # `T | None` (required) — no `?`, Optional unwrapped to bare type
        # in renderer. Required-ness preserved.
        assert "assignee: str" in out
        assert "assignee?:" not in out

    def test_optional_nullable(self):
        _seed({"g": {"OptionalNullable": optional_nullable}})
        out = _build_help("g")
        # `T? | None` — `?` plus None default rendered cleanly (no `=None`,
        # per the v2.5 convention).
        assert "assignee?: str" in out
        # Don't leak `=None`.
        assert "=None" not in out


class TestHelpSearch:
    def test_search_filters_in_local_group(self):
        _seed({
            "g_read": {
                "SearchAlpha": search_alpha,
                "SearchBravo": search_bravo,
            }
        })
        out = _build_help("g_read", search="alpha")
        assert "SearchAlpha" in out
        assert "SearchBravo" not in out
        assert "1 of 2 operations" in out

    def test_search_case_insensitive(self):
        _seed({"g_read": {"SearchAlpha": search_alpha}})
        out = _build_help("g_read", search="ALPHA")
        assert "SearchAlpha" in out

    def test_search_matches_docstring(self):
        _seed({"g_read": {"SearchAlpha": search_alpha}})
        # 'entities' is in the docstring, not the name.
        out = _build_help("g_read", search="entit")
        assert "SearchAlpha" in out

    def test_search_no_match(self):
        _seed({"g_read": {"SearchAlpha": search_alpha}})
        out = _build_help("g_read", search="qwerty")
        assert "No ops in g_read matching 'qwerty'" in out

    def test_search_cross_group_hint(self):
        _seed({
            "g_read": {"SearchAlpha": search_alpha},
            "g_write": {"SearchBravo": search_bravo},
        })
        out = _build_help("g_read", search="bravo")
        assert "No ops in g_read matching 'bravo'" in out
        assert "Found in other groups" in out
        assert "g_write" in out
        assert "SearchBravo" in out


class TestHelpUnsetRendering:
    """`Field(default_factory=lambda: _UNSET)` must NOT leak
    `=PydanticUndefined` into help output.
    """

    def test_unset_default_renders_as_optional_no_default(self):
        from jira_mcp.registry import _UNSET

        def fn_with_unset(owner: str, assignee: str = _UNSET):
            """Op with a sentinel-defaulted optional param."""

        _seed({"g_write": {"FnWithUnset": fn_with_unset}})
        out = _build_help("g_write")
        assert "assignee?: str" in out
        assert "PydanticUndefined" not in out
        assert "owner: str" in out
