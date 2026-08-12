"""Unit tests for `_validate_doc_examples`: group docs must not advertise
operation names the group does not expose.

The `Example: jira_read(operation="X", ...)` lines in `tools.py` are hand-written
while the real names come from `_to_pascal(fn.__name__)`, so a rename silently
leaves an agent's first call pointing at a non-existent op.
"""

from __future__ import annotations

import inspect

import pytest

from jira_mcp import server, tools
from jira_mcp.registry import Group


def test_group_doc_examples_name_registered_operations():
    groups = [
        obj
        for _, obj in inspect.getmembers(tools, lambda o: isinstance(o, Group))
        if obj.name in server._group_ops
    ]
    assert len(groups) == len(server._group_ops)
    for group in groups:
        for name in server._EXAMPLE_OPERATION.findall(group.doc):
            if name == "help":
                continue
            assert name in server._group_ops[group.name], (
                f"{group.name} example names {name!r}, which it does not expose"
            )


def test_doc_example_validation_rejects_unknown_operation():
    with pytest.raises(RuntimeError, match="NoSuchOp"):
        server._validate_doc_examples(
            "jira_read",
            'Example: jira_read(operation="NoSuchOp")',
            {"SearchIssues": None},
        )

    server._validate_doc_examples("jira_read", 'operation="help"', {})
