from __future__ import annotations

import base64
import logging
import time

import httpx

from .config import get_settings

_log = logging.getLogger("jira_mcp.client")


class APIError(Exception):
    def __init__(self, status: int, method: str, path: str, body):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> {status}: {body}")


class JiraClient:
    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        token: str | None = None,
    ):
        s = get_settings()
        self._base = (base_url or s.jira_url).rstrip("/")
        self._email = email or s.jira_email
        self._token = token or s.jira_token
        creds = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
        # No default Content-Type — httpx sets it per request based on json=/files=/data=.
        # A default 'application/json' would force multipart upload to fail with 415.
        self._http = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Basic {creds}"},
            timeout=30.0,
        )

    def _handle(self, r: httpx.Response, *, started: float | None = None):
        if started is not None:
            duration_ms = int((time.perf_counter() - started) * 1000)
            status = r.status_code
            if status >= 500:
                level = logging.ERROR
            elif status >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO
            _log.log(
                level, "%s %s %d %dms",
                r.request.method, r.request.url.path, status, duration_ms,
            )
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise APIError(r.status_code, r.request.method, str(r.url), body)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def get(self, path: str, **kwargs):
        started = time.perf_counter()
        return self._handle(self._http.get(path, **kwargs), started=started)

    def post(self, path: str, **kwargs):
        started = time.perf_counter()
        return self._handle(self._http.post(path, **kwargs), started=started)

    def put(self, path: str, **kwargs):
        started = time.perf_counter()
        return self._handle(self._http.put(path, **kwargs), started=started)

    def delete(self, path: str, **kwargs):
        started = time.perf_counter()
        return self._handle(
            self._http.request("DELETE", path, **kwargs), started=started,
        )

    def post_multipart(self, path: str, **kwargs):
        """POST with multipart form data (no JSON content-type)."""
        headers = {"X-Atlassian-Token": "no-check"}
        started = time.perf_counter()
        return self._handle(
            self._http.post(path, headers=headers, **kwargs), started=started,
        )

    def get_raw(self, path: str, **kwargs) -> httpx.Response:
        """GET returning raw response (for binary downloads).

        Jira redirects attachment downloads to CDN. We follow the
        redirect manually without auth headers (CDN rejects them).
        """
        started = time.perf_counter()
        r = self._http.get(path, follow_redirects=False, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        status = r.status_code
        level = (
            logging.ERROR if status >= 500
            else logging.WARNING if status >= 400
            else logging.INFO
        )
        _log.log(level, "GET %s %d %dms", r.request.url.path, status, duration_ms)
        if status >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise APIError(status, r.request.method, str(r.url), body)
        if r.is_redirect:
            redirect_url = r.headers.get("location")
            if redirect_url:
                r = httpx.get(redirect_url, timeout=60.0)
                if r.status_code >= 400:
                    raise APIError(r.status_code, "GET", redirect_url, r.text)
        return r
