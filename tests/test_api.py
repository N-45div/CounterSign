import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from countersign.api import create_app, hash_key
from countersign.desk import Desk
from countersign.house import SETTLE_MS, House, HouseConfig
from countersign.receipt import HouseKey, verify
from countersign.store import MemoryStore

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


RESIDENT = "hsr_resident_456"


@pytest.fixture
def resident_world():
    from countersign.desk import ClaimRecord, ClaimStatus
    from countersign.disputes import Disputes
    from countersign.receipt import issue

    clock = Clock(T0 + 60_000)
    store = MemoryStore()
    key = HouseKey.generate()
    house = House(HouseConfig("h1", "https://h.example/h/h1", "dev", "salt"), FakeRing(), Witness(), key, store,
                  clock_ms=clock)
    desk = Desk(house, store, Recorder(), clock_ms=clock)
    disputes = Disputes(store, key, "https://h.example/h/h1", clock_ms=clock)
    token = issue(key, {"verdict": "inconclusive", "reason": "camera_not_recording"}, issuer="https://h.example/h/h1",
                  issued_at_s=T0 // 1000, receipt_id="r_1")
    record = ClaimRecord("clm_1", "shop", "ORD-9", T0, T0, carrier="Delhivery", status=ClaimStatus.ISSUED,
                         receipt_id="r_1", verdict="inconclusive", reason="camera_not_recording", token=token)
    store.put_claim(record)
    question = disputes.open(record, "Did your Delhivery parcel for ORD-9 arrive?")
    interpreted = []

    def interpret(question_id):  # stands in for the dispute agent: quotes the reply it was given
        interpreted.append(question_id)
        return disputes.resolve(question_id, "not_received", "nothing came", "test-model")

    def make(interpret_reply=interpret):
        app = create_app(desk, house, store, {"shop": hash_key(KEY)}, WEBHOOK_KEY, clock, disputes=disputes,
                         resident_key_hash=hash_key(RESIDENT), interpret_reply=interpret_reply)
        return TestClient(app)

    return make, question, interpreted


def test_resident_routes_take_only_the_resident_key(world, resident_world):
    plain, *_ = world
    assert plain.get("/v1/resident/questions", headers=auth(RESIDENT)).status_code == 503
    make, *_ = resident_world
    client = make()
    assert client.get("/v1/resident/questions").status_code == 401
    assert client.get("/v1/resident/questions", headers=auth(KEY)).status_code == 401
    assert client.get("/v1/resident/questions/q_nope", headers=auth(RESIDENT)).status_code == 404


def test_a_resident_answer_becomes_a_signed_resolution_the_merchant_can_fetch(resident_world):
    make, question, interpreted = resident_world
    client = make()
    listed = client.get("/v1/resident/questions", headers=auth(RESIDENT)).json()["questions"]
    assert [q["question_id"] for q in listed] == [question.question_id]
    before = client.get("/v1/claims/clm_1", headers=auth()).json()["dispute"]
    assert before == {"status": "open", "question": question.text, "asked_at_ms": question.asked_at_ms}

    r = client.post(f"/v1/resident/questions/{question.question_id}/answer",
                    json={"reply": "Nothing came, I was home all day"}, headers=auth(RESIDENT))
    assert r.status_code == 200 and r.json()["outcome"] == "not_received" and interpreted == [question.question_id]
    assert client.get("/v1/resident/questions", headers=auth(RESIDENT)).json()["questions"] == []

    dispute = client.get("/v1/claims/clm_1", headers=auth()).json()["dispute"]
    assert dispute["outcome"] == "not_received" and "reply" not in dispute and "quote" not in dispute
    body = verify(dispute["resolution"], client.get("/.well-known/jwks.json").json())
    assert body["kind"] == "resolution" and body["resolves"] == "r_1" and body["outcome"] == "not_received"

    again = client.post(f"/v1/resident/questions/{question.question_id}/answer", json={"reply": "actually yes"},
                        headers=auth(RESIDENT))
    assert again.status_code == 409


def test_an_answer_waiting_for_the_agent_is_202_with_a_location(resident_world):
    make, question, _ = resident_world
    client = make(interpret_reply=None)
    assert client.post(f"/v1/resident/questions/{question.question_id}/answer", json={"reply": ""},
                       headers=auth(RESIDENT)).status_code == 422
    r = client.post(f"/v1/resident/questions/{question.question_id}/answer", json={"reply": "not sure"},
                    headers=auth(RESIDENT))
    assert r.status_code == 202 and r.json()["status"] == "answered"
    assert r.headers["location"] == f"/v1/resident/questions/{question.question_id}"
