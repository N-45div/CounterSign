"""The lab emulator behind a real HTTP server, driven by Countersign's unchanged Ring client."""

import hashlib
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from countersign.api import create_app, hash_key
from countersign.desk import Desk
from countersign.house import House, HouseConfig
from countersign.lab.emulator import LabState, create_emulator, send_status_webhooks
from countersign.lab.scenarios import SCENARIOS
from countersign.receipt import HouseKey, verify
from countersign.ring import RingAuthError, RingClient, RingError
from countersign.store import MemoryStore
from countersign.verdict import WitnessReport

TOKEN = "lab-token"
WEBHOOK_KEY = "lab-hmac"


@pytest.fixture(scope="module")
def lab():
    state = LabState()
    app = create_emulator(state, tokens={TOKEN})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield state, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def ring(base_url, token=TOKEN):
    return RingClient(token, base_url=base_url)


class TruthWitness:
    """Reports what the lab actually drew in each frame (vision is tested separately)."""

    def __init__(self, state):
        self.state = state

    def compare(self, before, after):
        scenes = self.state.world.scenes_by_hash
        b = scenes[hashlib.sha256(before.image).hexdigest()]
        a = scenes[hashlib.sha256(after.image).hexdigest()]
        notes = tuple(a.note) + tuple(b.note)
        return WitnessReport(b.parcel, a.parcel, 0.95, "lab-truth", bool(notes), notes)


def test_a_bad_token_gets_ringss_empty_401(lab):
    state, url = lab
    state.play("honest_delivery")
    with pytest.raises(RingAuthError):
        ring(url, "wrong").devices()


def test_devices_status_and_tagged_motion_history(lab):
    state, url = lab
    world = state.play("honest_delivery")
    client = ring(url)
    [device] = client.devices()
    assert device.id == world.device.id and device.name == "Lab Doorbell"
    assert client.online(device.id) is True
    events = client.motion_events(device.id, world.start_ms)
    assert [sorted(e.subtypes) for e in events] == [["vehicle"], ["human"], ["vehicle"]]
    assert events[1].start_ms == world.at(80)


def test_media_comes_through_a_303_and_misses_are_416(lab):
    state, url = lab
    world = state.play("honest_delivery")
    client = ring(url)
    frame = client.snapshot_at(world.device.id, world.at(90))
    assert frame.content[:2] == b"\xff\xd8" and frame.timestamp_ms == world.at(90)
    with pytest.raises(RingError) as missing:
        client.snapshot_at(world.device.id, world.at(30))  # between snapshots, no motion
    assert missing.value.status == 416
    latest = client.snapshot_latest(world.device.id, world.at(-1), world.at(30))
    assert latest.timestamp_ms == world.at(0)  # the periodic snapshot
    with pytest.raises(RingError):
        client.clip(world.device.id, world.at(80), 5_000)  # the lab records snapshots only


def test_history_pages_follow_next_links(lab):
    state, url = lab
    world = state.play("honest_delivery")
    from countersign.lab import emulator
    original = emulator.PAGE_SIZE
    emulator.PAGE_SIZE = 1
    try:
        events = ring(url).history(world.device.id)
    finally:
        emulator.PAGE_SIZE = original
    assert len(events) == 3 and events[0].start_ms > events[-1].start_ms


@pytest.mark.parametrize("name", [n for n, s in SCENARIOS.items()])
def test_every_scenario_reaches_its_expected_verdict(lab, name):
    state, url = lab
    world = state.play(name)
    store = MemoryStore()
    house = House(HouseConfig("lab-house", "https://lab.example", "", "salt"), ring(url), TruthWitness(state),
                  HouseKey.generate(), store, device_resolver=lambda: ring(url).devices()[0].id)
    desk = Desk(house, store)
    api = TestClient(create_app(desk, house, store, {"shop": hash_key("k")}, WEBHOOK_KEY))
    send_status_webhooks(world, "/webhooks/ring", WEBHOOK_KEY,
                         post=lambda u, data, headers, timeout: api.post(u, content=data, headers=headers))

    record = desk.submit("shop", f"ORD-{name}", world.at(world.scenario.claim_at_s))
    assert (record.verdict, record.reason) == world.scenario.expected
    payload = verify(record.token, house.jwks())
    assert payload["verdict"] == world.scenario.expected[0]
    if payload["evidence"]:
        assert len(store.get_frames(record.receipt_id)) == 2


def test_timeline_matches_what_the_camera_records(lab):
    state, url = lab
    world = state.play("honest_delivery")
    import requests
    timeline = requests.get(f"{url}/lab/timeline", timeout=10).json()
    assert timeline["claim_at_ms"] == world.at(150) and timeline["expected"]["verdict"] == "delivered"
    assert timeline["spans"]["parcels"] == [[112, None]]  # open-ended spans come back as null
    assert [world.at(t) for t in timeline["ticks"]] == world.frame_times()


def test_an_uploaded_3d_frame_is_the_snapshot_ring_serves(lab):
    state, url = lab
    world = state.play("honest_delivery")
    import requests
    tick = world.at(90)
    fake_render = b"\xff\xd8\xff\xe0" + b"3d-frame" * 10
    assert requests.post(f"{url}/lab/frames/{tick}", data=fake_render, timeout=10).status_code == 204
    assert ring(url).snapshot_at(world.device.id, tick).content == fake_render


def test_uploads_are_checked(lab):
    state, url = lab
    world = state.play("honest_delivery")
    import requests
    not_a_tick = world.at(30) + 500  # no motion and no snapshot at this moment
    assert requests.post(f"{url}/lab/frames/{not_a_tick}", data=b"\xff\xd8x", timeout=10).status_code == 400
    assert requests.post(f"{url}/lab/frames/{world.at(90)}", data=b"<svg/>", timeout=10).status_code == 415
    big = b"\xff\xd8" + b"0" * 1_600_000
    assert requests.post(f"{url}/lab/frames/{world.at(90)}", data=big, timeout=10).status_code == 413
