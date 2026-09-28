"""The house: turns a delivery claim into a signed receipt.

    claim -> Ring motion history -> visit -> before/after frames -> witness
          -> deterministic verdict -> signed receipt (frames stay here)

Two timing rules keep answers honest:

* Ring needs a moment to file events and make frames downloadable, so a
  claim is not checked until SETTLE_MS after it was made.
* Until the claim window has closed, only "delivered" is final: a courier who
  tapped early may still be walking up. Anything else asks the caller to come
  back when the window closes (NotReady.retry_at_ms).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from .episode import MINUTE, Claim, Observation, claim_window, observe
from .receipt import HouseKey, issue, sha256_hex
from .ring import HistoryEvent, Media, RingError
from .store import MemoryStore, StoredReceipt
from .verdict import Evidence, Verdict, WitnessReport, decide, device_ref, receipt_body
from .witness import Frame, WitnessError

SETTLE_MS = 2 * MINUTE
HISTORY_PAD_MS = 15 * MINUTE  # read past the window so neighbouring visits are known
BEFORE_LOOKBACK_MS = 30 * MINUTE  # fallback: latest frame in this span before the visit


class NotReady(Exception):
    def __init__(self, retry_at_ms: int, why: str):
        self.retry_at_ms = retry_at_ms
        self.why = why
        super().__init__(f"not ready until {retry_at_ms}: {why}")


class RingReader(Protocol):
    def motion_events(self, device_id: str, since_ms: int, until_ms: int | None = None) -> list[HistoryEvent]: ...
    def online(self, device_id: str) -> bool | None: ...
    def snapshot_at(self, device_id: str, at_ms: int, component_id: str | None = None) -> Media: ...
    def snapshot_latest(self, device_id: str, start_ms: int, end_ms: int, component_id: str | None = None) -> Media: ...


class Witness(Protocol):
    def compare(self, before: Frame, after: Frame) -> WitnessReport: ...


@dataclass(frozen=True)
class HouseConfig:
    house_id: str
    issuer: str  # the URL merchants fetch the house's JWK set from
    device_id: str  # the Ring camera that faces the drop point
    salt: str  # per-house secret for the device pseudonym


@dataclass(frozen=True)
class IssuedReceipt:
    receipt_id: str
    token: str
    verdict: Verdict
    reason: str
    body: dict
    witness: WitnessReport | None  # house-side detail, not part of the signed receipt


class House:
    def __init__(
        self,
        config: HouseConfig,
        ring: RingReader,
        witness: Witness | None,
        key: HouseKey,
        store: MemoryStore,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self.config = config
        self.ring = ring
        self.witness = witness
        self.key = key
        self.store = store
        self._now = clock_ms
        self._new_id = new_id

    def jwks(self) -> dict:
        return {"keys": [self.key.public_jwk()]}

    def verify(self, claim: Claim) -> IssuedReceipt:
        now = self._now()
        window = claim_window(claim)
        if now < claim.claimed_at_ms + SETTLE_MS:
            raise NotReady(claim.claimed_at_ms + SETTLE_MS, "Ring is still filing events for this moment")

        device = self.config.device_id
        events = self.ring.motion_events(device, window.start_ms - HISTORY_PAD_MS, min(now, window.end_ms + HISTORY_PAD_MS))
        obs = observe(claim, events)
        coverage = self.store.coverage(device, window, self._online())
        report, evidence, frames = self._witness(obs)
        verdict, reason = decide(obs, coverage, report)

        closes_at = window.end_ms + SETTLE_MS
        if now < closes_at and verdict is not Verdict.DELIVERED:
            raise NotReady(closes_at, "the claim window is still open")

        body = receipt_body(obs, verdict, reason, coverage, report, evidence, device_ref(device, self.config.salt))
        receipt_id = self._new_id()
        issued_at_s = now // 1000
        token = issue(self.key, body, issuer=self.config.issuer, issued_at_s=issued_at_s, receipt_id=receipt_id)
        self.store.put_receipt(StoredReceipt(receipt_id, claim.order_id, claim.merchant, verdict.value, reason, token, issued_at_s))
        if frames:
            self.store.put_frames(receipt_id, frames)
        return IssuedReceipt(receipt_id, token, verdict, reason, body, report)

    def _online(self) -> bool | None:
        try:
            return self.ring.online(self.config.device_id)
        except RingError:
            return None

    def _frame(self, at_ms: int, fallback_start_ms: int, fallback_end_ms: int) -> Media:
        """The frame nearest a moment, else the latest one recorded in a fallback span."""
        try:
            return self.ring.snapshot_at(self.config.device_id, at_ms)
        except RingError:
            return self.ring.snapshot_latest(self.config.device_id, fallback_start_ms, fallback_end_ms)

    def _witness(self, obs: Observation) -> tuple[WitnessReport | None, list[Evidence], dict[str, bytes]]:
        ep = obs.best
        if ep is None or self.witness is None:
            return None, [], {}
        try:
            before = self._frame(ep.before_ms, ep.before_ms - BEFORE_LOOKBACK_MS, ep.before_ms)
            after = self._frame(ep.after_ms, ep.end_ms, ep.after_ms)
        except RingError:
            return None, [], {}
        evidence = [
            Evidence("snapshot_before", before.timestamp_ms or ep.before_ms, sha256_hex(before.content)),
            Evidence("snapshot_after", after.timestamp_ms or ep.after_ms, sha256_hex(after.content)),
        ]
        frames = {"before": before.content, "after": after.content}
        try:
            report = self.witness.compare(Frame(before.content, evidence[0].at_ms), Frame(after.content, evidence[1].at_ms))
        except WitnessError:
            report = None
        return report, evidence, frames
