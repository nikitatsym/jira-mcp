"""Jira tool operations. All public functions are auto-registered as MCP tools."""

from __future__ import annotations

import os
import tempfile
from typing import Annotated, Any

from pydantic import Field

from .client import JiraClient
from .prepare import (
    _adf_to_text,
    _body,
    _fields,
    _ok,
    _prepare_issue_fields,
    _text_to_adf,
    _validate_brief,
    _verify_response,
    _verify_top_level,
)
from .registry import _UNSET, ROOT, Group, _op

# ── Client singleton ──────────────────────────────────────────────────

_client: JiraClient | None = None


def _get_client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient()
    return _client


# ── Slim helpers ──────────────────────────────────────────────────────

_SLIM_ISSUE_FIELDS = {
    "key", "summary", "status", "priority", "assignee", "issuetype", "updated", "labels",
}

_SLIM_COMMENT_FIELDS = {"id", "author", "created", "updated", "body"}

_SLIM_PROJECT_FIELDS = {"key", "name", "projectTypeKey", "style"}

_SLIM_BOARD_FIELDS = {"id", "name", "type"}

_SLIM_ATTACHMENT_FIELDS = {"id", "filename", "mimeType", "size", "created"}

_SLIM_USER_FIELDS = {"accountId", "displayName", "emailAddress", "active"}


def _slim(item: dict, fields: set) -> dict:
    return {k: v for k, v in item.items() if k in fields}


def _slim_list(items: list, fields: set) -> list:
    return [_slim(i, fields) for i in items if isinstance(i, dict)]


def _slim_issue(issue: dict) -> dict:
    """Extract slim fields from a Jira issue (fields are nested)."""
    fields = issue.get("fields", {})
    result: dict = {"key": issue.get("key")}
    for f in ("summary", "updated", "labels"):
        if f in fields:
            result[f] = fields[f]
    for f in ("status", "priority", "issuetype"):
        val = fields.get(f)
        if val is None:
            result[f] = None
        elif isinstance(val, dict):
            result[f] = val.get("name")
    assignee = fields.get("assignee")
    if assignee is None:
        result["assignee"] = None
    elif isinstance(assignee, dict):
        result["assignee"] = assignee.get("displayName")
    return result


def _slim_comment(comment: dict) -> dict:
    """Slim a comment: id, author name, dates, body as plain text (first 200 chars)."""
    result: dict = {"id": comment.get("id")}
    author = comment.get("author")
    result["author"] = author.get("displayName") if isinstance(author, dict) else None
    for f in ("created", "updated"):
        result[f] = comment.get(f)
    body = comment.get("body")
    if body:
        result["body"] = _adf_to_text(body)[:200]
    else:
        result["body"] = None
    return result


def _clean_issue(issue: dict) -> None:
    """Remove noise from a full issue response (in-place)."""
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return
    to_drop = [k for k, v in fields.items() if k.startswith("customfield_") and v is None]
    for k in to_drop:
        del fields[k]
    fields.pop("worklog", None)
    comment_data = fields.get("comment")
    if isinstance(comment_data, dict) and "comments" in comment_data:
        comment_data["comments"] = [_slim_comment(c) for c in comment_data["comments"]]
    _strip_avatars(fields)


