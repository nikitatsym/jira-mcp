"""Jira tool operations. All public functions are auto-registered as MCP tools."""

import os
import tempfile

from .client import JiraClient
from .registry import ROOT, Group, _op

# ── Client singleton ──────────────────────────────────────────────────

_client: JiraClient | None = None


def _get_client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient()
    return _client


def _ok(data):
    if data is None:
        return {"status": "ok"}
    return data


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


# ── ADF helpers ───────────────────────────────────────────────────────


def _text_to_adf(text: str) -> dict:
    """Wrap plain text into minimal ADF paragraph nodes."""
    paragraphs = []
    for line in text.split("\n"):
        if line:
            paragraphs.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            })
        else:
            paragraphs.append({"type": "paragraph", "content": []})
    return {"version": 1, "type": "doc", "content": paragraphs}


def _adf_to_text(adf: dict) -> str:
    """Extract plain text from ADF document."""
    if not isinstance(adf, dict):
        return str(adf) if adf else ""
    parts = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(adf)
    return "\n".join(parts) if parts else ""


# ── Groups ────────────────────────────────────────────────────────────

jira_read = Group(
    "jira_read",
    "Query Jira data (safe, read-only).\n\n"
    "Call with operation=\"help\" to list all available read operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_read(operation=\"SearchIssues\", "
    "params={\"jql\": \"project = PROJ AND status = Open\"})",
)

jira_write = Group(
    "jira_write",
    "Create or update Jira resources (non-destructive).\n\n"
    "Call with operation=\"help\" to list all available write operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_write(operation=\"CreateIssue\", "
    "params={\"project_key\": \"PROJ\", \"issue_type\": \"Task\", \"summary\": \"Fix login bug\"})",
)

