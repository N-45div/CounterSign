import pytest

from handshake.desk import MAX_ATTEMPTS, ClaimStatus, Desk, InvalidClaim, claim_id_for
from handshake.episode import LOOK_AFTER_CLAIM_MS
from handshake.house import SETTLE_MS, House, HouseConfig
from handshake.receipt import HouseKey, verify
from handshake.ring import HistoryEvent, Media, RingAuthError, RingError
from handshake.store import MemoryStore
from handshake.verdict import WitnessReport

T0 = 1_790_000_000_000
CONFIG = HouseConfig("h1", "https://handshake.example/h/h1", "dev", "salt")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeRing:
    def __init__(self, visit=True):
        self.visit = visit
        self.fail = None

    def motion_events(self, device_id, since_ms, until_ms=None):
        if self.fail:
            raise self.fail
        if not self.visit:
            return []
        return [HistoryEvent("h", "dev", "motion", T0 + 10_000, T0 + 50_000, frozenset({"human"}))]

    def online(self, device_id):
        return True

    def snapshot_at(self, device_id, at_ms, component_id=None):
        return Media(b"\xff\xd8" + (b"b" if at_ms < T0 + 10_000 else b"a"), "image/jpeg", at_ms, None, False)

    def snapshot_latest(self, device_id, start_ms, end_ms, component_id=None):
        raise RingError(404)


class Witness:
    def compare(self, before, after):
        return WitnessReport(False, True, 0.95, "us.amazon.nova-lite-v1:0")


class Recorder:
    def __init__(self):
        self.calls = []

    def schedule(self, claim_id, at_ms):
        self.calls.append((claim_id, at_ms))


def desk(ring, now):
    clock = Clock(now)
    store = MemoryStore()
    house = House(CONFIG, ring, Witness(), HouseKey.generate(), store, clock_ms=clock)
    sched = Recorder()
    return Desk(house, store, sched, clock_ms=clock), clock, sched, house


def test_a_fresh_claim_is_scheduled_for_when_ring_has_settled():
    d, clock, sched, _ = desk(FakeRing(), T0 + 60_000)
    record = d.submit("shop", "ORD-1", T0 + 60_000)
    assert record.status is ClaimStatus.PENDING
    assert record.retry_at_ms == T0 + 60_000 + SETTLE_MS
    assert sched.calls == [(record.claim_id, record.retry_at_ms)]


def test_the_scheduled_attempt_issues_a_receipt():
    d, clock, sched, house = desk(FakeRing(), T0 + 60_000)
    record = d.submit("shop", "ORD-1", T0 + 60_000)
    clock.now = record.retry_at_ms
    issued = d.attempt(record.claim_id)
    assert issued.status is ClaimStatus.ISSUED and issued.verdict == "delivered"
    assert verify(issued.token, house.jwks())["order_id"] == "ORD-1"


def test_polling_after_the_retry_time_moves_the_claim_on():
    d, clock, _, _ = desk(FakeRing(), T0 + 60_000)
    record = d.submit("shop", "ORD-1", T0 + 60_000)
    clock.now = record.retry_at_ms + 1
    assert d.poll(record.claim_id, "shop").status is ClaimStatus.ISSUED


def test_resubmitting_returns_the_same_claim():
    d, _, _, _ = desk(FakeRing(), T0 + 60_000)
    first = d.submit("shop", "ORD-1", T0 + 60_000)
    again = d.submit("shop", "ORD-1", T0 + 60_000)
    assert again.claim_id == first.claim_id
    assert first.claim_id == claim_id_for("shop", "ORD-1", T0 + 60_000)


def test_a_merchant_cannot_read_another_merchants_claim():
    d, _, _, _ = desk(FakeRing(), T0 + 60_000)
    record = d.submit("shop", "ORD-1", T0 + 60_000)
    assert d.poll(record.claim_id, "other-shop") is None


def test_no_visit_waits_for_the_window_then_says_not_observed():
    d, clock, _, _ = desk(FakeRing(visit=False), T0 + 60_000)
    record = d.submit("shop", "ORD-2", T0 + 60_000)
    clock.now = record.retry_at_ms
    waiting = d.attempt(record.claim_id)
    assert waiting.status is ClaimStatus.PENDING
    assert waiting.retry_at_ms == T0 + 60_000 + LOOK_AFTER_CLAIM_MS + SETTLE_MS
    clock.now = waiting.retry_at_ms
    assert d.attempt(record.claim_id).verdict == "not_observed"


def test_an_expired_ring_token_keeps_the_claim_pending_with_a_reason():
    ring = FakeRing()
    d, clock, _, _ = desk(ring, T0 + 60_000)
    record = d.submit("shop", "ORD-3", T0 + 60_000)
    ring.fail = RingAuthError(401, "Unauthorized")
    clock.now = record.retry_at_ms
    waiting = d.attempt(record.claim_id)
    assert waiting.status is ClaimStatus.PENDING and waiting.last_error == "ring_token_expired"


def test_repeated_ring_failures_give_up_after_max_attempts():
    ring = FakeRing()
    d, clock, _, _ = desk(ring, T0 + 60_000)
    record = d.submit("shop", "ORD-4", T0 + 60_000)
    ring.fail = RingError(503, "Service Unavailable")
    for _ in range(MAX_ATTEMPTS):
        clock.now = (d.store.get_claim(record.claim_id).retry_at_ms or clock.now) + 1
        record = d.attempt(record.claim_id)
    assert record.status is ClaimStatus.FAILED and record.last_error == "ring_error_503"


@pytest.mark.parametrize("order_id, claimed_at, message", [
    ("", T0, "order_id"),
    ("x" * 129, T0, "order_id"),
    ("ORD", T0 + 3_600_000, "future"),
    ("ORD", T0 - 31 * 86_400_000, "30 days"),
])
def test_bad_claims_are_refused(order_id, claimed_at, message):
    d, _, _, _ = desk(FakeRing(), T0)
    with pytest.raises(InvalidClaim, match=message):
        d.submit("shop", order_id, claimed_at)


def test_on_issued_runs_once_and_cannot_undo_the_receipt():
    d, clock, _, _ = desk(FakeRing(), T0 + 60_000)
    seen = []

    def hook(record):
        seen.append(record.verdict)
        raise ConnectionError("queue down")

    d.on_issued = hook
    record = d.submit("shop", "ORD-5", T0 + 60_000)
    clock.now = record.retry_at_ms
    issued = d.attempt(record.claim_id)
    assert issued.status is ClaimStatus.ISSUED and d.store.get_claim(record.claim_id).token == issued.token
    d.attempt(record.claim_id)
    assert seen == ["delivered"]