def _strip_avatars(obj):
    if isinstance(obj, dict):
        obj.pop("avatarUrls", None)
        for v in obj.values():
            _strip_avatars(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_avatars(item)


def _fields_csv(payload: dict) -> str:
    """CSV of wire-keys from a prepared field payload — for ?fields= queries."""
    return ",".join(sorted(payload.keys()))


# ── Groups ────────────────────────────────────────────────────────────

jira_read = Group(
    "jira_read",
    "Query Jira data (safe, read-only).\n\n"
    "Call with operation=\"$help\" to list all available read operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_read(operation=\"$SearchIssues\", "
    "params={\"jql\": \"project = PROJ AND status = Open\"})",
)

jira_write = Group(
    "jira_write",
    "Create or update Jira resources (non-destructive).\n\n"
    "Call with operation=\"$help\" to list all available write operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_write(operation=\"$CreateIssue\", "
    "params={\"project_key\": \"PROJ\", \"issue_type\": \"Task\", \"summary\": \"Fix login bug\"})",
)

jira_execute = Group(
    "jira_execute",
    "Trigger Jira state changes (transition, assign, unassign).\n\n"
    "Distinct from write because these are side-effects on existing issues "
    "rather than resource creation.\n\n"
    "Call with operation=\"$help\" to list all available execute operations.\n\n"
    "Example: jira_execute(operation=\"$TransitionIssue\", "
    "params={\"issue_key\": \"PROJ-1\", \"transition_id\": \"31\"})",
)

jira_delete = Group(
    "jira_delete",
    "Delete Jira resources (destructive, irreversible).\n\n"
    "Call with operation=\"$help\" to list all available delete operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_delete(operation=\"$DeleteIssue\", "
    "params={\"issue_key\": \"PROJ-123\"})",
)


# ── Standalone ────────────────────────────────────────────────────────


@_op(ROOT)
def jira_version():
    """Get the Jira MCP server version and service status."""
    from importlib.metadata import version

    try:
        info = _get_client().get("/rest/api/3/myself")
        service = {"status": "ok", "user": info.get("displayName")}
    except Exception:  # noqa: BLE001 - version check must not crash the whole tool
        service = {"status": "error"}
    return {"mcp": version("jira-mcp"), "service": service}


# ── Read operations ──────────────────────────────────────────────────


@_op(jira_read)
def search_issues(
    jql: str,
    limit: int = 20,
    next_page_token: str | None = None,
    fields: Annotated[
        str,
        Field(description="Comma-separated wire field names to fetch. Override the default slim set. When set, the response is returned as-is without the slim-issue transform."),
    ] = _UNSET,
):
    """Search issues using JQL. Paginate with next_page_token from response nextPageToken."""
    explicit_fields = fields is not _UNSET
    fields_str = fields if explicit_fields else ",".join(_SLIM_ISSUE_FIELDS)
    params: dict = {"jql": jql, "maxResults": limit, "fields": fields_str}
    if next_page_token is not None:
        params["nextPageToken"] = next_page_token
    data = _get_client().get(
        "/rest/api/3/search/jql",
        params=params,
    )
    if not explicit_fields and isinstance(data, dict) and "issues" in data:
        data["issues"] = [_slim_issue(i) for i in data["issues"]]
    return data


@_op(jira_read)
def get_issue(
    issue_key: str,
    fields: Annotated[
        str,
        Field(description="Comma-separated wire field names. Override the default full fetch. When set, the slim/clean transform is skipped so the caller receives exactly what they asked for."),
    ] = _UNSET,
    expand: Annotated[
        str,
        Field(description="Comma-separated expand directives (e.g. 'names,renderedFields,changelog'). Pass-through to Jira."),
    ] = _UNSET,
):
    """Get full detail of a specific issue."""
    explicit_fields = fields is not _UNSET
    params: dict = {}
    if explicit_fields:
        params["fields"] = fields
    if expand is not _UNSET:
        params["expand"] = expand
    data = _get_client().get(f"/rest/api/3/issue/{issue_key}", params=params)
    if not explicit_fields and isinstance(data, dict):
        _clean_issue(data)
    return data


@_op(jira_read)
def get_issue_comments(issue_key: str, limit: int = 20):
    """Get comments on an issue."""
    data = _get_client().get(
        f"/rest/api/3/issue/{issue_key}/comment",
        params={"maxResults": limit},
    )
    if isinstance(data, dict) and "comments" in data:
        data["comments"] = [_slim_comment(c) for c in data["comments"]]
    return data


@_op(jira_read)
def get_issue_transitions(issue_key: str):
    """Get available status transitions for an issue."""
    return _get_client().get(f"/rest/api/3/issue/{issue_key}/transitions")


@_op(jira_read)
def get_issue_watchers(issue_key: str):
    """Get watchers of an issue."""
    data = _get_client().get(f"/rest/api/3/issue/{issue_key}/watchers")
    if isinstance(data, dict) and "watchers" in data:
        data["watchers"] = _slim_list(data["watchers"], _SLIM_USER_FIELDS)
    return data


@_op(jira_read)
def get_issue_changelog(issue_key: str, limit: int = 20):
    """Get history of field changes on an issue."""
    data = _get_client().get(
        f"/rest/api/3/issue/{issue_key}/changelog",
        params={"maxResults": limit},
    )
    if isinstance(data, dict):
        _strip_avatars(data)
    return data


@_op(jira_read)
def list_issue_links(issue_key: str):
    """List issue links. Slim shape {id, type, direction, other_key} for DeleteIssueLink discovery."""
    data = _get_client().get(
        f"/rest/api/3/issue/{issue_key}",
        params={"fields": "issuelinks"},
    )
    links = data.get("fields", {}).get("issuelinks") if isinstance(data, dict) else None
    if not isinstance(links, list):
        return []
    out: list[dict] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type", {}).get("name") if isinstance(link.get("type"), dict) else None
        inward = link.get("inwardIssue")
        outward = link.get("outwardIssue")
        if isinstance(outward, dict):
            out.append({
                "id": link.get("id"),
                "type": link_type,
                "direction": "outward",
                "other_key": outward.get("key"),
            })
        if isinstance(inward, dict):
            out.append({
                "id": link.get("id"),
                "type": link_type,
                "direction": "inward",
                "other_key": inward.get("key"),
            })
    return out


@_op(jira_read)
def list_projects(limit: int = 20):
    """List all accessible projects."""
    data = _get_client().get(
        "/rest/api/3/project/search",
        params={"maxResults": limit},
    )
    if isinstance(data, dict) and "values" in data:
        data["values"] = _slim_list(data["values"], _SLIM_PROJECT_FIELDS)
    return data


@_op(jira_read)
def get_project(project_key: str):
    """Get full detail of a project."""
    return _get_client().get(f"/rest/api/3/project/{project_key}")


@_op(jira_read)
def list_statuses():
    """List all issue statuses."""
    data = _get_client().get("/rest/api/3/status")
    if isinstance(data, list):
        return [{"id": s.get("id"), "name": s.get("name"),
                 "category": s.get("statusCategory", {}).get("name")}
                for s in data if isinstance(s, dict)]
    return data


@_op(jira_read)
def list_priorities():
    """List all issue priorities."""
    return _get_client().get("/rest/api/3/priority")


@_op(jira_read)
def list_fields():
    """List all fields including custom fields."""
    data = _get_client().get("/rest/api/3/field")
    if isinstance(data, list):
        return [{"id": f.get("id"), "name": f.get("name"), "custom": f.get("custom")}
                for f in data if isinstance(f, dict)]
    return data


@_op(jira_read)
def list_issue_types(project_key: str):
    """List issue types available in a project."""
    return _get_client().get(
        f"/rest/api/3/issue/createmeta/{project_key}/issuetypes"
    )


@_op(jira_read)
def list_labels(limit: int = 20):
    """List all labels."""
    return _get_client().get(
        "/rest/api/3/label",
        params={"maxResults": limit},
    )


@_op(jira_read)
def search_users(query: str, limit: int = 20):
    """Search for users by name or email."""
    data = _get_client().get(
        "/rest/api/3/user/search",
        params={"query": query, "maxResults": limit},
    )
    if isinstance(data, list):
        return _slim_list(data, _SLIM_USER_FIELDS)
    return data


@_op(jira_read)
def get_myself():
    """Get the current authenticated user."""
    data = _get_client().get("/rest/api/3/myself")
    if isinstance(data, dict):
        return _slim(data, _SLIM_USER_FIELDS)
    return data


@_op(jira_read)
def list_components(project_key: str):
    """List components in a project."""
    return _get_client().get(f"/rest/api/3/project/{project_key}/components")


@_op(jira_read)
def list_versions(project_key: str):
    """List versions/releases in a project."""
    return _get_client().get(f"/rest/api/3/project/{project_key}/versions")


@_op(jira_read)
def list_boards(project_key: str | None = None, limit: int = 20):
    """List agile boards, optionally filtered by project."""
    params: dict = {"maxResults": limit}
    if project_key is not None:
        params["projectKeyOrId"] = project_key
    data = _get_client().get("/rest/agile/1.0/board", params=params)
    if isinstance(data, dict) and "values" in data:
        data["values"] = _slim_list(data["values"], _SLIM_BOARD_FIELDS)
    return data


@_op(jira_read)
def list_sprints(board_id: int, state: str | None = None, limit: int = 20):
    """List sprints for a board. State: active, closed, future."""
    params: dict = {"maxResults": limit}
    if state is not None:
        params["state"] = state
    return _get_client().get(
        f"/rest/agile/1.0/board/{board_id}/sprint",
        params=params,
    )


@_op(jira_read)
def get_sprint_issues(sprint_id: int, limit: int = 20):
    """Get issues in a sprint."""
    data = _get_client().get(
        f"/rest/agile/1.0/sprint/{sprint_id}/issue",
        params={"maxResults": limit},
    )
    if isinstance(data, dict) and "issues" in data:
        data["issues"] = [_slim_issue(i) for i in data["issues"]]
    return data


@_op(jira_read)
def get_board_backlog(board_id: int, limit: int = 20):
    """Get backlog issues for a board."""
    data = _get_client().get(
        f"/rest/agile/1.0/board/{board_id}/backlog",
        params={"maxResults": limit},
    )
    if isinstance(data, dict) and "issues" in data:
        data["issues"] = [_slim_issue(i) for i in data["issues"]]
    return data


@_op(jira_read)
def list_attachments(issue_key: str):
    """List attachments on an issue."""
    data = _get_client().get(
        f"/rest/api/3/issue/{issue_key}",
        params={"fields": "attachment"},
    )
    attachments = data.get("fields", {}).get("attachment", [])
    return _slim_list(attachments, _SLIM_ATTACHMENT_FIELDS)


@_op(jira_read)
def get_attachment(attachment_id: str):
    """Get full metadata of an attachment."""
    return _get_client().get(f"/rest/api/3/attachment/{attachment_id}")


@_op(jira_read)
def download_attachment(attachment_id: str, path: str | None = None):
    """Download an attachment. Omit path to save to a fresh per-call temp dir. Returns saved filepath and size."""
    meta = _get_client().get(f"/rest/api/3/attachment/{attachment_id}")
    if path is None:
        filename = os.path.basename(
            meta.get("filename", "") or ""
        ) or f"attachment_{attachment_id}"
        filename = filename[:255]
        path = os.path.join(tempfile.mkdtemp(prefix="jira_mcp_"), filename)
    r = _get_client().get_raw(f"/rest/api/3/attachment/content/{attachment_id}")
    with open(path, "wb") as f:
        f.write(r.content)
    return {"path": path, "size": len(r.content), "filename": os.path.basename(path)}


# ── Write operations ─────────────────────────────────────────────────


@_op(jira_write)
def create_issue(
    project_key: str,
    issue_type: str,
    summary: str,
    description: Annotated[
        str | None,
        Field(description="Plain text (converted to ADF). Must contain <brief>summary</brief> when MCP_JIRA_BRIEF_MAX>0 (default 100). Set the env var to 0 to disable. Sent as fields.description on the wire."),
    ] = _UNSET,
    priority: Annotated[
        str | None,
        Field(description="Priority name e.g. 'High'. Pass null to clear (where the screen allows). Wrapped as {'name': ...}."),
    ] = _UNSET,
    assignee: Annotated[
        str | None,
        Field(description="accountId of the assignee, NOT email or username. Pass null to leave unassigned. Wrapped as {'accountId': ...}."),
    ] = _UNSET,
    reporter: Annotated[
        str | None,
        Field(description="accountId of the reporter. Pass null to reset to project default (may be substituted server-side). Wrapped as {'accountId': ...}."),
    ] = _UNSET,
    parent_key: Annotated[
        str,
        Field(description="Parent issue key (e.g. 'STS-238'). For Epic Link on company-managed projects use custom_fields={'customfield_<id>': key} instead. Sent as fields.parent.key."),
    ] = _UNSET,
    due_date: Annotated[
        str | None,
        Field(description="Due date YYYY-MM-DD. Pass null to clear. Sent as fields.duedate on the wire (Python param uses snake_case)."),
    ] = _UNSET,
    environment: Annotated[
        str | None,
        Field(description="Free-text environment field (converted to ADF). Pass null to clear."),
    ] = _UNSET,
    labels: Annotated[
        list[str],
        Field(description="Label strings. Pass [] to clear (cannot be null)."),
    ] = _UNSET,
    components: Annotated[
        list[str],
        Field(description="Component names. Pass [] to clear. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    fix_versions: Annotated[
        list[str],
        Field(description="Fix-version names. Pass [] to clear. Sent as fields.fixVersions on the wire. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    affects_versions: Annotated[
        list[str],
        Field(description="Affects-version names. Sent as fields.versions on the wire. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    custom_fields: Annotated[
        dict,
        Field(description="Map of customfield_NNNNN → value, merged flat into fields. Collisions with named params or wire-shape names (parent/assignee/...) are rejected fail-fast — use the named param instead."),
    ] = _UNSET,
):
    """Create a new issue. Description/environment are plain text (converted to ADF)."""
    if description is not _UNSET and isinstance(description, str):
        _validate_brief(description)

    local_vars = dict(locals())
    sent = _body(
        local_vars,
        exclude=("project_key", "issue_type"),
        rename={
            "parent_key": "parent",
            "due_date": "duedate",
            "fix_versions": "fixVersions",
            "affects_versions": "versions",
        },
        keep_null=(
            "assignee", "reporter", "priority", "duedate", "environment",
        ),
    )
    fields = _prepare_issue_fields(sent)
    fields["project"] = {"key": project_key}
    fields["issuetype"] = {"name": issue_type}

    created = _get_client().post("/rest/api/3/issue", json=_fields(fields))
    issue_key = created.get("key") if isinstance(created, dict) else None
    if issue_key is None:
        raise ValueError(
            f"Unexpected create response: {created!r}. Expected an object "
            "with 'key'. The issue may or may not have been created."
        )

    verify_fields = {k: v for k, v in fields.items() if k not in ("project", "issuetype")}
    if verify_fields:
        received = _get_client().get(
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": _fields_csv(verify_fields)},
        )
        _verify_response(verify_fields, received)
    return created


@_op(jira_write)
def update_issue(
    issue_key: str,
    summary: Annotated[
        str,
        Field(description="New summary. Cannot be cleared — omit to leave unchanged."),
    ] = _UNSET,
    description: Annotated[
        str | None,
        Field(description="Plain text (converted to ADF). Must contain <brief>summary</brief> when MCP_JIRA_BRIEF_MAX>0. Pass null to clear. Sent as fields.description."),
    ] = _UNSET,
    priority: Annotated[
        str | None,
        Field(description="Priority name. Pass null to clear (where screen allows). Wrapped as {'name': ...}."),
    ] = _UNSET,
    assignee: Annotated[
        str | None,
        Field(description="accountId. Pass null to unassign. NOTE: this also exists as a dedicated jira_execute.AssignIssue op; either works."),
    ] = _UNSET,
    reporter: Annotated[
        str | None,
        Field(description="accountId. Pass null to reset to project default."),
    ] = _UNSET,
    due_date: Annotated[
        str | None,
        Field(description="YYYY-MM-DD. Pass null to clear. Sent as fields.duedate."),
    ] = _UNSET,
    environment: Annotated[
        str | None,
        Field(description="Plain text (converted to ADF). Pass null to clear."),
    ] = _UNSET,
    labels: Annotated[
        list[str],
        Field(description="Replace label list. Pass [] to clear."),
    ] = _UNSET,
    components: Annotated[
        list[str],
        Field(description="Component names. Pass [] to clear. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    fix_versions: Annotated[
        list[str],
        Field(description="Fix-version names. Sent as fields.fixVersions. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    affects_versions: Annotated[
        list[str],
        Field(description="Affects-version names. Sent as fields.versions. Wrapped as [{'name': ...}, ...]."),
    ] = _UNSET,
    custom_fields: Annotated[
        dict,
        Field(description="customfield_NNNNN map, flat-merged. Rejected if it contains 'resolution' — use transition_issue instead."),
    ] = _UNSET,
):
    """Update an issue. Only provided fields change. For resolution use TransitionIssue."""
    if isinstance(custom_fields, dict) and "resolution" in custom_fields:
        raise ValueError(
            "Setting 'resolution' via update_issue is rejected — most Jira "
            "workflows don't expose resolution on the edit-issue screen and "
            "the field is silently dropped. Use jira_execute.TransitionIssue "
            "(issue_key, transition_id, resolution=...) instead."
        )
    if description is not _UNSET and isinstance(description, str):
        _validate_brief(description)

    local_vars = dict(locals())
    sent = _body(
        local_vars,
        exclude=("issue_key",),
        rename={
            "due_date": "duedate",
            "fix_versions": "fixVersions",
            "affects_versions": "versions",
        },
        keep_null=(
            "assignee", "reporter", "priority", "duedate",
            "description", "environment",
        ),
    )
    fields = _prepare_issue_fields(sent)
    if not fields:
        return {"status": "ok", "note": "no fields to update"}

    received = _get_client().put(
        f"/rest/api/3/issue/{issue_key}",
        params={"returnIssue": "true", "expand": "names"},
        json=_fields(fields),
    )
    _verify_response(fields, received)
    return _ok(received)


@_op(jira_write)
def add_comment(
    issue_key: str,
    body: str,
    visibility: Annotated[
        dict,
        Field(description="Restrict the comment to a role or group: {'type': 'role'|'group', 'value': '<name>'}. Omit for unrestricted."),
    ] = _UNSET,
):
    """Add a comment. Body is plain text (converted to ADF)."""
    payload: dict[str, Any] = {"body": _text_to_adf(body)}
    sent_for_verify: dict = {"body": body}
    if visibility is not _UNSET:
        payload["visibility"] = visibility
        sent_for_verify["visibility"] = visibility
    received = _get_client().post(
        f"/rest/api/3/issue/{issue_key}/comment",
        json=payload,
    )
    _verify_top_level(sent_for_verify, received, skip={"body"})
    return _ok(received)


@_op(jira_write)
def update_comment(
    issue_key: str,
    comment_id: str,
    body: str,
    visibility: Annotated[
        dict,
        Field(description="Restrict the comment to a role or group. Same shape as add_comment.visibility."),
    ] = _UNSET,
):
    """Update an existing comment. Body is plain text (converted to ADF)."""
    payload: dict[str, Any] = {"body": _text_to_adf(body)}
    sent_for_verify: dict = {"body": body}
    if visibility is not _UNSET:
        payload["visibility"] = visibility
        sent_for_verify["visibility"] = visibility
    received = _get_client().put(
        f"/rest/api/3/issue/{issue_key}/comment/{comment_id}",
        json=payload,
    )
    _verify_top_level(sent_for_verify, received, skip={"body"})
    return _ok(received)


@_op(jira_write)
def add_watcher(issue_key: str, account_id: str):
    """Add a watcher to an issue."""
    return _ok(_get_client().post(
        f"/rest/api/3/issue/{issue_key}/watchers",
        json=account_id,
    ))


@_op(jira_write)
def create_issue_link(type: str, inward_issue: str, outward_issue: str):
    """Link two issues. Type e.g. 'Blocks', 'Duplicate', 'Relates'."""
    return _ok(_get_client().post(
        "/rest/api/3/issueLink",
        json={
            "type": {"name": type},
            "inwardIssue": {"key": inward_issue},
            "outwardIssue": {"key": outward_issue},
        },
    ))


@_op(jira_write)
def add_worklog(
    issue_key: str,
    time_spent: Annotated[
        str | None,
        Field(description="Jira format e.g. '2h 30m', '1d'. Mutually exclusive with time_spent_seconds — passing both is rejected (Jira would silently pick one)."),
    ] = _UNSET,
    time_spent_seconds: Annotated[
        int | None,
        Field(description="Integer seconds. Mutually exclusive with time_spent."),
    ] = _UNSET,
    comment: Annotated[
        str | None,
        Field(description="Plain text (converted to ADF)."),
    ] = _UNSET,
    started: Annotated[
        str | None,
        Field(description="ISO timestamp e.g. '2026-06-08T10:00:00.000+0000'. Defaults to now if omitted."),
    ] = _UNSET,
    visibility: Annotated[
        dict,
        Field(description="Restrict to role/group: {'type': 'role'|'group', 'value': '<name>'}."),
    ] = _UNSET,
):
    """Add a worklog entry. Pass exactly one of time_spent or time_spent_seconds."""
    if time_spent is not _UNSET and time_spent_seconds is not _UNSET:
        raise ValueError(
            "Pass exactly one of time_spent (Jira format e.g. '2h 30m') or "
            "time_spent_seconds (int) — Jira accepts only one and silently "
            "picks one when both are sent."
        )
    if time_spent is _UNSET and time_spent_seconds is _UNSET:
        raise ValueError(
            "Either time_spent or time_spent_seconds must be set."
        )
    body: dict[str, Any] = {}
    if time_spent is not _UNSET:
        body["timeSpent"] = time_spent
    if time_spent_seconds is not _UNSET:
        body["timeSpentSeconds"] = time_spent_seconds
    if comment is not _UNSET and isinstance(comment, str):
        body["comment"] = _text_to_adf(comment)
    if started is not _UNSET:
        body["started"] = started
    if visibility is not _UNSET:
        body["visibility"] = visibility
    return _ok(_get_client().post(f"/rest/api/3/issue/{issue_key}/worklog", json=body))


@_op(jira_write)
def upload_attachment(issue_key: str, file_path: str):
    """Upload a local file as attachment. Reads file_path with no sandboxing."""
    with open(file_path, "rb") as f:
        return _ok(_get_client().post_multipart(
            f"/rest/api/3/issue/{issue_key}/attachments",
            files={"file": (os.path.basename(file_path), f)},
        ))


# ── Execute operations (state changes on existing issues) ────────────


@_op(jira_execute)
def transition_issue(
    issue_key: str,
    transition_id: str,
    resolution: Annotated[
        str | None,
        Field(description="Resolution name (e.g. 'Done', \"Won't Do\"). Sent as fields.resolution={'name': value}. SOME workflows reject this shape and require the update.resolution=[{'set': {'name': value}}] form — if you get a 'field resolution cannot be set' error, retry passing the same value via the update= param instead."),
    ] = _UNSET,
    comment: Annotated[
        str | None,
        Field(description="Plain text (converted to ADF). Adds a comment in the same call."),
    ] = _UNSET,
    fields: Annotated[
        dict,
        Field(description="Additional field updates merged into body.fields during the transition. Wire-shape values."),
    ] = _UNSET,
    update: Annotated[
        dict,
        Field(description="Additional update operations merged into body.update. Wire-shape values, e.g. {'resolution': [{'set': {'name': 'Done'}}]}."),
    ] = _UNSET,
):
    """Transition an issue. Get available transitions via GetIssueTransitions."""
    body: dict[str, Any] = {"transition": {"id": transition_id}}
    body_fields: dict[str, Any] = {}
    body_update: dict[str, Any] = {}
    if resolution is not _UNSET:
        if resolution is None:
            body_fields["resolution"] = None
        else:
            body_fields["resolution"] = {"name": resolution}
    if fields is not _UNSET and fields:
        body_fields.update(fields)
    if update is not _UNSET and update:
        body_update.update(update)
    if comment is not _UNSET and isinstance(comment, str):
        body_update.setdefault("comment", []).append(
            {"add": {"body": _text_to_adf(comment)}}
        )
    if body_fields:
        body["fields"] = body_fields
    if body_update:
        body["update"] = body_update

    _get_client().post(f"/rest/api/3/issue/{issue_key}/transitions", json=body)

    if body_fields:
        verify_keys = sorted(body_fields.keys())
        received = _get_client().get(
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": ",".join(verify_keys)},
        )
        _verify_response(body_fields, received)
    return {"status": "ok"}


@_op(jira_execute)
def assign_issue(
    issue_key: str,
    account_id: Annotated[
        str,
        Field(description="accountId of the user to assign. To unassign, call UnassignIssue instead. Email/username are NOT accepted — only accountId."),
    ],
):
    """Assign an issue to a user. account_id is required (use UnassignIssue to clear)."""
    _get_client().put(
        f"/rest/api/3/issue/{issue_key}/assignee",
        json={"accountId": account_id},
    )
    received = _get_client().get(
        f"/rest/api/3/issue/{issue_key}",
        params={"fields": "assignee"},
    )
    fields_ = received.get("fields", {}) if isinstance(received, dict) else {}
    assignee = fields_.get("assignee") if isinstance(fields_, dict) else None
    got = assignee.get("accountId") if isinstance(assignee, dict) else None
    if got != account_id:
        raise ValueError(
            f"Assign-issue verification: requested accountId={account_id!r} "
            f"but issue {issue_key} now shows {got!r}. Jira may have "
            "substituted (unknown accountId → project default) or the "
            "PUT silently no-op'd. Check the user exists and is assignable."
        )
    return {"status": "ok", "assignee": account_id}


@_op(jira_execute)
def unassign_issue(issue_key: str):
    """Unassign an issue. Dedicated op so omission of account_id can't unassign by accident."""
    _get_client().put(
        f"/rest/api/3/issue/{issue_key}/assignee",
        json={"accountId": None},
    )
    received = _get_client().get(
        f"/rest/api/3/issue/{issue_key}",
        params={"fields": "assignee"},
    )
    fields_ = received.get("fields", {}) if isinstance(received, dict) else {}
    if fields_.get("assignee") is not None:
        raise ValueError(
            f"Unassign-issue verification: assignee on {issue_key} is "
            f"{fields_.get('assignee')!r}, expected null. The PUT silently "
            "did not clear it."
        )
    return {"status": "ok"}


# ── Delete operations ────────────────────────────────────────────────


@_op(jira_delete)
def delete_issue(issue_key: str, delete_subtasks: bool = False):
    """Delete an issue. Irreversible."""
    params = {}
    if delete_subtasks:
        params["deleteSubtasks"] = "true"
    return _ok(_get_client().delete(f"/rest/api/3/issue/{issue_key}", params=params))


@_op(jira_delete)
def delete_comment(issue_key: str, comment_id: str):
    """Delete a comment. Irreversible."""
    return _ok(_get_client().delete(f"/rest/api/3/issue/{issue_key}/comment/{comment_id}"))


@_op(jira_delete)
def delete_issue_link(link_id: str):
    """Delete an issue link."""
    return _ok(_get_client().delete(f"/rest/api/3/issueLink/{link_id}"))


@_op(jira_delete)
def remove_watcher(issue_key: str, account_id: str):
    """Remove a watcher from an issue."""
    return _ok(_get_client().delete(
        f"/rest/api/3/issue/{issue_key}/watchers",
        params={"accountId": account_id},
    ))


@_op(jira_delete)
def delete_worklog(issue_key: str, worklog_id: str):
    """Delete a worklog entry."""
    return _ok(_get_client().delete(f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}"))


@_op(jira_delete)
def delete_attachment(attachment_id: str):
    """Delete an attachment. Irreversible."""
    return _ok(_get_client().delete(f"/rest/api/3/attachment/{attachment_id}"))
