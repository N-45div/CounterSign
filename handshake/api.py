"""The house's HTTP API.

Merchants (or the couriers working for them) authenticate with a per-merchant
API key, submit delivery claims, and poll for the signed receipt. Anyone can
fetch the house's public keys to verify a receipt. Ring delivers webhooks to
/webhooks/ring, signed with the app's HMAC key.

    POST /v1/claims              submit a claim       -> 202 pending | 200 issued
    GET  /v1/claims/{claim_id}   poll a claim         -> same shape
    GET  /.well-known/jwks.json  house public keys
    POST /webhooks/ring          Ring events (HMAC-verified, deduplicated)
    GET  /healthz
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime
from typing import Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .desk import ClaimRecord, ClaimStatus, Desk, InvalidClaim
from .house import House
from .store import MemoryStore
from .webhooks import InvalidWebhook, verified_event


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class ClaimIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    claimed_at: int | str = Field(description="Epoch milliseconds, or ISO 8601 with a timezone")
    carrier: str | None = Field(default=None, max_length=64)


def _to_ms(value: int | str) -> int:
    if isinstance(value, int):
        return value
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidClaim("claimed_at must be epoch milliseconds or ISO 8601") from exc
    if when.tzinfo is None:
        raise InvalidClaim("claimed_at needs a timezone, for example 2026-10-08T14:15:00+05:30")
    return int(when.timestamp() * 1000)


def _claim_view(record: ClaimRecord, now_ms: int) -> dict:
    view = {
        "claim_id": record.claim_id,
        "status": record.status.value,
        "order_id": record.order_id,
        "carrier": record.carrier,
        "claimed_at_ms": record.claimed_at_ms,
        "attempts": record.attempts,
    }
    if record.status is ClaimStatus.PENDING:
        view["retry_at_ms"] = record.retry_at_ms
        view["retry_after_s"] = max(0, ((record.retry_at_ms or now_ms) - now_ms + 999) // 1000)
    if record.last_error:
        view["last_error"] = record.last_error
    if record.status is ClaimStatus.ISSUED:
        view.update(receipt_id=record.receipt_id, verdict=record.verdict, reason=record.reason, receipt=record.token)
    return view


def _respond(record: ClaimRecord, now_ms: int) -> JSONResponse:
    body = _claim_view(record, now_ms)
    if record.status is ClaimStatus.PENDING:
        return JSONResponse(body, status_code=202, headers={
            "Retry-After": str(body["retry_after_s"]),
            "Location": f"/v1/claims/{record.claim_id}",
        })
    return JSONResponse(body, status_code=200)


def _error(status: int, code: str, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status, headers=headers)


def create_app(
    desk: Desk,
    house: House,
    store: MemoryStore,
    merchant_key_hashes: dict[str, str],
    webhook_signing_key: str | None,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> FastAPI:
    app = FastAPI(title="Handshake", version="0.1.0", summary="Signed doorstep delivery receipts, witnessed by a Ring camera")

    def merchant_for(authorization: str | None) -> str | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        presented = hash_key(authorization[7:].strip())
        found = None
        for merchant, expected in merchant_key_hashes.items():
            if hmac.compare_digest(presented, expected):
                found = merchant
        return found

    unauthorized = lambda: _error(401, "unauthorized", "send Authorization: Bearer <merchant API key>",  # noqa: E731
                                  {"WWW-Authenticate": "Bearer"})

    @app.exception_handler(InvalidClaim)
    async def invalid_claim(_: Request, exc: InvalidClaim):
        return _error(422, "invalid_claim", str(exc))

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "house": house.config.house_id}

    @app.get("/.well-known/jwks.json")
    def jwks():
        return JSONResponse(house.jwks(), headers={"Cache-Control": "public, max-age=300"})

    @app.post("/v1/claims")
    def submit_claim(claim: ClaimIn, authorization: str | None = Header(default=None)):
        merchant = merchant_for(authorization)
        if merchant is None:
            return unauthorized()
        record = desk.submit(merchant, claim.order_id, _to_ms(claim.claimed_at), claim.carrier)
        return _respond(record, clock_ms())

    @app.get("/v1/claims/{claim_id}")
    def get_claim(claim_id: str, authorization: str | None = Header(default=None)):
        merchant = merchant_for(authorization)
        if merchant is None:
            return unauthorized()
        record = desk.poll(claim_id, merchant)
        if record is None:
            return _error(404, "not_found", "no such claim for this merchant")
        return _respond(record, clock_ms())

    @app.post("/webhooks/ring")
    async def ring_webhook(request: Request, x_signature: str | None = Header(default=None)):
        if not webhook_signing_key:
            return _error(503, "not_configured", "webhook signing key not set")
        raw = await request.body()
        try:
            event = verified_event(webhook_signing_key, raw, x_signature)
        except InvalidWebhook as exc:
            return _error(401, "bad_signature", str(exc))
        if not store.first_sighting(event.request_id):
            return {"ok": True, "duplicate": True}
        if event.type in ("device_online", "device_offline") and event.device_id and event.timestamp_ms:
            store.record_status(event.device_id, event.timestamp_ms, event.type == "device_online")
        return {"ok": True, "type": event.type}

    return app
