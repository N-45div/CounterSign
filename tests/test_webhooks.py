import hashlib
import hmac
import json

import pytest

from handshake.webhooks import InvalidWebhook, link_nonce, parse_event, verified_event, verify_signature

KEY = "test-signing-key"

MOTION = json.dumps({
    "meta": {"version": "1.1", "time": "2026-09-23T10:00:00Z", "request_id": "req-1",
             "account_id": "ava1.ring.account.ABC"},
    "data": {"id": "dev_human_1790000000000", "type": "motion_detected",
             "attributes": {"source": "ava1.ring.device.FRONT", "source_type": "devices",
                            "timestamp": 1790000000000, "sub_type": "human", "component_ids": ["0"]}},
}, separators=(",", ":")).encode()


def sign(body: bytes, key: str = KEY) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def test_accepts_a_correct_signature():
    assert verify_signature(KEY, MOTION, sign(MOTION))


def test_rejects_tampered_body_wrong_key_and_missing_header():
    assert not verify_signature(KEY, MOTION.replace(b"human", b"other"), sign(MOTION))
    assert not verify_signature(KEY, MOTION, sign(MOTION, "other-key"))
    assert not verify_signature(KEY, MOTION, None)
    assert not verify_signature("", MOTION, sign(MOTION))


def test_reserialised_json_does_not_verify():
    # Ring signs the raw bytes; parsing and re-dumping changes whitespace.
    reserialised = json.dumps(json.loads(MOTION), indent=1).encode()
    assert not verify_signature(KEY, reserialised, sign(MOTION))


def test_parses_a_motion_event():
    ev = parse_event(MOTION)
    assert ev.type == "motion_detected" and ev.subtype == "human"
    assert ev.device_id == "ava1.ring.device.FRONT" and ev.timestamp_ms == 1790000000000
    assert ev.request_id == "req-1" and ev.account_id == "ava1.ring.account.ABC"
    assert ev.component_ids == ("0",)


def test_verified_event_refuses_unsigned_payloads():
    with pytest.raises(InvalidWebhook):
        verified_event(KEY, MOTION, "sha256=deadbeef")
    assert verified_event(KEY, MOTION, sign(MOTION)).subtype == "human"


def test_garbage_is_an_invalid_webhook():
    with pytest.raises(InvalidWebhook):
        parse_event(b"not json")


def test_link_nonce_matches_ring_encoding():
    # Recomputed independently: URL-safe base64 of HMAC-SHA256, no padding.
    import base64
    mac = hmac.new(KEY.encode(), b"1771130906289:ava1.ring.account.ABC", hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(mac).decode().rstrip("=")
    assert link_nonce(1771130906289, "ava1.ring.account.ABC", KEY) == expected
    assert "=" not in expected and "+" not in expected and "/" not in expected
