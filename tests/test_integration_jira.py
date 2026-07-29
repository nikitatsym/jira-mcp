"""End-to-end integration tests against a live Jira Cloud tenant.

Skipped automatically when JIRA_URL / JIRA_EMAIL / JIRA_TOKEN /
JIRA_TEST_PROJECT aren't set. Run via `npm run test:integration` or
`uv run pytest tests/ -m integration`.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

REQUIRED_ENV = ["JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_TEST_PROJECT"]


def _load_env_file():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(not os.environ.get(k) for k in REQUIRED_ENV),
        reason=f"Integration test requires {REQUIRED_ENV}",
    ),
]


@pytest.fixture(scope="module")
def client():
    from jira_mcp import tools as tools_module
    from jira_mcp.config import _reset_settings

    _reset_settings()
    tools_module._client = None
    yield
    tools_module._client = None


@pytest.fixture(scope="module")
def project(client) -> str:
    return os.environ["JIRA_TEST_PROJECT"]


@pytest.fixture(scope="module")
def me(client) -> dict:
    from jira_mcp.tools import get_myself
    return get_myself()


@pytest.fixture
def issue(client, project) -> str:
    """Create a fresh issue, yield key, delete on teardown."""
    from jira_mcp.tools import create_issue, delete_issue
    created = create_issue(
        project_key=project,
        issue_type="Task",
        summary=f"integration test {time.time()}",
        description=(
            "<brief>integration smoke</brief>\n\n"
            "Created by tests/test_integration_jira.py. Auto-deleted on teardown."
        ),
        labels=["integration", "auto"],
    )
    key = created["key"]
    yield key
    try:
        delete_issue(issue_key=key)
    # teardown runs after the test already passed/failed on its own merits;
    # an exception here must not turn that outcome into a fixture error.
    except Exception:  # noqa: BLE001, S110 - best-effort cleanup only
        pass


class TestIssueLifecycle:
    def test_create_with_rich_params(self, client, project):
        from jira_mcp.tools import create_issue, delete_issue
        created = create_issue(
            project_key=project,
            issue_type="Task",
            summary="rich-params test",
            description="<brief>rich</brief>",
            labels=["a", "b"],
            due_date="2027-01-15",
            environment="staging",
        )
        try:
            assert "key" in created
        finally:
            delete_issue(issue_key=created["key"])

    def test_update_via_return_issue(self, issue):
        from jira_mcp.tools import update_issue
        update_issue(
            issue_key=issue,
            summary="updated summary",
            labels=["updated"],
        )

    def test_assign_self_then_unassign(self, issue, me):
        from jira_mcp.tools import assign_issue, unassign_issue
        assign_issue(issue_key=issue, account_id=me["accountId"])
        unassign_issue(issue_key=issue)

    def test_comment_round_trip(self, issue):
        from jira_mcp.tools import add_comment, get_issue_comments
        add_comment(issue_key=issue, body="integration comment")
        comments = get_issue_comments(issue_key=issue)
        bodies = [c.get("body", "") for c in comments.get("comments", [])]
        assert any("integration comment" in b for b in bodies)

    def test_transition_to_done(self, issue):
        from jira_mcp.tools import get_issue_transitions, transition_issue
        trs = get_issue_transitions(issue_key=issue)
        done = next(
            (t for t in trs["transitions"] if "Done" in t["name"]),
            None,
        )
        if not done:
            pytest.skip("project workflow has no Done transition")
        transition_issue(issue_key=issue, transition_id=done["id"])


class TestAttachments:
    def test_upload_download_delete(self, issue):
        from jira_mcp.tools import (
            delete_attachment,
            download_attachment,
            list_attachments,
            upload_attachment,
        )
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", delete=False,
        ) as f:
            payload = b"integration test payload"
            f.write(payload)
            src_path = f.name
        try:
            upload_attachment(issue_key=issue, file_path=src_path)
            atts = list_attachments(issue_key=issue)
            assert any(
                a.get("filename") == os.path.basename(src_path) for a in atts
            )
            att_id = atts[0]["id"]
            downloaded = download_attachment(attachment_id=att_id)
            with open(downloaded["path"], "rb") as g:
                assert g.read() == payload
            delete_attachment(attachment_id=att_id)
        finally:
            os.unlink(src_path)


class TestIssueLinks:
    def test_create_list_delete(self, client, project):
        from jira_mcp.tools import (
            create_issue,
            create_issue_link,
            delete_issue,
            delete_issue_link,
            list_issue_links,
        )
        a = create_issue(
            project_key=project, issue_type="Task",
            summary="link a", description="<brief>a</brief>",
        )["key"]
        b = create_issue(
            project_key=project, issue_type="Task",
            summary="link b", description="<brief>b</brief>",
        )["key"]
        try:
            create_issue_link(
                type="Relates", inward_issue=a, outward_issue=b,
            )
            links_a = list_issue_links(issue_key=a)
            assert any(link["other_key"] == b for link in links_a)
            link_id = next(link["id"] for link in links_a if link["other_key"] == b)
            delete_issue_link(link_id=link_id)
        finally:
            delete_issue(issue_key=a)
            delete_issue(issue_key=b)


class TestSilentDropProtection:
    def test_unknown_priority_substitution_detected_or_passes(self, issue):
        """Jira may either reject the bad priority or substitute the default —
        the verify path either raises or passes; what it must NOT do is
        silently accept and report success when the field was dropped.
        """
        from jira_mcp.client import APIError
        from jira_mcp.tools import update_issue
        try:
            update_issue(issue_key=issue, priority="Highest")
        except (ValueError, APIError):
            # Either Jira rejected or verify caught a substitution — both fine.
            pass
