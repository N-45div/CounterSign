"""Decide what the camera can honestly say about a claimed delivery.

Three answers, each with a reason code:

* delivered: someone came to the door in the claim window, and a package is
  in the after-frame that was not in the before-frame.
* not_observed: the camera was recording the whole window and saw no one
  come to the door, or saw a visit that left nothing behind.
* inconclusive: anything the camera cannot settle, stated as such. A merchant
  should route these to a person, not treat them as a refusal.

The rules are deterministic. A vision model reports what is in the two
frames (see witness.py); it never decides the verdict.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from .episode import Observation

MIN_CONFIDENCE = 0.8


class Verdict(StrEnum):
    DELIVERED = "delivered"
    NOT_OBSERVED = "not_observed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class WitnessReport:
    """What a vision model saw in the before and after frames."""

    package_before: bool | None
    package_after: bool | None
    confidence: float
    model: str
    instruction_like_text: bool = False  # text in the frame addressed to a machine


@dataclass(frozen=True)
class Coverage:
    """Whether the camera was recording across the claim window."""

    complete: bool
    missing_ms: int = 0
    source: str = "clip"  # how we know: a clip request over the window, or status events


@dataclass(frozen=True)
class Evidence:
    kind: str  # snapshot_before, snapshot_after, clip
    at_ms: int
    sha256: str


def decide(obs: Observation, coverage: Coverage, witness: WitnessReport | None) -> tuple[Verdict, str]:
    if obs.best is None:
        if not coverage.complete:
            return Verdict.INCONCLUSIVE, "camera_not_recording"
        if obs.other_activity:
            return Verdict.INCONCLUSIVE, "unclassified_motion"
        return Verdict.NOT_OBSERVED, "no_one_at_door"

    if witness is None:
        return Verdict.INCONCLUSIVE, "frames_unavailable"
    if witness.instruction_like_text:
        # Never confirm on a frame that carries text aimed at the system reading it.
        return Verdict.INCONCLUSIVE, "instruction_text_in_frame"
    if witness.confidence < MIN_CONFIDENCE:
        return Verdict.INCONCLUSIVE, "low_confidence"
    if witness.package_after and witness.package_before:
        return Verdict.INCONCLUSIVE, "package_already_there"
    if witness.package_after and witness.package_before is False:
        return Verdict.DELIVERED, "package_appeared"
    if witness.package_after is False:
        return Verdict.NOT_OBSERVED, "visit_without_package"
    return Verdict.INCONCLUSIVE, "package_state_unknown"


def device_ref(device_id: str, house_salt: str) -> str:
    """A stable per-house pseudonym, so receipts never carry the Ring device id."""
    return hashlib.sha256(f"{house_salt}:{device_id}".encode()).hexdigest()[:16]


def receipt_body(
    obs: Observation,
    verdict: Verdict,
    reason: str,
    coverage: Coverage,
    witness: WitnessReport | None,
    evidence: list[Evidence],
    device: str,
) -> dict:
    """The signed part of a receipt: claim, verdict, and hashes. No frames, no model prose."""
    ep = obs.best
    return {
        "order_id": obs.claim.order_id,
        "merchant": obs.claim.merchant,
        "carrier": obs.claim.carrier,
        "claimed_at_ms": obs.claim.claimed_at_ms,
        "verdict": verdict.value,
        "reason": reason,
        "device": device,
        "window": {"start_ms": obs.window.start_ms, "end_ms": obs.window.end_ms},
        "coverage": {"complete": coverage.complete, "missing_ms": coverage.missing_ms, "source": coverage.source},
        "visit": None if ep is None else {
            "start_ms": ep.start_ms,
            "end_ms": ep.end_ms,
            "events": list(ep.event_ids),
            "vehicle_arrival": ep.arrival is not None,
            "vehicle_departure": ep.departure is not None,
        },
        "witness": None if witness is None else {
            "model": witness.model,
            "package_before": witness.package_before,
            "package_after": witness.package_after,
            "confidence": round(witness.confidence, 3),
        },
        "evidence": [{"kind": e.kind, "at_ms": e.at_ms, "sha256": e.sha256} for e in evidence],
    }
