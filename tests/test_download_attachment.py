"""Path-traversal hardening for download_attachment."""

from __future__ import annotations

import os

import httpx

from jira_mcp.tools import download_attachment


def _meta_then_content(filename: str, content: bytes = b"payload"):
    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/attachment/content/" in req.url.path:
            return httpx.Response(200, content=content)
        if req.method == "GET" and "/attachment/" in req.url.path:
            return httpx.Response(200, json={
                "id": "1",
                "filename": filename,
                "size": len(content),
            })
        return httpx.Response(500, json={"err": "unexpected"})

    return _h


class TestDownloadAttachment:
    def test_normal_filename(self, mock_jira):
        mock_jira.handler(_meta_then_content("report.pdf"))
        out = download_attachment(attachment_id="1")
        assert os.path.basename(out["path"]) == "report.pdf"
        assert out["path"].startswith(os.path.join(os.environ.get("TMPDIR", "/tmp"), "jira_mcp_")) or "jira_mcp_" in out["path"]

    def test_hostile_traversal(self, mock_jira):
        """A `../../etc/passwd` filename must NOT escape the temp dir."""
        mock_jira.handler(_meta_then_content("../../etc/passwd"))
        out = download_attachment(attachment_id="1")
        # The saved path stays inside the chosen mkdtemp dir.
        assert os.path.basename(out["path"]) == "passwd"
        assert "jira_mcp_" in out["path"]
        # No escape — the parent dir of the file is the mkdtemp dir, not /etc.
        parent = os.path.dirname(out["path"])
        assert os.path.basename(parent).startswith("jira_mcp_")

    def test_empty_filename_fallback(self, mock_jira):
        mock_jira.handler(_meta_then_content(""))
        out = download_attachment(attachment_id="42")
        assert os.path.basename(out["path"]) == "attachment_42"

    def test_traversal_only_fallback(self, mock_jira):
        """`basename("../")` is "" — fallback should kick in."""
        mock_jira.handler(_meta_then_content("../"))
        out = download_attachment(attachment_id="99")
        assert os.path.basename(out["path"]) == "attachment_99"

    def test_overlong_truncated(self, mock_jira):
        long = "a" * 500 + ".pdf"
        mock_jira.handler(_meta_then_content(long))
        out = download_attachment(attachment_id="1")
        assert len(os.path.basename(out["path"])) == 255

    def test_writes_payload(self, mock_jira):
        mock_jira.handler(_meta_then_content("report.pdf", b"hello world"))
        out = download_attachment(attachment_id="1")
        with open(out["path"], "rb") as f:
            assert f.read() == b"hello world"
        assert out["size"] == 11
