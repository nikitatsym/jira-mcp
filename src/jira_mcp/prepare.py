"""Shared helpers for tools.py: payload assembly, ADF, verify-writes."""

from __future__ import annotations

import re
from typing import Any

from .config import get_settings
from .registry import _UNSET

_BRIEF_RE = re.compile(r"<brief>(.*?)</brief>", re.DOTALL)


# ── Brief tag validation ────────────────────────────────────────────────────


def _extract_brief(text: str | None) -> str | None:
    """Extract <brief>...</brief> summary from text."""
    if not text:
        return None
    m = _BRIEF_RE.search(text)
    return m.group(1).strip() if m else None


def _validate_brief(text: str | None) -> None:
    """Raise if MCP_JIRA_BRIEF_MAX>0 and text lacks/exceeds <brief>...</brief>."""
    brief_max = get_settings().mcp_jira_brief_max
    if brief_max == 0:
        return
    if not text or not _BRIEF_RE.search(text):
        raise ValueError(
            "description must contain a <brief>one-line summary</brief> tag. "
            "Add it at the top of the description text, or set "
            "MCP_JIRA_BRIEF_MAX=0 to disable this requirement."
        )
    brief = _extract_brief(text)
    if brief and len(brief) > brief_max:
        raise ValueError(
            f"<brief> too long: {len(brief)} chars, max {brief_max}. "
            "Keep it to a concise one-liner."
        )


# ── Payload assembly ────────────────────────────────────────────────────────


def _ok(data):
    if data is None:
        return {"status": "ok"}
    return data


def _fields(payload: dict) -> dict:
    """Wrap a field payload into Jira's `{"fields": ...}` envelope."""
    return {"fields": payload}


def _body(
    local_vars: dict,
    exclude=(),
    rename: dict | None = None,
    keep_null=(),
) -> dict:
    """Drop _UNSET/None from locals; keep_null= opts specific keys into JSON null."""
    excl = set(exclude)
    rmap = rename or {}
    keep = set(keep_null)
    out: dict = {}
    for k, v in local_vars.items():
        if k in excl:
            continue
        if v is _UNSET:
            continue
        if v is None and k not in keep:
            continue
        out[rmap.get(k, k)] = v
    return out


# ── ADF helpers ─────────────────────────────────────────────────────────────


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
    parts: list[str] = []

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


# ── Field-shape mapper ──────────────────────────────────────────────────────


# Names we already reshape via dedicated params — `custom_fields` containing
# any of these is a layering mistake (caller bypassing the Python layer);
# reject fail-fast pointing at the dedicated param.
_RESHAPED_WIRE_NAMES = frozenset({
    "parent", "assignee", "reporter", "priority", "duedate",
    "versions", "fixVersions", "components", "labels", "summary",
    "description", "environment",
})


def _prepare_issue_fields(sent: dict) -> dict:
    """Map _body() output to Jira wire shapes. custom_fields merges flat."""
    mapped: dict[str, Any] = {}
    for k, v in sent.items():
        if k == "custom_fields":
            continue  # handled at the bottom after the rest is built
        if k in ("description", "environment"):
            mapped[k] = _text_to_adf(v) if isinstance(v, str) else v
        elif k in ("assignee", "reporter"):
            mapped[k] = {"accountId": v} if isinstance(v, str) else v
        elif k == "priority":
            mapped[k] = {"name": v} if isinstance(v, str) else v
        elif k == "parent":
            mapped[k] = {"key": v} if isinstance(v, str) else v
        elif k == "components":
            mapped[k] = [{"name": c} for c in v] if isinstance(v, list) else v
        elif k in ("fixVersions", "versions"):
            mapped[k] = [{"name": x} for x in v] if isinstance(v, list) else v
        else:
            mapped[k] = v  # labels, duedate, summary, anything else

    cf = sent.get("custom_fields")
    if cf:
        if not isinstance(cf, dict):
            raise ValueError(
                f"custom_fields must be a dict of {{wire_key: value}}, "
                f"got {type(cf).__name__}."
            )
        collisions = set(cf) & set(mapped)
        if collisions:
            raise ValueError(
                f"custom_fields keys collide with explicit params: "
                f"{sorted(collisions)}. Pass the value via the named param "
                "(e.g. summary=, duedate=) instead of in custom_fields."
            )
        shadow = set(cf) & _RESHAPED_WIRE_NAMES
        if shadow:
            raise ValueError(
                f"custom_fields contains keys that have dedicated params: "
                f"{sorted(shadow)}. Use the named param instead (e.g. "
                "parent_key= for parent, assignee= for assignee)."
            )
        mapped.update(cf)

    return mapped


# ── Verify-writes ───────────────────────────────────────────────────────────


def _verify_response(
    sent_fields: dict,
    received_issue: dict | None,
    *,
    skip: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Walk sent keys against received_issue["fields"]; raise on silent drop.

    Catches *dropped* fields (key absent from response), NOT *substituted*
    values (key present, value swapped — e.g. Jira replacing an invalid
    priority with the project default). For ops where the value itself is
    the contract (assign/unassign), use an inline value-equality check.
    """
    if not sent_fields:
        return
    if not received_issue or not isinstance(received_issue, dict):
        raise ValueError(
            "Verify-writes: write succeeded but the response was empty or "
            "non-dict. Likely cause: the follow-up GET / returnIssue path "
            "is wired wrong. The write may or may not have applied — "
            "re-read the resource and reconcile manually."
        )
    fields = received_issue.get("fields")
    if not isinstance(fields, dict) or not fields:
        keys = sorted(received_issue.keys()) if received_issue else []
        raise ValueError(
            f"Verify-writes: write succeeded but response did not include "
            f"'fields' (got top-level keys: {keys}). Likely causes: PUT "
            "was issued without ?returnIssue=true, the follow-up GET hit "
            "the wrong path, or Jira returned an unexpected shape. The "
            "write may or may not have applied — re-read the resource "
            "and reconcile manually."
        )
    skip_set = set(skip)
    for key in sent_fields:
        if key in skip_set:
            continue
        if key not in fields:
            raise ValueError(
                f"API silently dropped {key!r}. Resource was "
                "created/updated but the field was ignored. Common "
                "causes: field not on the screen scheme, permission "
                "missing, value format wrong (e.g. status name vs id, "
                "assignee email vs accountId)."
            )


def _verify_top_level(
    sent: dict,
    received: dict | None,
    *,
    skip: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Like _verify_response but flat — for comment endpoints (no fields wrapper)."""
    if not sent:
        return
    if not received or not isinstance(received, dict):
        raise ValueError(
            "Verify-writes: write succeeded but response body was empty or "
            "non-dict. Likely cause: the helper was called with a 204/None "
            "response. Re-check the call site."
        )
    skip_set = set(skip)
    for key in sent:
        if key in skip_set:
            continue
        if key not in received:
            raise ValueError(
                f"API silently dropped {key!r}. Resource was "
                "created/updated but the field was ignored. Common "
                "causes: permission missing, value format wrong, screen "
                "scheme rejected the field."
            )
