"""Where the house gets its Ring access token.

* StaticToken: a token handed in directly (tests, local runs).
* SecretToken: a token kept in AWS Secrets Manager. The Ring Developer
  Playground issues 30-minute tokens, so the deployed house rereads the secret
  instead of baking a token into its configuration.
* RefreshingToken: the production path. Ring access tokens last about four
  hours and refresh tokens about 30 days; every refresh returns a new refresh
  token that replaces the old one, so the new one is saved before use.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

import requests

from .ring import RingAuthError

OAUTH_URL = "https://oauth.ring.com/oauth/token"
EARLY_REFRESH_S = 120


def clean_token(raw: str) -> str:
    """Accept a bare token, a JSON {"access_token": ...}, or the Playground's `export TOKEN="..."` line."""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            return str(json.loads(raw).get("access_token", "")).strip()
        except ValueError:
            return ""
    match = re.search(r"TOKEN=[\"']?([^\"'\s]+)", raw)
    return match.group(1) if match else raw.strip("\"'")


class StaticToken:
    def __init__(self, token: str):
        self.token = clean_token(token)

    def __call__(self) -> str:
        if not self.token:
            raise RingAuthError(401, "No Ring token", "no token configured")
        return self.token


class SecretToken:
    def __init__(self, secrets_client, secret_id: str, ttl_s: float = 60, clock: Callable[[], float] = time.time):
        self.client = secrets_client
        self.secret_id = secret_id
        self.ttl_s = ttl_s
        self._clock = clock
        self._value = ""
        self._read_at = float("-inf")

    def __call__(self) -> str:
        if self._clock() - self._read_at >= self.ttl_s:
            raw = self.client.get_secret_value(SecretId=self.secret_id).get("SecretString", "")
            self._value = clean_token(raw)
            self._read_at = self._clock()
        if not self._value:
            raise RingAuthError(401, "No Ring token", f"secret {self.secret_id} is empty")
        return self._value


class RefreshingToken:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        load_refresh: Callable[[], str],
        save_refresh: Callable[[str], None],
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self._load = load_refresh
        self._save = save_refresh
        self.session = session or requests.Session()
        self._clock = clock
        self._access = ""
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._access and self._clock() < self._expires_at - EARLY_REFRESH_S:
            return self._access
        resp = self.session.post(OAUTH_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._load(),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, timeout=30)
        if resp.status_code != 200:
            raise RingAuthError(resp.status_code, "Token refresh failed", resp.text[:300])
        body = resp.json()
        if body.get("refresh_token"):
            self._save(body["refresh_token"])  # the old refresh token is now spent
        self._access = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 14_400))
        return self._access
