"""A stand-in for requests.Session that answers from a routing table."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict


def response(status: int = 200, body=None, *, content: bytes | None = None, headers: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.headers = CaseInsensitiveDict(headers or {})
    if body is not None:
        resp._content = json.dumps(body).encode()
        resp.headers.setdefault("Content-Type", "application/json")
    else:
        resp._content = content or b""
    return resp


class FakeSession:
    """Routes are (method, path) -> handler(call) or a list of responses served in order."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []

    def on(self, method: str, path: str, handler) -> "FakeSession":
        self.routes[(method.upper(), path)] = handler
        return self

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, allow_redirects=True):
        parts = urlsplit(url)
        query = dict(p.split("=", 1) for p in parts.query.split("&") if p) if parts.query else {}
        call = {
            "method": method.upper(),
            "url": url,
            "path": parts.path,
            "params": {**query, **(params or {})},
            "json": json,
            "headers": dict(headers or {}),
        }
        self.calls.append(call)
        key = (call["method"], parts.path)
        if key not in self.routes:
            key = (call["method"], url)
        handler = self.routes.get(key)
        if handler is None:
            return response(404, {"errors": [{"status": "404", "title": "Not Found", "detail": url}]})
        if isinstance(handler, list):
            return handler.pop(0) if len(handler) > 1 else handler[0]
        return handler(call)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)
