import socket
import threading
import time

import pytest
import requests
import uvicorn

from handshake.lab.server import MERCHANT_KEY, create_lab
from handshake.receipt import verify


@pytest.fixture(scope="module")
def lab_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(create_lab(base, "truth"), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def claim(base, timeline, order="ORD-1"):
    return requests.post(f"{base}/house/v1/claims", headers={"Authorization": f"Bearer {MERCHANT_KEY}"},
                         json={"order_id": order, "claimed_at": timeline["claim_at_ms"]}, timeout=30)


def test_the_house_waits_for_a_scenario(lab_url):
    # Before any start the house slot answers 409 rather than a half-built house.
    r = requests.get(f"{lab_url}/house/healthz", timeout=10)
    assert r.status_code == 409 and r.json()["error"] == "not_playing"


def test_start_then_claim_gives_a_signed_receipt_and_evidence(lab_url):
    timeline = requests.post(f"{lab_url}/lab/start", json={"scenario": "honest_delivery"}, timeout=30).json()
    assert timeline["ticks"] and timeline["webhooks_sent"] == 0
    body = claim(lab_url, timeline).json()
    assert (body["verdict"], body["reason"]) == ("delivered", "package_appeared")
    jwks = requests.get(f"{lab_url}/house/.well-known/jwks.json", timeout=10).json()
    assert verify(body["receipt"], jwks)["order_id"] == "ORD-1"
    for kind in ("before", "after"):
        frame = requests.get(f"{lab_url}/lab/evidence/{body['receipt_id']}/{kind}", timeout=10)
        assert frame.status_code == 200 and frame.content[:2] == b"\xff\xd8"


def test_offline_webhooks_reach_the_house_and_runs_do_not_leak(lab_url):
    offline = requests.post(f"{lab_url}/lab/start", json={"scenario": "camera_offline"}, timeout=30).json()
    assert offline["webhooks_sent"] == 2
    assert claim(lab_url, offline, "ORD-off").json()["reason"] == "camera_not_recording"
    # The next run gets a fresh house: the earlier offline stretch must not count against it.
    fake = requests.post(f"{lab_url}/lab/start", json={"scenario": "fake_tap"}, timeout=30).json()
    assert claim(lab_url, fake, "ORD-fake").json()["reason"] == "no_one_at_door"


def test_key_set_is_stable_across_runs_and_config_names_the_witness(lab_url):
    first = requests.get(f"{lab_url}/house/.well-known/jwks.json", timeout=10).json()
    requests.post(f"{lab_url}/lab/start", json={"scenario": "already_there"}, timeout=30)
    second = requests.get(f"{lab_url}/house/.well-known/jwks.json", timeout=10).json()
    assert first == second
    config = requests.get(f"{lab_url}/lab/config", timeout=10).json()
    assert config["witness"] == "lab-truth" and len(config["scenarios"]) == 7


def test_unknown_scenarios_are_refused(lab_url):
    assert requests.post(f"{lab_url}/lab/start", json={"scenario": "nope"}, timeout=10).status_code == 400
