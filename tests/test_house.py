import hashlib

import pytest

from handshake.episode import LOOK_AFTER_CLAIM_MS, Claim
from handshake.house import SETTLE_MS, House, HouseConfig, NotReady
from handshake.receipt import HouseKey, verify
from handshake.ring import HistoryEvent, Media, RingError
from handshake.store import MemoryStore
from handshake.verdict import WitnessReport
from handshake.witness import WitnessError

T0 = 1_790_000_000_000
JPEG_BEFORE = b"\xff\xd8before"
JPEG_AFTER = b"\xff\xd8after"
CONFIG = HouseConfig("h1", "https://handshake.example/h/h1", "ava1.ring.device.FRONT", "salt")
CLAIM = Claim("ORD-9", "shop", T0 + 120_000, carrier="Delhivery")
AFTER_WINDOW = T0 + 120_000 + LOOK_AFTER_CLAIM_MS + SETTLE_MS


class FakeRing:
    def __init__(self, events, online=True, snapshot_at_fails=False, latest_fails=False):
        self.events, self._online = events, online
        self.snapshot_at_fails, self.latest_fails = snapshot_at_fails, latest_fails
        self.calls = []

    def motion_events(self, device_id, since_ms, until_ms=None):
        return [e for e in self.events if e.end_ms >= since_ms and (until_ms is None or e.start_ms <= until_ms)]

    def online(self, device_id):
        return self._online

    def snapshot_at(self, device_id, at_ms, component_id=None):
        self.calls.append(("at", at_ms))
        if self.snapshot_at_fails:
            raise RingError(404, "Not Found")
        return Media(JPEG_BEFORE if at_ms < T0 + 40_000 else JPEG_AFTER, "image/jpeg", at_ms, None, False)

    def snapshot_latest(self, device_id, start_ms, end_ms, component_id=None):
        self.calls.append(("latest", start_ms, end_ms))
        if self.latest_fails:
            raise RingError(404, "Not Found")
        return Media(JPEG_BEFORE if end_ms <= T0 + 40_000 else JPEG_AFTER, "image/jpeg", end_ms - 1, None, False)


class FakeWitness:
    def __init__(self, report=None, error=False):
        self.report = report or WitnessReport(False, True, 0.93, "us.amazon.nova-lite-v1:0")
        self.error = error
        self.seen = []

    def compare(self, before, after):
        self.seen.append((before.image, after.image))
        if self.error:
            raise WitnessError("throttled")
        return self.report


def visit():
    return [HistoryEvent("courier", CONFIG.device_id, "motion", T0 + 40_000, T0 + 90_000, frozenset({"human"}))]


def house(ring, witness=None, now=AFTER_WINDOW, store=None):
    key = HouseKey.generate()
    return House(CONFIG, ring, witness, key, store or MemoryStore(), clock_ms=lambda: now, new_id=lambda: "r-1")


def test_too_soon_after_the_claim_is_not_ready():
    with pytest.raises(NotReady) as err:
        house(FakeRing(visit()), FakeWitness(), now=CLAIM.claimed_at_ms + 10_000).verify(CLAIM)
    assert err.value.retry_at_ms == CLAIM.claimed_at_ms + SETTLE_MS


def test_delivered_is_issued_while_the_window_is_still_open():
    store = MemoryStore()
    h = house(FakeRing(visit()), FakeWitness(), now=CLAIM.claimed_at_ms + SETTLE_MS, store=store)
    issued = h.verify(CLAIM)
    payload = verify(issued.token, h.jwks())
    assert payload["verdict"] == "delivered" and payload["order_id"] == "ORD-9"
    hashes = {e["kind"]: e["sha256"] for e in payload["evidence"]}
    assert hashes["snapshot_after"] == hashlib.sha256(JPEG_AFTER).hexdigest()
    assert store.get_frames("r-1") == {"before": JPEG_BEFORE, "after": JPEG_AFTER}
    assert "ava1.ring.device" not in issued.token and JPEG_AFTER.hex() not in issued.token


def test_anything_but_delivered_waits_for_the_window_to_close():
    h = house(FakeRing([]), FakeWitness(), now=CLAIM.claimed_at_ms + SETTLE_MS)
    with pytest.raises(NotReady) as err:
        h.verify(CLAIM)
    assert err.value.retry_at_ms == AFTER_WINDOW


def test_no_one_came_after_the_window_closed():
    issued = house(FakeRing([]), FakeWitness()).verify(CLAIM)
    assert (issued.verdict.value, issued.reason) == ("not_observed", "no_one_at_door")
    assert issued.body["coverage"]["source"] == "status_now"


def test_falls_back_to_the_latest_frame_when_there_is_none_at_the_moment():
    ring = FakeRing(visit(), snapshot_at_fails=True)
    issued = house(ring, FakeWitness()).verify(CLAIM)
    assert issued.verdict.value == "delivered"
    assert [c[0] for c in ring.calls] == ["at", "latest", "at", "latest"]


def test_no_frames_at_all_is_inconclusive():
    issued = house(FakeRing(visit(), snapshot_at_fails=True, latest_fails=True), FakeWitness()).verify(CLAIM)
    assert (issued.verdict.value, issued.reason) == ("inconclusive", "frames_unavailable")
    assert issued.body["evidence"] == []


def test_witness_failure_is_inconclusive_not_a_crash():
    issued = house(FakeRing(visit()), FakeWitness(error=True)).verify(CLAIM)
    assert (issued.verdict.value, issued.reason) == ("inconclusive", "frames_unavailable")


def test_offline_camera_with_no_visit_is_inconclusive():
    issued = house(FakeRing([], online=False), FakeWitness()).verify(CLAIM)
    assert (issued.verdict.value, issued.reason) == ("inconclusive", "camera_not_recording")
