"""The claims desk: accepts delivery claims from merchants and sees each one
through to a signed receipt.

A claim usually cannot be answered on arrival. Ring needs a couple of
minutes to file events, and anything other than "delivered" waits for the
claim window to close. So the desk records the claim, tries once, and if
the house says "not ready" it schedules another attempt for the moment the
house named. A scheduler (SQS when deployed) brings the claim back; a merchant
polling for it also nudges it along if an attempt is due.

When a receipt is issued the desk calls on_issued once, which is how an
inconclusive claim reaches the resident (disputes.py).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable, Protocol

from .episode import MINUTE, Claim
from .house import House, NotReady
from .ring import RingAuthError, RingError

MAX_ATTEMPTS = 12
TOKEN_RETRY_MS = 5 * MINUTE  # waiting for the resident to refresh the Ring token
ERROR_RETRY_MS = 2 * MINUTE
FUTURE_SLACK_MS = 5 * MINUTE  # clocks drift; claims a little in the future are fine
MAX_CLAIM_AGE_MS = 30 * 24 * 60 * MINUTE

log = logging.getLogger(__name__)


class ClaimStatus(StrEnum):
    PENDING = "pending"
    ISSUED = "issued"
    FAILED = "failed"


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    merchant: str
    order_id: str
    claimed_at_ms: int
    created_at_ms: int
    carrier: str | None = None
    status: ClaimStatus = ClaimStatus.PENDING
    retry_at_ms: int | None = None
    attempts: int = 0
    receipt_id: str | None = None
    verdict: str | None = None
    reason: str | None = None
    token: str | None = None
    last_error: str | None = None

    def as_claim(self) -> Claim:
        return Claim(self.order_id, self.merchant, self.claimed_at_ms, self.carrier)


class InvalidClaim(ValueError):
    pass


class ClaimStore(Protocol):
    def put_claim(self, record: ClaimRecord) -> None: ...
    def get_claim(self, claim_id: str) -> ClaimRecord | None: ...


class Scheduler(Protocol):
    def schedule(self, claim_id: str, at_ms: int) -> None: ...


class PollingScheduler:
    """No background worker: attempts happen when a merchant polls and one is due."""

    def schedule(self, claim_id: str, at_ms: int) -> None:
        return None


def claim_id_for(merchant: str, order_id: str, claimed_at_ms: int) -> str:
    """Same merchant, order and claimed time: same claim. Resubmitting is safe."""
    return "clm_" + hashlib.sha256(f"{merchant}\x1f{order_id}\x1f{claimed_at_ms}".encode()).hexdigest()[:24]


class Desk:
    def __init__(
        self,
        house: House,
        store: ClaimStore,
        scheduler: Scheduler | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        on_issued: Callable[[ClaimRecord], None] | None = None,
    ):
        self.house = house
        self.on_issued = on_issued
        self.store = store
        self.scheduler = scheduler or PollingScheduler()
        self._now = clock_ms

    def submit(self, merchant: str, order_id: str, claimed_at_ms: int, carrier: str | None = None) -> ClaimRecord:
        now = self._now()
        order_id = (order_id or "").strip()
        if not order_id or len(order_id) > 128:
            raise InvalidClaim("order_id must be 1 to 128 characters")
        if claimed_at_ms > now + FUTURE_SLACK_MS:
            raise InvalidClaim("claimed_at is in the future")
        if claimed_at_ms < now - MAX_CLAIM_AGE_MS:
            raise InvalidClaim("claimed_at is more than 30 days ago")

        claim_id = claim_id_for(merchant, order_id, claimed_at_ms)
        existing = self.store.get_claim(claim_id)
        if existing is not None:
            return self.poll(claim_id, merchant) or existing
        record = ClaimRecord(claim_id, merchant, order_id, claimed_at_ms, now, carrier=carrier)
        self.store.put_claim(record)
        return self.attempt(claim_id)

    def poll(self, claim_id: str, merchant: str) -> ClaimRecord | None:
        """A merchant's view of its claim; runs an attempt if one is overdue."""
        record = self.store.get_claim(claim_id)
        if record is None or record.merchant != merchant:
            return None
        if record.status is ClaimStatus.PENDING and (record.retry_at_ms or 0) <= self._now():
            return self.attempt(claim_id)
        return record

    def attempt(self, claim_id: str) -> ClaimRecord:
        record = self.store.get_claim(claim_id)
        if record is None:
            raise KeyError(claim_id)
        if record.status is not ClaimStatus.PENDING:
            return record

        now = self._now()
        record = replace(record, attempts=record.attempts + 1)
        try:
            issued = self.house.verify(record.as_claim())
        except NotReady as wait:
            return self._later(record, wait.retry_at_ms, None)
        except RingAuthError:
            return self._later(record, now + TOKEN_RETRY_MS, "ring_token_expired")
        except RingError as exc:
            return self._later(record, now + ERROR_RETRY_MS, f"ring_error_{exc.status}")

        record = replace(
            record, status=ClaimStatus.ISSUED, retry_at_ms=None, last_error=None,
            receipt_id=issued.receipt_id, verdict=issued.verdict.value, reason=issued.reason, token=issued.token,
        )
        self.store.put_claim(record)
        if self.on_issued is not None:
            try:
                self.on_issued(record)
            except Exception:  # a follow-up must never undo a signed receipt
                log.exception("on_issued failed for %s", claim_id)
        return record

    def _later(self, record: ClaimRecord, retry_at_ms: int, error: str | None) -> ClaimRecord:
        if error and record.attempts >= MAX_ATTEMPTS:
            record = replace(record, status=ClaimStatus.FAILED, retry_at_ms=None, last_error=error)
            self.store.put_claim(record)
            return record
        record = replace(record, retry_at_ms=retry_at_ms, last_error=error)
        self.store.put_claim(record)
        self.scheduler.schedule(record.claim_id, retry_at_ms)
        return record
