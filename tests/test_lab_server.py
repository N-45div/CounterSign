import socket
import threading
import time

import boto3
import pytest
import requests
import uvicorn
from moto import mock_aws

from countersign.dispute_agent import RemoteDisputeAgent
from countersign.lab.server import create_lab
from countersign.lab.sessions import DynamoSessions, MemorySessions
from countersign.receipt import HouseKey, verify
from countersign.store import MemoryStore


def serve_lab(**options):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_lab(MemorySessions(), MemoryStore(), HouseKey.generate(), "lab-webhook-key", None, base, **options)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def lab_url():
    yield from serve_lab()


def fake_agent(disputes):
    """The dispute agent's proposals without a model: a fixed question, and the reply's first words quoted."""
    def invoke(payload):
        if payload["task"] == "question":
            return {"question": f"Did your {payload['facts']['carrier']} parcel for {payload['facts']['order_id']} arrive?"}
        reply = payload["exchange"]["reply"]
        outcome = "not_received" if reply.lower().startswith("no") else "received"
        return {"interpretation": {"outcome": outcome, "quote": " ".join(reply.split()[:3]), "note": ""}}

    return RemoteDisputeAgent(invoke, disputes, "fake-agent")


@pytest.fixture(scope="module")
def agent_lab_url():
    yield from serve_lab(dispute_agent=fake_agent, agent_label="Strands on AgentCore Runtime")


def start(base, scenario):
    return requests.post(f"{base}/lab/start", json={"scenario": scenario}, timeout=30).json()


def claim(base, tl, order):
    return requests.post(f"{base}/lab/s/{tl['session']}/claim",
                         json={"order_id": order, "claimed_at": tl["claim_at_ms"], "carrier": "Lab Courier"},
                         timeout=60).json()


def ask(base, tl, claim_id):
    return requests.post(f"{base}/lab/s/{tl['session']}/claims/{claim_id}/ask", timeout=30)


def reply(base, tl, question_id, text):
    return requests.post(f"{base}/lab/s/{tl['session']}/questions/{question_id}/answer", json={"reply": text},
                         timeout=30)


def test_a_session_runs_end_to_end_with_evidence_and_a_verifiable_receipt(lab_url):
    tl = start(lab_url, "honest_delivery")
    assert tl["session"] and tl["ticks"] and tl["device_id"].startswith("ava1.ring.device.LAB")
    body = claim(lab_url, tl, "ORD-1")
    assert (body["verdict"], body["reason"], body["witness"]) == ("delivered", "package_appeared", "lab-truth")
    jwks = requests.get(f"{lab_url}/lab/jwks.json", timeout=10).json()
    assert verify(body["receipt"], jwks)["order_id"] == "ORD-1"
    assert requests.get(f"{lab_url}/.well-known/jwks.json", timeout=10).json() == jwks
    for kind in ("before", "after"):
        frame = requests.get(f"{lab_url}/lab/s/{tl['session']}/evidence/{body['receipt_id']}/{kind}", timeout=10)
        assert frame.status_code == 200 and frame.content[:2] == b"\xff\xd8"


def test_two_visitors_at_once_do_not_see_each_others_door(lab_url):
    offline = start(lab_url, "camera_offline")
    honest = start(lab_url, "honest_delivery")
    fake = start(lab_url, "fake_tap")
    assert len({offline["device_id"], honest["device_id"], fake["device_id"]}) == 3
    # Interleaved claims: each house only sees its own device and its own offline webhooks.
    assert claim(lab_url, fake, "ORD-fake")["reason"] == "no_one_at_door"
    assert claim(lab_url, offline, "ORD-off")["reason"] == "camera_not_recording"
    assert claim(lab_url, honest, "ORD-honest")["verdict"] == "delivered"
    # One session's Ring token cannot read another session's device.
    r = requests.get(f"{lab_url}/v1/devices", headers={"Authorization": f"Bearer lab_{honest['session']}"}, timeout=10)
    assert [d["id"] for d in r.json()["data"]] == [honest["device_id"]]
    other = requests.get(f"{lab_url}/v1/devices/{fake['device_id']}/status",
                         headers={"Authorization": f"Bearer lab_{honest['session']}"}, timeout=10)
    assert other.status_code == 404


def test_frame_uploads_land_in_the_session(lab_url):
    tl = start(lab_url, "honest_delivery")
    tick = tl["start_ms"] + int(tl["ticks"][3] * 1000)
    fake = b"\xff\xd8\xff\xe0" + b"3d" * 50
    assert requests.post(f"{lab_url}/lab/s/{tl['session']}/frames/{tick}", data=fake, timeout=10).status_code == 204
    snap = requests.post(f"{lab_url}/v1/devices/{tl['device_id']}/media/image/download",
                         headers={"Authorization": f"Bearer lab_{tl['session']}"},
                         json={"type": "at_timestamp", "timestamp": tick}, timeout=10)
    assert snap.status_code == 200 and snap.content == fake  # requests followed the 303


