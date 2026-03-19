# jira-mcp

MCP server for Jira Cloud — issue tracking, project management, and agile workflows.

## Install

```
uvx --extra-index-url https://nikitatsym.github.io/jira-mcp/simple jira-mcp
```

## Configure

```json
{
  "mcpServers": {
    "jira": {
      "command": "uvx",
      "args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/jira-mcp/simple", "jira-mcp"],
      "env": {
        "JIRA_URL": "https://myteam.atlassian.net",
        "JIRA_EMAIL": "you@example.com",
        "JIRA_TOKEN": "your-api-token"
      }
    }
  }
}
```

Get an API token at https://id.atlassian.com/manage-profile/security/api-tokens

## Groups

| Tool | Description |
|------|-------------|
| `jira_read` | Search issues, list projects, boards, sprints (read-only) |
| `jira_write` | Create/update issues, comments, links, worklogs (non-destructive) |
| `jira_delete` | Delete issues, comments, links (destructive) |

Call any group with `operation="help"` to list available operations.
