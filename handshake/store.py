"""Where a house keeps its receipts, its private evidence frames, and the
camera's online history.

The in-memory store backs tests and the local demo; a DynamoDB and S3 store
implements the same methods for the deployed house.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .episode import Window
from .verdict import Coverage


@dataclass(frozen=True)
class StoredReceipt:
    receipt_id: str
    order_id: str
    merchant: str
    verdict: str
    reason: str
    token: str  # the signed JWS
    issued_at_s: int


@dataclass
class MemoryStore:
    receipts: dict[str, StoredReceipt] = field(default_factory=dict)
    frames: dict[str, dict[str, bytes]] = field(default_factory=dict)
    # device id -> [(at_ms, online)] in arrival order
    status_log: dict[str, list[tuple[int, bool]]] = field(default_factory=dict)
    claims: dict[str, object] = field(default_factory=dict)
    seen_webhooks: set[str] = field(default_factory=set)

    # -- claims --------------------------------------------------------------

    def put_claim(self, record) -> None:
        self.claims[record.claim_id] = record

    def get_claim(self, claim_id: str):
        return self.claims.get(claim_id)

    def first_sighting(self, request_id: str) -> bool:
        """True the first time a webhook request id is seen (Ring retries deliveries)."""
        if request_id in self.seen_webhooks:
            return False
        self.seen_webhooks.add(request_id)
        return True

    # -- receipts ------------------------------------------------------------

    def put_receipt(self, receipt: StoredReceipt) -> None:
        self.receipts[receipt.receipt_id] = receipt

    def get_receipt(self, receipt_id: str) -> StoredReceipt | None:
        return self.receipts.get(receipt_id)

    def receipts_for_order(self, merchant: str, order_id: str) -> list[StoredReceipt]:
        found = [r for r in self.receipts.values() if r.merchant == merchant and r.order_id == order_id]
        return sorted(found, key=lambda r: r.issued_at_s)

    def recent_receipts(self, limit: int = 50) -> list[StoredReceipt]:
        return sorted(self.receipts.values(), key=lambda r: r.issued_at_s, reverse=True)[:limit]

    # -- frames (never leave the house) ---------------------------------------

    def put_frames(self, receipt_id: str, frames: dict[str, bytes]) -> None:
        self.frames[receipt_id] = dict(frames)

    def get_frames(self, receipt_id: str) -> dict[str, bytes]:
        return self.frames.get(receipt_id, {})

    # -- camera online history -----------------------------------------------

    def record_status(self, device_id: str, at_ms: int, online: bool) -> None:
        self.status_log.setdefault(device_id, []).append((at_ms, online))

    def coverage(self, device_id: str, window: Window, online_now: bool | None) -> Coverage:
        return coverage_from_log(self.status_log.get(device_id, []), window, online_now)


def coverage_from_log(log: list[tuple[int, bool]], window: Window, online_now: bool | None) -> Coverage:
    """How much of the window the camera was online.

    With device_online/device_offline webhooks on record, offline stretches are
    measured. Without any (for example a Playground token, which gets no
    webhooks), the answer falls back to the device's status right now and says
    so in `source`, so a merchant can weigh it.
    """
    events = sorted(log)
    if not events:
        if online_now is None:
            return Coverage(complete=False, missing_ms=window.end_ms - window.start_ms, source="unknown")
        return Coverage(complete=online_now, missing_ms=0 if online_now else window.end_ms - window.start_ms,
                        source="status_now")

    # State at the window start is the last event before it; assume online if none.
    state = True
    for at, online in events:
        if at <= window.start_ms:
            state = online
    missing, cursor = 0, window.start_ms
    for at, online in events:
        if at <= window.start_ms:
            continue
        if at >= window.end_ms:
            break
        if not state:
            missing += at - cursor
        state, cursor = online, at
    if not state:
        missing += window.end_ms - cursor
    return Coverage(complete=missing == 0, missing_ms=missing, source="webhooks")