def test_unknown_sessions_and_bad_tokens(lab_url):
    assert requests.post(f"{lab_url}/lab/s/nope/claim", json={"order_id": "x", "claimed_at": 1}, timeout=10).status_code == 404
    assert requests.get(f"{lab_url}/v1/devices", headers={"Authorization": "Bearer lab_nope"}, timeout=10).status_code == 401
    assert requests.post(f"{lab_url}/lab/start", json={"scenario": "nope"}, timeout=10).status_code == 400


def test_webhooks_need_the_lab_signature(lab_url):
    tl = start(lab_url, "fake_tap")
    r = requests.post(f"{lab_url}/lab/s/{tl['session']}/webhooks/ring", data=b"{}",
                      headers={"X-Signature": "sha256=00"}, timeout=10)
    assert r.status_code == 401


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="hs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="frames")
        yield table, s3


def test_dynamo_sessions_rebuild_the_same_world_and_keep_frames_in_s3(aws):
    table, s3 = aws
    sessions = DynamoSessions(table, s3, "frames")
    session = sessions.create("honest_delivery")
    world = sessions.world(session.sid)
    assert sessions.world_for_token(session.token).start_ms == world.start_ms
    tick = world.frame_times()[0]
    world.upload(tick, b"\xff\xd8frame")
    again = sessions.world(session.sid)  # a different "invocation"
    assert again.frame_near(tick) == (tick, b"\xff\xd8frame")
    assert sessions.get("missing") is None and sessions.get("../etc") is None


def test_the_daily_nova_budget_runs_out(aws):
    table, s3 = aws
    sessions = DynamoSessions(table, s3, "frames", daily_witness_runs=2)
    assert [sessions.take_witness_run() for _ in range(3)] == [True, True, False]


def test_an_offline_camera_goes_to_the_resident_and_comes_back_signed(agent_lab_url):
    assert requests.get(f"{agent_lab_url}/lab/config", timeout=10).json()["dispute_agent"] == \
        "Strands on AgentCore Runtime"
    tl = start(agent_lab_url, "camera_offline")
    receipt = claim(agent_lab_url, tl, "ORD-off")
    assert receipt["verdict"] == "inconclusive"
    q = ask(agent_lab_url, tl, receipt["claim_id"]).json()
    assert q["text"] == "Did your Lab Courier parcel for ORD-off arrive?" and q["asked_by"] == "fake-agent"
    assert ask(agent_lab_url, tl, receipt["claim_id"]).json()["question_id"] == q["question_id"]  # one per claim

    done = reply(agent_lab_url, tl, q["question_id"], "haan mil gaya, guard ke paas tha").json()
    assert done["status"] == "resolved" and done["outcome"] == "received" and done["quote"] == "haan mil gaya,"
    body = verify(done["resolution"], requests.get(f"{agent_lab_url}/lab/jwks.json", timeout=10).json())
    assert body["kind"] == "resolution" and body["resolves"] == receipt["receipt_id"]
    assert reply(agent_lab_url, tl, q["question_id"], "no").status_code == 409


def test_only_this_sessions_inconclusive_claims_go_to_the_resident(agent_lab_url):
    honest = start(agent_lab_url, "honest_delivery")
    delivered = claim(agent_lab_url, honest, "ORD-ok")
    assert ask(agent_lab_url, honest, delivered["claim_id"]).status_code == 409
    offline = start(agent_lab_url, "camera_offline")
    mine = claim(agent_lab_url, offline, "ORD-mine")
    assert ask(agent_lab_url, honest, mine["claim_id"]).status_code == 404  # another visitor's claim
    q = ask(agent_lab_url, offline, mine["claim_id"]).json()
    assert reply(agent_lab_url, honest, q["question_id"], "no").status_code == 404
    assert reply(agent_lab_url, offline, q["question_id"], "").status_code == 422


def test_without_an_agent_the_question_is_a_template_and_the_reply_waits(lab_url):
    tl = start(lab_url, "camera_offline")
    receipt = claim(lab_url, tl, "ORD-t")
    q = ask(lab_url, tl, receipt["claim_id"]).json()
    assert q["asked_by"] == "template" and q["text"].startswith("Did your Lab Courier parcel from lab-courier")
    waiting = reply(lab_url, tl, q["question_id"], "yes it came").json()
    assert waiting["status"] == "answered" and waiting["agent"] is None


def test_the_lab_names_the_day_in_the_visitors_timezone(lab_url):
    from countersign.dispute_agent import _day

    tl = start(lab_url, "camera_offline")
    receipt = claim(lab_url, tl, "ORD-tz")
    q = requests.post(f"{lab_url}/lab/s/{tl['session']}/claims/{receipt['claim_id']}/ask",
                      json={"timezone": "Pacific/Kiritimati"}, timeout=30).json()
    assert q["text"].endswith(f"arrive on {_day(tl['claim_at_ms'], 'Pacific/Kiritimati')}?")
