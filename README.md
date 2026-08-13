# jira-mcp

MCP server for Jira Cloud — issue tracking, comments, transitions, attachments.

## Install

```json
{
  "mcpServers": {
    "jira": {
      "command": "uvx",
      "args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/jira-mcp/simple", "jira-mcp"],
      "env": {
        "JIRA_URL": "https://myteam.atlassian.net",
        "JIRA_EMAIL": "you@example.com",
        "JIRA_TOKEN": "your-api-token",
        "MCP_JIRA_BRIEF_MAX": "100"
      }
    }
  }
}
```

Get an API token at https://id.atlassian.com/manage-profile/security/api-tokens

## Groups

| Tool | Description |
|------|-------------|
| `jira_read` | Search, list projects/boards/sprints, fetch issues/comments/transitions (read-only) |
| `jira_write` | Create/update issues, comments, links, worklogs, attachments (non-destructive) |
| `jira_execute` | Trigger state changes: transition, assign, unassign |
| `jira_delete` | Delete issues, comments, links, attachments (destructive) |

Call any group with `operation="help"` to list operations and their typed signatures; `operation="schema"` returns JSON Schema for one op (`params={"op": "OpName"}`).

## Environment

| Var | Required | Default | Purpose |
|---|---|---|---|
| `JIRA_URL` | yes | — | Atlassian site URL, e.g. `https://myteam.atlassian.net` |
| `JIRA_EMAIL` | yes | — | Atlassian account email (basic-auth pair with token) |
| `JIRA_TOKEN` | yes | — | API token from id.atlassian.com |
| `MCP_JIRA_BRIEF_MAX` | no | `100` | Max length of `<brief>` summary on issue descriptions. `0` disables enforcement |

## Gotchas

**`parent_key` vs Epic Link customfield.** Team-managed (next-gen) projects accept `parent_key="STS-1"` and turn it into `fields.parent={"key": "STS-1"}`. Company-managed (classic) projects with Epic Link enabled require `custom_fields={"customfield_NNNNN": "STS-1"}` where `NNNNN` is the Epic Link field id (varies per site — see `jira_read.ListFields`). `parent_key` will not error in this case but the link won't be drawn either; the brief idea is to use `custom_fields` when working with classic projects.

**ADF.** `description`, `environment`, and comment bodies are converted from plain text to Atlassian Document Format. **No markdown is rendered** — `*bold*` stays literal. Roundtrip is lossy (the `body` returned by Jira is normalized ADF, not your input text), so `_verify_top_level` skips `body` from the verify pass.

**accountId vs username/email.** Jira Cloud accepts only `accountId` (e.g. `5e9c3a99...`) for `assignee`/`reporter`/etc. Email or username will be silently rewritten or rejected. Look up accountIds with `jira_read.SearchUsers`.

**`<brief>` requirement on `create_issue.description`.** When `MCP_JIRA_BRIEF_MAX > 0` (default 100), the description must contain `<brief>one-line summary</brief>`. Set `MCP_JIRA_BRIEF_MAX=0` in env to disable. Brief is enforced on `description` only (not on comment bodies).

**Verify-writes.** After every write that returns a resource, jira-mcp does either zero extra RTT (`update_issue` uses `?returnIssue=true`, comment ops use the POST response) or one slim follow-up GET (`create_issue`/`transition_issue`/`assign_issue`/`unassign_issue`, with `?fields=<sent keys>`). The verify pass catches *dropped* fields — Jira silently ignored what we sent. It does NOT catch *substituted* values — if Jira swaps an invalid `priority="Highest"` for the project default, the key is present but the value differs. Strict-equality checks (assignee accountId, unassign null) are done inline as separate raises.

**Resolution on update.** `update_issue` rejects `resolution` in `custom_fields` because most workflow screens drop it silently. Use `jira_execute.TransitionIssue(..., resolution=...)`. If the workflow screen still rejects the `fields.resolution` shape with a 400, retry passing the value via the `update=` parameter: `update={"resolution": [{"set": {"name": "Done"}}]}`.

**`upload_attachment` reads any local file.** The op passes `file_path` straight to `open(...)`. An agent with this tool can read and POST any file the MCP process can read. Treat reach as equivalent to the process's filesystem scope; do not run jira-mcp with elevated privileges or wider filesystem access than you'd grant the model.

**`download_attachment` path-traversal hardening.** The `path is None` branch sanitizes the server-supplied `filename` via `os.path.basename(...) or f"attachment_<id>"`, caps it at 255 chars, and saves into a fresh per-call `mkdtemp("jira_mcp_")` directory. Explicit `path=...` is the caller's responsibility.

## Development

Tested against a real Jira Cloud sandbox. Unit tests use `httpx.MockTransport` (no network); integration tests are gated on env vars.

`tests/test_swagger_conformance.py` reads every registered op off its own AST and asserts method, path, query-param names and top-level body-field names against Atlassian's published OpenAPI documents, vendored gzipped under `tests/specs/`. It needs neither network nor credentials; its module docstring carries the refresh commands for the vendored copies.

```bash
uv sync
npm run test               # unit tests, no network
npm run jira:bootstrap     # verify tests/.env against your sandbox
npm run test:integration   # end-to-end against real Jira
```

`tests/.env` (gitignored) must contain `JIRA_URL`/`JIRA_EMAIL`/`JIRA_TOKEN`/`JIRA_TEST_PROJECT`. CI runs the integration job against the same secrets when set on the repo.