jira_delete = Group(
    "jira_delete",
    "Delete Jira resources (destructive, irreversible).\n\n"
    "Call with operation=\"help\" to list all available delete operations.\n"
    "Otherwise pass the operation name and a JSON object with parameters.\n\n"
    "Example: jira_delete(operation=\"DeleteIssue\", "
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
    except Exception:
        service = {"status": "error"}
    return {"mcp": version("jira-mcp"), "service": service}


# ── Read operations ──────────────────────────────────────────────────


@_op(jira_read)
def search_issues(jql: str, limit: int = 20, next_page_token: str | None = None):
    """Search issues using JQL. Paginate with next_page_token from response nextPageToken."""
    _SEARCH_FIELDS = ",".join(_SLIM_ISSUE_FIELDS)
    params: dict = {"jql": jql, "maxResults": limit, "fields": _SEARCH_FIELDS}
    if next_page_token is not None:
        params["nextPageToken"] = next_page_token
    data = _get_client().get(
        "/rest/api/3/search/jql",
        params=params,
    )
    if isinstance(data, dict) and "issues" in data:
        data["issues"] = [_slim_issue(i) for i in data["issues"]]
    return data


@_op(jira_read)
def get_issue(issue_key: str):
    """Get full detail of a specific issue."""
    data = _get_client().get(f"/rest/api/3/issue/{issue_key}")
    if isinstance(data, dict):
        _clean_issue(data)
    return data


def _clean_issue(issue: dict) -> None:
    """Remove noise from a full issue response (in-place)."""
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return
    # Drop custom fields with null values
    to_drop = [k for k, v in fields.items() if k.startswith("customfield_") and v is None]
    for k in to_drop:
        del fields[k]
    # Remove embedded worklog (use dedicated operation)
    fields.pop("worklog", None)
    # Slim embedded comments
    comment_data = fields.get("comment")
    if isinstance(comment_data, dict) and "comments" in comment_data:
        comment_data["comments"] = [_slim_comment(c) for c in comment_data["comments"]]
    # Strip avatarUrls from nested user objects
    _strip_avatars(fields)


def _strip_avatars(obj):
    """Recursively remove avatarUrls from dicts."""
    if isinstance(obj, dict):
        obj.pop("avatarUrls", None)
        for v in obj.values():
            _strip_avatars(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_avatars(item)


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
    """Download an attachment. Omit path to save to OS temp dir. Returns saved filepath and size."""
    meta = _get_client().get(f"/rest/api/3/attachment/{attachment_id}")
    filename = meta.get("filename", f"attachment_{attachment_id}")
    if path is None:
        path = os.path.join(tempfile.gettempdir(), filename)
    r = _get_client().get_raw(f"/rest/api/3/attachment/content/{attachment_id}")
    with open(path, "wb") as f:
        f.write(r.content)
    return {"path": path, "size": len(r.content), "filename": filename}


# ── Write operations ─────────────────────────────────────────────────


@_op(jira_write)
def create_issue(
    project_key: str,
    issue_type: str,
    summary: str,
    description: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    labels: list | None = None,
    components: list | None = None,
):
    """Create a new issue. Description is plain text (converted to ADF)."""
    fields: dict = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description is not None:
        fields["description"] = _text_to_adf(description)
    if priority is not None:
        fields["priority"] = {"name": priority}
    if assignee is not None:
        fields["assignee"] = {"accountId": assignee}
    if labels is not None:
        fields["labels"] = labels
    if components is not None:
        fields["components"] = [{"name": c} for c in components]
    return _ok(_get_client().post("/rest/api/3/issue", json={"fields": fields}))


@_op(jira_write)
def update_issue(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    labels: list | None = None,
    components: list | None = None,
):
    """Update an issue. Only provided fields are changed."""
    fields: dict = {}
    if summary is not None:
        fields["summary"] = summary
    if description is not None:
        fields["description"] = _text_to_adf(description)
    if priority is not None:
        fields["priority"] = {"name": priority}
    if assignee is not None:
        fields["assignee"] = {"accountId": assignee}
    if labels is not None:
        fields["labels"] = labels
    if components is not None:
        fields["components"] = [{"name": c} for c in components]
    return _ok(_get_client().put(f"/rest/api/3/issue/{issue_key}", json={"fields": fields}))


@_op(jira_write)
def transition_issue(issue_key: str, transition_id: str, comment: str | None = None):
    """Transition an issue to a new status. Use get_issue_transitions first."""
    body: dict = {"transition": {"id": transition_id}}
    if comment is not None:
        body["update"] = {
            "comment": [{"add": {"body": _text_to_adf(comment)}}]
        }
    return _ok(_get_client().post(f"/rest/api/3/issue/{issue_key}/transitions", json=body))


@_op(jira_write)
def assign_issue(issue_key: str, account_id: str | None = None):
    """Assign an issue to a user. Pass null to unassign."""
    return _ok(_get_client().put(
        f"/rest/api/3/issue/{issue_key}/assignee",
        json={"accountId": account_id},
    ))


@_op(jira_write)
def add_comment(issue_key: str, body: str):
    """Add a comment to an issue. Body is plain text (converted to ADF)."""
    return _ok(_get_client().post(
        f"/rest/api/3/issue/{issue_key}/comment",
        json={"body": _text_to_adf(body)},
    ))


@_op(jira_write)
def update_comment(issue_key: str, comment_id: str, body: str):
    """Update an existing comment. Body is plain text (converted to ADF)."""
    return _ok(_get_client().put(
        f"/rest/api/3/issue/{issue_key}/comment/{comment_id}",
        json={"body": _text_to_adf(body)},
    ))


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
def add_worklog(issue_key: str, time_spent: str, comment: str | None = None):
    """Add a worklog entry. time_spent e.g. '2h 30m'."""
    body: dict = {"timeSpent": time_spent}
    if comment is not None:
        body["comment"] = _text_to_adf(comment)
    return _ok(_get_client().post(f"/rest/api/3/issue/{issue_key}/worklog", json=body))


@_op(jira_write)
def upload_attachment(issue_key: str, file_path: str):
    """Upload a local file as an attachment to an issue."""
    with open(file_path, "rb") as f:
        return _ok(_get_client().post_multipart(
            f"/rest/api/3/issue/{issue_key}/attachments",
            files={"file": (os.path.basename(file_path), f)},
        ))


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
