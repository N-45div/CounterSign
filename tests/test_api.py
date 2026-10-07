import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from handshake.api import create_app, hash_key
from handshake.desk import Desk
from handshake.house import SETTLE_MS, House, HouseConfig
from handshake.receipt import HouseKey, verify
from handshake.store import MemoryStore

from .test_desk import T0, Clock, FakeRing, Recorder, Witness

KEY = "hsk_test_shop_123"
WEBHOOK_KEY = "ring-hmac-key"


@pytest.fixture
def world():
    clock = Clock(T0 + 60_000)
    store = MemoryStore()
    house = House(HouseConfig("h1", "https://h.example/h/h1", "dev", "salt"), FakeRing(), Witness(),
                  HouseKey.generate(), store, clock_ms=clock)
    desk = Desk(house, store, Recorder(), clock_ms=clock)
    app = create_app(desk, house, store, {"shop": hash_key(KEY), "other": hash_key("hsk_other")}, WEBHOOK_KEY, clock)
    return TestClient(app), clock, house, store


def auth(key=KEY):
    return {"Authorization": f"Bearer {key}"}


def test_health_and_public_keys(world):
    client, _, house, _ = world
    assert client.get("/healthz").json() == {"ok": True, "house": "h1"}
    jwks = client.get("/.well-known/jwks.json")
    assert jwks.json() == house.jwks() and "max-age" in jwks.headers["cache-control"]


def test_claims_need_a_merchant_key(world):
    client, *_ = world
    body = {"order_id": "ORD-1", "claimed_at": T0}
    assert client.post("/v1/claims", json=body).status_code == 401
    assert client.post("/v1/claims", json=body, headers=auth("wrong")).status_code == 401


def test_a_claim_goes_pending_then_issued(world):
    client, clock, house, _ = world
    r = client.post("/v1/claims", json={"order_id": "ORD-1", "claimed_at": T0 + 60_000, "carrier": "Delhivery"},
                    headers=auth())
    assert r.status_code == 202
    pending = r.json()
    assert pending["status"] == "pending" and pending["retry_after_s"] == SETTLE_MS // 1000
    assert r.headers["location"] == f"/v1/claims/{pending['claim_id']}"
    assert r.headers["retry-after"] == str(SETTLE_MS // 1000)

    clock.now += SETTLE_MS
    done = client.get(f"/v1/claims/{pending['claim_id']}", headers=auth())
    assert done.status_code == 200
    body = done.json()
    assert body["verdict"] == "delivered" and body["reason"] == "package_appeared"
    assert verify(body["receipt"], client.get("/.well-known/jwks.json").json())["order_id"] == "ORD-1"


def test_iso_timestamps_need_a_timezone(world):
    client, *_ = world
    ok = client.post("/v1/claims", json={"order_id": "ORD-2", "claimed_at": "2026-09-21T19:46:40+05:30"},
                     headers=auth())
    assert ok.status_code in (200, 202)
    naive = client.post("/v1/claims", json={"order_id": "ORD-3", "claimed_at": "2026-09-21T14:16:40"}, headers=auth())
    assert naive.status_code == 422 and naive.json()["error"] == "invalid_claim"


def test_another_merchant_gets_404_not_the_claim(world):
    client, *_ = world
    claim_id = client.post("/v1/claims", json={"order_id": "ORD-1", "claimed_at": T0 + 60_000},
                           headers=auth()).json()["claim_id"]
    assert client.get(f"/v1/claims/{claim_id}", headers=auth("hsk_other")).status_code == 404


def test_schema_errors_are_422(world):
    client, *_ = world
    assert client.post("/v1/claims", json={"claimed_at": T0}, headers=auth()).status_code == 422


def signed(body: dict, key=WEBHOOK_KEY):
    raw = json.dumps(body).encode()
    return raw, {"X-Signature": "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(),
                 "Content-Type": "application/json"}


OFFLINE = {"meta": {"version": "1.1", "request_id": "req-9", "account_id": "acct"},
           "data": {"id": "dev_device_offline_1", "type": "device_offline",
                    "attributes": {"source": "dev", "source_type": "devices", "timestamp": T0 + 5_000}}}


def test_webhook_records_status_once_and_rejects_bad_signatures(world):
    client, _, _, store = world
    raw, headers = signed(OFFLINE)
    first = client.post("/webhooks/ring", content=raw, headers=headers)
    assert first.status_code == 200 and first.json()["type"] == "device_offline"
    again = client.post("/webhooks/ring", content=raw, headers=headers)
    assert again.json()["duplicate"] is True
    assert store.status_log["dev"] == [(T0 + 5_000, False)]

    _, bad_headers = signed(OFFLINE, key="someone-else")
    assert client.post("/webhooks/ring", content=raw, headers=bad_headers).status_code == 401
