from handshake.episode import SNAPSHOT_MARGIN_MS, Claim, observe
from handshake.ring import HistoryEvent

T0 = 1_790_000_000_000  # an arbitrary 10:00:00
S = 1_000
M = 60 * S


def ev(eid: str, start_s: float, end_s: float, *subtypes: str) -> HistoryEvent:
    return HistoryEvent(eid, "dev", "motion", int(T0 + start_s * S), int(T0 + end_s * S), frozenset(subtypes))


def claim_at(seconds: float) -> Claim:
    return Claim(order_id="ORD-1", merchant="shop", claimed_at_ms=int(T0 + seconds * S))


def test_van_courier_walk_up_and_back_is_one_episode():
    events = [
        ev("van-in", 0, 40, "vehicle"),
        ev("walk-up", 20, 70, "human"),
        ev("walk-back", 90, 110, "human"),  # 20 s after the first human event: same visit
        ev("van-out", 100, 130, "vehicle"),
    ]
    obs = observe(claim_at(150), events)
    ep = obs.best
    assert len(obs.episodes) == 1
    assert [e.id for e in ep.visit] == ["walk-up", "walk-back"]
    assert ep.arrival.id == "van-in" and ep.departure.id == "van-out"
    assert ep.before_ms == T0 - SNAPSHOT_MARGIN_MS  # before the van, not just before the person
    assert ep.after_ms == T0 + 110 * S + SNAPSHOT_MARGIN_MS
    assert ep.event_ids == ("walk-up", "walk-back", "van-in", "van-out")
    assert obs.other_activity == ()


def test_scooter_or_foot_delivery_needs_no_vehicle():
    obs = observe(claim_at(120), [ev("walk-up", 0, 45, "human")])
    assert obs.best.arrival is None and obs.best.departure is None
    assert obs.best.before_ms == T0 - SNAPSHOT_MARGIN_MS


def test_after_snapshot_comes_before_the_resident_picks_the_parcel_up():
    events = [ev("courier", 0, 40, "human"), ev("resident", 40 + 100, 160, "human")]
    obs = observe(claim_at(60), events)
    courier = obs.best
    assert [e.id for e in courier.visit] == ["courier"]
    assert courier.after_ms < T0 + 140 * S  # strictly before the resident arrives


def test_a_van_parked_through_the_visit_counts_once():
    events = [ev("van", 0, 200, "vehicle"), ev("courier", 30, 90, "human")]
    ep = observe(claim_at(100), events).best
    assert ep.arrival.id == ep.departure.id == "van"
    assert ep.event_ids == ("courier", "van")


def test_prefers_the_visit_nearest_the_claim():
    events = [ev("neighbour", -15 * 60, -15 * 60 + 30, "human"), ev("courier", 2 * 60, 2 * 60 + 40, "human")]
    obs = observe(claim_at(0), events)
    assert [e.id for e in obs.best.visit] == ["courier"]  # 2 min after the tap beats 15 min before
    assert len(obs.episodes) == 2


def test_visits_outside_the_window_are_ignored():
    events = [ev("yesterday", -24 * 3600, -24 * 3600 + 30, "human"), ev("later", 45 * 60, 45 * 60 + 20, "human")]
    obs = observe(claim_at(0), events)
    assert obs.episodes == () and obs.best is None


def test_unclassified_motion_is_kept_as_other_activity():
    events = [ev("van", -60, -20, "vehicle"), ev("blur", -30, -25, "other_motion")]
    obs = observe(claim_at(0), events)
    assert obs.best is None
    assert {e.id for e in obs.other_activity} == {"van", "blur"}
