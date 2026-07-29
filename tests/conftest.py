"""Unit-test fixtures for jira-mcp."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

import httpx
import pytest
from pydantic import Field

from jira_mcp import server
from jira_mcp.registry import _UNSET, Group, _op
from jira_mcp.server import _build_params_model, _to_pascal, mcp


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: end-to-end test requiring a live Jira tenant "
        "(env JIRA_URL/JIRA_EMAIL/JIRA_TOKEN/JIRA_TEST_PROJECT)",
    )


# ── Synthetic test ops ──────────────────────────────────────────────────────
#
# These exist purely to give server/help/schema something to chew on without
# importing real tools.py. They aren't registered with MCPServer — tests reach
# them directly through helper fixtures.


_TEST_READ = Group("test_read", "Synthetic read group (test-only).")
_TEST_WRITE = Group("test_write", "Synthetic write group (test-only).")
_TEST_EXECUTE = Group("test_execute", "Synthetic execute group (test-only).")


@_op(_TEST_READ)
def list_things(owner: str, repo: str):
    """List things in a repository."""
    return {"owner": owner, "repo": repo}


@_op(_TEST_READ)
def get_thing(thing_id: int):
    """Get a single thing by id."""
    return {"id": thing_id}


@_op(_TEST_WRITE)
def create_thing(
    owner: str,
    name: str,
    body: Annotated[
        str | None,
        Field(description="Free-text body. Must contain <brief>summary</brief>."),
    ] = None,
    priority: Literal["low", "medium", "high"] = "medium",
    archived: bool = False,
):
    """Create a thing in the given owner's scope.

    Body must contain a <brief>summary</brief> tag for downstream slimming.
    """
    return {"owner": owner, "name": name, "body": body, "priority": priority,
            "archived": archived}


@_op(_TEST_WRITE)
def update_thing(
    thing_id: int,
    name: str = _UNSET,
    body: str | None = _UNSET,
    archived: bool = _UNSET,
):
    """Update an existing thing — every field is optional."""
    return {"id": thing_id, "name": name, "body": body, "archived": archived}


@_op(_TEST_EXECUTE)
def transition_thing(thing_id: int, transition_id: str):
    """Trigger a state transition on a thing."""
    return {"id": thing_id, "transition_id": transition_id}


SYNTHETIC_OPS: dict[str, dict[str, Any]] = {
    "test_read": {
        "ListThings": list_things,
        "GetThing": get_thing,
    },
    "test_write": {
        "CreateThing": create_thing,
        "UpdateThing": update_thing,
    },
    "test_execute": {
        "TransitionThing": transition_thing,
    },
}


@pytest.fixture
def synthetic_ops():
    """Install the synthetic ops into server._group_ops/_all_grouped for the
    duration of a test, then restore whatever was there (real registration).
    """
    saved_ops = dict(server._group_ops)
    saved_all = dict(server._all_grouped)
    server._group_ops.clear()
    server._all_grouped.clear()
    for group_name, ops in SYNTHETIC_OPS.items():
        server._group_ops[group_name] = {}
        for pascal, fn in ops.items():
            fn._params_model = _build_params_model(fn)
            server._group_ops[group_name][pascal] = fn
            server._all_grouped[pascal] = group_name
    yield SYNTHETIC_OPS
    server._group_ops.clear()
    server._all_grouped.clear()
    server._group_ops.update(saved_ops)
    server._all_grouped.update(saved_all)


# ── Agent simulator ─────────────────────────────────────────────────────────


class AgentSimulator:
    """Calls registered MCP tools by name with a params dict, parses JSON
    results when present. Mirrors the LLM-side call shape.
    """

    def __init__(self):
        self._tools: dict[str, Any] = {}
        for tool in mcp._tool_manager._tools.values():
            self._tools[tool.name] = tool.fn
        self.call_log: list[dict] = []

    def call(self, tool_name: str, **kwargs):
        pascal = _to_pascal(tool_name)
        if pascal in server._all_grouped:
            group = server._all_grouped[pascal]
            fn = self._tools[group]
            result = fn(operation=pascal, params=kwargs)
        else:
            fn = self._tools.get(tool_name)
            if fn is None:
                raise ValueError(
                    f"Unknown tool: {tool_name}. "
                    f"Available: {sorted(self._tools.keys())}"
                )
            result = fn(**kwargs)
        self.call_log.append({"tool": tool_name, "kwargs": kwargs, "result": result})
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (json.JSONDecodeError, ValueError):
                return result
        return result


@pytest.fixture
def agent() -> AgentSimulator:
    return AgentSimulator()


# ── HTTP MockTransport ──────────────────────────────────────────────────────


class MockJira:
    """Record requests and return scripted responses."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handlers: list[Callable[[httpx.Request], httpx.Response]] = []

    def handler(self, fn: Callable[[httpx.Request], httpx.Response]) -> MockJira:
        self.handlers.append(fn)
        return self

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for h in self.handlers:
            r = h(request)
            if r is not None:
                return r
        return httpx.Response(500, json={"err": "no handler matched"})


@pytest.fixture
def mock_jira(monkeypatch):
    """Replace the singleton JiraClient with one wired to a MockTransport."""
    from jira_mcp import client as client_module
    from jira_mcp import tools as tools_module

    mock = MockJira()
    transport = httpx.MockTransport(mock.respond)

    real_init = client_module.JiraClient.__init__

    def _mocked_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self._http = httpx.Client(
            base_url=self._http.base_url,
            headers=self._http.headers,
            transport=transport,
        )

    monkeypatch.setattr(client_module.JiraClient, "__init__", _mocked_init)
    monkeypatch.setattr(tools_module, "_client", None)
    # Force env so client construction doesn't fail.
    monkeypatch.setenv("JIRA_URL", "https://mock.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "mock-token")
    from jira_mcp.config import _reset_settings
    _reset_settings()
    yield mock
    monkeypatch.setattr(tools_module, "_client", None)
    _reset_settings()
