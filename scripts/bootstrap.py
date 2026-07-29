#!/usr/bin/env python3
"""Verify Jira sandbox access for integration tests.

Reads tests/.env (or falls back to the process env), confirms auth, checks
the configured test project exists and allows create+delete of a draft
issue. Does NOT create the project — Jira Cloud project creation needs
admin rights and a tenant-specific templateKey, see README.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = REPO / "tests" / ".env"


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    _load_env()
    required = ["JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_TEST_PROJECT"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
        print(f"  edit {ENV_FILE} or export them in your shell", file=sys.stderr)
        return 1

    sys.path.insert(0, str(REPO / "src"))
    from jira_mcp.client import APIError, JiraClient

    project_key = os.environ["JIRA_TEST_PROJECT"]
    c = JiraClient()

    try:
        me = c.get("/rest/api/3/myself")
    except APIError as e:
        print(f"ERROR: auth check failed: {e}", file=sys.stderr)
        return 2
    print(f"auth ok: {me.get('emailAddress')} / {me.get('displayName')}")

    try:
        p = c.get(f"/rest/api/3/project/{project_key}")
    except APIError as e:
        print(f"ERROR: project '{project_key}' not accessible: {e}", file=sys.stderr)
        print(
            "  Create it manually in the Jira UI (any Kanban template), "
            "then re-run.",
            file=sys.stderr,
        )
        return 3
    issue_types = [t["name"] for t in p.get("issueTypes", [])]
    print(f"project ok: {p['key']} - {p['name']} (types: {issue_types})")

    draft_type = "Task" if "Task" in issue_types else issue_types[0]
    try:
        draft = c.post(
            "/rest/api/3/issue",
            json={
                "fields": {
                    "project": {"key": project_key},
                    "issuetype": {"name": draft_type},
                    "summary": "bootstrap probe (auto-deleted)",
                },
            },
        )
        c.delete(f"/rest/api/3/issue/{draft['key']}")
        print(f"crud ok: created+deleted {draft['key']}")
    except APIError as e:
        print(f"ERROR: cannot create+delete issue: {e}", file=sys.stderr)
        return 4

    print("\nready for integration tests:")
    print("  npm run test:integration   # or: uv run pytest tests/ -m integration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
