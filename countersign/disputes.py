"""Settling inconclusive claims with the resident's own word.

When the camera cannot settle a claim (offline, a parcel already there, text
in the frame), Countersign asks the resident one plain question and turns the
answer into a second signed receipt, a resolution, that points back at the
first. The dispute agent (dispute_agent.py) words the question and reads the
resident's free-text reply; everything that gets signed is decided here, in
code:

* An interpretation is accepted only if its quote appears word for word in
  what the resident wrote, so the model cannot put words in their mouth.
* The resolution carries a hash of the verbatim reply, not the reply itself.
* One question per claim, one resolution per question.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable

from .receipt import HouseKey, issue, sha256_hex

MAX_QUESTION_CHARS = 280


class Outcome(StrEnum):
    RECEIVED = "received"
    NOT_RECEIVED = "not_received"
    UNSURE = "unsure"


@dataclass(frozen=True)
class Question:
    question_id: str
    claim_id: str
    receipt_id: str
    order_id: str
    merchant: str
    text: str
    asked_at_ms: int
    status: str = "open"  # open, answered, resolved
    reply: str | None = None
    answered_at_ms: int | None = None
    outcome: str | None = None
    quote: str | None = None
    note: str | None = None
    interpreted_by: str | None = None
    resolution_id: str | None = None
    resolution: str | None = None  # the signed JWS


class DisputeError(ValueError):
    pass


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


class Disputes:
    def __init__(self, store, key: HouseKey, issuer: str,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex):
        self.store = store
        self.key = key
        self.issuer = issuer
        self._now = clock_ms
        self._new_id = new_id

    def open(self, claim, text: str) -> Question:
        """Record the question the resident will see. One per claim."""
        if claim.verdict != "inconclusive":
            raise DisputeError("only inconclusive claims go to the resident")
        text = " ".join((text or "").split())
        if not text or len(text) > MAX_QUESTION_CHARS:
            raise DisputeError(f"the question must be 1 to {MAX_QUESTION_CHARS} characters")
        existing = self.store.question_for_claim(claim.claim_id)
        if existing is not None:
            return existing
        q = Question(f"q_{self._new_id()[:20]}", claim.claim_id, claim.receipt_id, claim.order_id, claim.merchant,
                     text, self._now())
        self.store.put_question(q)
        return q

    def answer(self, question_id: str, reply: str) -> Question:
        """The resident's reply, verbatim, before anyone interprets it."""
        q = self._get(question_id)
        if q.status != "open":
            raise DisputeError("this question has already been answered")
        reply = (reply or "").strip()
        if not reply or len(reply) > 2000:
            raise DisputeError("the reply must be 1 to 2000 characters")
        q = replace(q, status="answered", reply=reply, answered_at_ms=self._now())
        self.store.put_question(q)
        return q

    def resolve(self, question_id: str, outcome: str, quote: str, interpreted_by: str, note: str = "") -> Question:
        """Turn an interpretation into a signed resolution, if the reply backs it up."""
        q = self._get(question_id)
        if q.status == "resolved":
            return q
        if q.status != "answered":
            raise DisputeError("the resident has not replied yet")
        try:
            outcome = Outcome(outcome)
        except ValueError as exc:
            raise DisputeError(f"outcome must be one of {[o.value for o in Outcome]}") from exc
        quote = (quote or "").strip()
        if not quote or _normalise(quote) not in _normalise(q.reply):
            raise DisputeError("the quote must be words the resident actually wrote")
        resolution_id = f"res_{self._new_id()[:24]}"
        now = self._now()
        body = {
            "kind": "resolution",
            "resolves": q.receipt_id,
            "claim_id": q.claim_id,
            "order_id": q.order_id,
            "merchant": q.merchant,
            "outcome": outcome.value,
            "basis": "resident_statement",
            "question": q.text,
            "asked_at_ms": q.asked_at_ms,
            "answered_at_ms": q.answered_at_ms,
            "statement_sha256": sha256_hex(q.reply.encode()),
            "interpreted_by": interpreted_by,
        }
        token = issue(self.key, body, issuer=self.issuer, issued_at_s=now // 1000, receipt_id=resolution_id)
        q = replace(q, status="resolved", outcome=outcome.value, quote=quote, note=(note or "")[:300],
                    interpreted_by=interpreted_by, resolution_id=resolution_id, resolution=token)
        self.store.put_question(q)
        return q

    def _get(self, question_id: str) -> Question:
        q = self.store.get_question(question_id)
        if q is None:
            raise DisputeError("no such question")
        return q
