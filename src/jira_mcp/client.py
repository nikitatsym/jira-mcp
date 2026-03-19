import base64

import httpx

from .config import get_settings


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
        headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        }
        self._http = httpx.Client(
            base_url=self._base,
            headers=headers,
            timeout=30.0,
        )

    def _handle(self, r: httpx.Response):
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
        return self._handle(self._http.get(path, **kwargs))

    def post(self, path: str, **kwargs):
        return self._handle(self._http.post(path, **kwargs))

    def put(self, path: str, **kwargs):
        return self._handle(self._http.put(path, **kwargs))

    def delete(self, path: str, **kwargs):
        return self._handle(self._http.request("DELETE", path, **kwargs))

    def post_multipart(self, path: str, **kwargs):
        """POST with multipart form data (no JSON content-type)."""
        headers = {"X-Atlassian-Token": "no-check"}
        return self._handle(self._http.post(path, headers=headers, **kwargs))

    def get_raw(self, path: str, **kwargs) -> httpx.Response:
        """GET returning raw response (for binary downloads).

        Jira redirects attachment downloads to CDN. We follow the
        redirect manually without auth headers (CDN rejects them).
        """
        r = self._http.get(path, follow_redirects=False, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise APIError(r.status_code, r.request.method, str(r.url), body)
        if r.is_redirect:
            redirect_url = r.headers.get("location")
            if redirect_url:
                r = httpx.get(redirect_url, timeout=60.0)
                if r.status_code >= 400:
                    raise APIError(r.status_code, "GET", redirect_url, r.text)
        return r
