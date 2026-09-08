"""Unit tests for `_render_group_doc`: group docs interpolate operation names
instead of hardcoding them.

The `Example: jira_read(operation="$X", ...)` lines in `tools.py` are hand-written
while the real names come from `_to_pascal(fn.__name__)`, so rendering them from
the registry is what keeps a rename from leaving an agent's first call pointing
at a non-existent op.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from jira_mcp import server, tools
from jira_mcp.registry import Group


def test_group_docs_resolve_operation_placeholders():
    groups = [
        obj
        for _, obj in inspect.getmembers(tools, lambda o: isinstance(o, Group))
        if obj.name in server._group_ops
    ]
    assert len(groups) == len(server._group_ops)
    for group in groups:
        rendered = server._render_group_doc(
            group.name, group.doc, server._group_ops[group.name]
        )
        assert "$" not in rendered, f"{group.name} doc left a placeholder unrendered"


def test_render_group_doc_rejects_unknown_placeholder():
    with pytest.raises(RuntimeError, match="NoSuchOp"):
        server._render_group_doc(
            "jira_read",
            'Example: jira_read(operation="$NoSuchOp")',
            {"SearchIssues": None},
        )


def test_render_group_doc_rejects_hardcoded_operation():
    with pytest.raises(RuntimeError, match="hardcodes"):
        server._render_group_doc(
            "jira_read",
            'Example: jira_read(operation="SearchIssues")',
            {"SearchIssues": None},
        )

    with pytest.raises(RuntimeError, match="hardcodes"):
        server._render_group_doc(
            "jira_read",
            'Example: jira_read(operation = "SearchIssues")',
            {"SearchIssues": None},
        )


def test_render_group_doc_resolves_meta_and_keeps_generic_form():
    rendered = server._render_group_doc(
        "jira_read", 'operation="$help" or operation="$schema" or operation="<OpName>"', {}
    )
    assert rendered == 'operation="help" or operation="schema" or operation="<OpName>"'


def test_registered_tools_emit_compact_json():
    from mcp.types import TextContent

    assert all(
        tool.fn_metadata.output_schema is None
        for tool in server.mcp._tool_manager.list_tools()
    )

    expected = server._dispatch("schema", "jira_read", {})
    result = asyncio.run(server.mcp.call_tool("jira_read", {"operation": "schema"}))

    assert result.structured_content is None
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert "\n" not in content.text
    assert json.loads(content.text) == expected
