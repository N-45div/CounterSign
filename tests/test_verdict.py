import pytest

from countersign.episode import Claim, observe
from countersign.ring import HistoryEvent
from countersign.verdict import Coverage, Evidence, Verdict, WitnessReport, decide, device_ref, receipt_body

T0 = 1_790_000_000_000
FULL = Coverage(complete=True)
GAP = Coverage(complete=False, missing_ms=120_000)
CLAIM = Claim(order_id="ORD-7", merchant="shop", claimed_at_ms=T0 + 120_000, carrier="Blue Dart")


def ev(eid, start_s, end_s, *subtypes):
    return HistoryEvent(eid, "dev", "motion", T0 + start_s * 1000, T0 + end_s * 1000, frozenset(subtypes))


VISIT = [ev("van", 0, 30, "vehicle"), ev("courier", 10, 60, "human")]


def witness(before, after, confidence=0.95, injected=False):
    return WitnessReport(before, after, confidence, "amazon.nova-lite-v1:0", injected)


@pytest.mark.parametrize("w, expected", [
    (witness(False, True), (Verdict.DELIVERED, "package_appeared")),
    (witness(False, False), (Verdict.NOT_OBSERVED, "visit_without_package")),
    (witness(True, True), (Verdict.INCONCLUSIVE, "package_already_there")),
    (witness(False, True, confidence=0.6), (Verdict.INCONCLUSIVE, "low_confidence")),
    (witness(False, True, injected=True), (Verdict.INCONCLUSIVE, "instruction_text_in_frame")),
    (witness(None, None), (Verdict.INCONCLUSIVE, "package_state_unknown")),
    (None, (Verdict.INCONCLUSIVE, "frames_unavailable")),
])
def test_visit_verdicts(w, expected):
    assert decide(observe(CLAIM, VISIT), FULL, w) == expected


def test_no_one_came_and_the_camera_was_recording():
    assert decide(observe(CLAIM, []), FULL, None) == (Verdict.NOT_OBSERVED, "no_one_at_door")


def test_no_one_came_but_the_camera_was_offline_part_of_the_time():
    assert decide(observe(CLAIM, []), GAP, None) == (Verdict.INCONCLUSIVE, "camera_not_recording")


def test_motion_the_camera_could_not_classify_is_not_a_no():
    obs = observe(CLAIM, [ev("blur", 30, 35, "other_motion")])
    assert decide(obs, FULL, None) == (Verdict.INCONCLUSIVE, "unclassified_motion")


def test_receipt_body_carries_hashes_and_a_pseudonymous_device():
    obs = observe(CLAIM, VISIT)
    w = witness(False, True)
    verdict, reason = decide(obs, FULL, w)
    ev_after = Evidence("snapshot_after", obs.best.after_ms, "cd" * 32)
    body = receipt_body(obs, verdict, reason, FULL, w, [ev_after], device_ref("ava1.ring.device.FRONT", "salt"))
    assert body["verdict"] == "delivered" and body["reason"] == "package_appeared"
    assert body["visit"]["events"] == ["courier", "van"] and body["visit"]["vehicle_arrival"]
    assert body["evidence"] == [{"kind": "snapshot_after", "at_ms": obs.best.after_ms, "sha256": "cd" * 32}]
    assert "ava1.ring.device" not in str(body)
    assert len(body["device"]) == 16
    assert device_ref("ava1.ring.device.FRONT", "salt") != device_ref("ava1.ring.device.FRONT", "other-house")
