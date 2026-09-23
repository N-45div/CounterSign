"""Verify and parse Ring webhooks, and compute account-linking nonces.

Both use the HMAC signing key Ring issues with the app credentials, with
different output encodings: webhook signatures are hex with a "sha256="
prefix, linking nonces are URL-safe base64 without padding.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass


def _mac(signing_key: str, message: bytes) -> bytes:
    return hmac.new(signing_key.encode(), message, hashlib.sha256).digest()


def verify_signature(signing_key: str, raw_body: bytes, header: str | None) -> bool:
    """Check X-Signature against the raw request body, exactly as received."""
    if not header or not signing_key:
        return False
    received = header.strip().removeprefix("sha256=")
    expected = _mac(signing_key, raw_body).hex()
    return hmac.compare_digest(expected, received.lower())


def link_nonce(time_param: str | int, account_id: str, signing_key: str) -> str:
    """The nonce Ring puts on the account-link redirect: HMAC("<time>:<account_id>")."""
    mac = _mac(signing_key, f"{time_param}:{account_id}".encode())
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


@dataclass(frozen=True)
class WebhookEvent:
    request_id: str  # use for idempotency: Ring retries 5xx and timeouts
    account_id: str | None
    sent_at: str | None
    event_id: str
    type: str  # motion_detected, button_press, device_online, ...
    device_id: str | None
    timestamp_ms: int | None
    subtype: str | None  # motion, human, vehicle, other_motion
    component_ids: tuple[str, ...]


class InvalidWebhook(ValueError):
    pass


def parse_event(raw_body: bytes) -> WebhookEvent:
    try:
        payload = json.loads(raw_body)
        meta, data = payload.get("meta", {}), payload["data"]
        attrs = data.get("attributes", {})
    except (ValueError, KeyError, AttributeError) as exc:
        raise InvalidWebhook(f"not a Ring v1.1 webhook: {exc}") from exc
    source = attrs.get("source") if attrs.get("source_type") == "devices" else None
    ts = attrs.get("timestamp")
    return WebhookEvent(
        request_id=meta.get("request_id") or data.get("id", ""),
        account_id=meta.get("account_id"),
        sent_at=meta.get("time"),
        event_id=data.get("id", ""),
        type=data.get("type", "unknown"),
        device_id=source,
        timestamp_ms=int(ts) if isinstance(ts, (int, float)) else None,
        subtype=attrs.get("sub_type"),
        component_ids=tuple(str(c) for c in attrs.get("component_ids", [])),
    )


def verified_event(signing_key: str, raw_body: bytes, header: str | None) -> WebhookEvent:
    """Parse only after the signature checks out."""
    if not verify_signature(signing_key, raw_body, header):
        raise InvalidWebhook("bad or missing X-Signature")
    return parse_event(raw_body)
