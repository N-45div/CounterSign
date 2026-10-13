"""The Doorstep Lab server: the Ring emulator, a lab house, and the 3D page.

    python -m handshake.lab.server [--port 8800] [--witness nova|truth] [--model MODEL]

Every visitor gets a session (see sessions.py), so the same app runs locally
for one person and on Lambda for many:

    GET  /                                  the 3D doorstep page
    GET  /lab/config, /lab/scenarios
    POST /lab/start                         new session: scenario timeline + Ring token
    POST /lab/s/{sid}/frames/{at_ms}        a doorbell frame the page rendered
    POST /lab/s/{sid}/webhooks/ring         Ring webhooks for this session's house (HMAC-checked)
    POST /lab/s/{sid}/claim                 the courier's claim -> verdict + signed receipt
    GET  /lab/s/{sid}/claims/{claim_id}     poll a claim
    GET  /lab/s/{sid}/evidence/{receipt}/{before|after}
    POST /lab/s/{sid}/claims/{claim_id}/ask           an inconclusive claim goes to the resident
    POST /lab/s/{sid}/questions/{question_id}/answer  the resident's reply -> signed resolution
    GET  /lab/jwks.json                     the lab house's public keys
    /v1/..., /lab/media/...                 the Ring Partner API emulator (by bearer token)

The house's Ring client calls the emulator over HTTP with the session's token,
exactly as it would call api.amazonvision.com.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..api import _claim_view, _question_view, _to_ms
from ..desk import ClaimStatus, Desk, InvalidClaim
from ..dispute_agent import _payload, template_question
from ..disputes import DisputeError, Disputes
from ..house import House, HouseConfig
from ..receipt import HouseKey
from ..ring import RingClient
from ..verdict import WitnessReport, device_ref
from ..webhooks import InvalidWebhook, verified_event
from .emulator import LabWorld, _err, check_frame, ring_api, send_status_webhooks
from .scenarios import SCENARIOS

STATIC = Path(__file__).parent / "static"
MERCHANT = "lab-courier"
LAB_SALT = "lab-salt"


class TruthWitness:
    """Reports what the scenario put in front of the camera at each frame's moment.

    Used locally without AWS, and on the hosted lab once the day's Nova budget is spent."""

    model_id = "lab-truth"

    def __init__(self, world: LabWorld):
        self.world = world

    def compare(self, before, after):
        b = self.world.scenario.scene_at(self.world.seconds(before.at_ms))
        a = self.world.scenario.scene_at(self.world.seconds(after.at_ms))
        notes = tuple(a.note) + tuple(b.note)
        return WitnessReport(b.parcel, a.parcel, 1.0, self.model_id, bool(notes), notes)


class ClaimIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    claimed_at: int | str
    carrier: str | None = Field(default=None, max_length=64)


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)


def create_lab(
    sessions,
    house_store,
    key: HouseKey,
    webhook_key: str,
    nova: Callable[[], object] | None = None,
    base_url: str | None = None,
    dispute_agent: Callable[[Disputes], object] | None = None,
    agent_label: str | None = None,
) -> FastAPI:
    """`nova` builds the Nova witness; None means the scenario's ground truth.
    `base_url` is where the house reaches the emulator; None means the URL the request came in on.
    `dispute_agent` builds the agent that words the resident's question and reads the reply (it shares
    the daily budget with the witness); None means a template question and nobody to read the reply."""
    app = FastAPI(title="Handshake Doorstep Lab")

    def base(request: Request) -> str:
        return (base_url or str(request.base_url)).rstrip("/")

    def house_for(world: LabWorld, sid: str, request: Request) -> tuple[House, str]:
        if nova is not None and sessions.take_witness_run():
            witness = nova()
        else:
            witness = TruthWitness(world)
        ring = RingClient(sessions.get(sid).token, base_url=base(request))
        config = HouseConfig("lab-house", f"{base(request)}/lab", world.device.id, LAB_SALT)
        return House(config, ring, witness, key, house_store), witness.model_id

    def session_claim(world: LabWorld, claim_id: str):
        """A claim this session's camera witnessed: its receipt names this session's device pseudonym."""
        record = house_store.get_claim(claim_id)
        if record is None or record.merchant != MERCHANT or not record.token:
            return None
        return record if _payload(record.token).get("device") == device_ref(world.device.id, LAB_SALT) else None

    def agent_for(request: Request):
        disputes = Disputes(house_store, key, f"{base(request)}/lab")
        if dispute_agent is not None and sessions.take_witness_run():
            return disputes, dispute_agent(disputes)
        return disputes, None

    def receive_webhook(raw: bytes, signature: str | None) -> dict:
        event = verified_event(webhook_key, raw, signature)
        if house_store.first_sighting(event.request_id) and event.type in ("device_online", "device_offline"):
            house_store.record_status(event.device_id, event.timestamp_ms, event.type == "device_online")
        return {"ok": True, "type": event.type}

    @app.exception_handler(InvalidClaim)
    async def invalid_claim(_: Request, exc: InvalidClaim):
        return JSONResponse({"error": "invalid_claim", "detail": str(exc)}, status_code=422)

    @app.get("/lab/config")
    def config():
        return {"witness": "Amazon Nova on Bedrock + Amazon Textract" if nova else "lab-truth",
                "dispute_agent": agent_label if dispute_agent else None, "scenarios": list(SCENARIOS)}

    @app.get("/lab/scenarios")
    def scenarios():
        return [{"name": s.name, "title": s.title, "story": s.story,
                 "expected": {"verdict": s.expected[0], "reason": s.expected[1]}} for s in SCENARIOS.values()]

    @app.post("/lab/start")
    def start(body: dict):
        if body.get("scenario") not in SCENARIOS:
            return _err(400, "UNKNOWN_SCENARIO", f"choose one of {sorted(SCENARIOS)}")
        session = sessions.create(body["scenario"])
        world = sessions.world(session.sid)
        sent = send_status_webhooks(world, "in-process", webhook_key,
                                    post=lambda url, data, headers, timeout: receive_webhook(data, headers["X-Signature"]))
        return {"session": session.sid, "device_id": world.device.id, "webhooks_sent": sent, **world.timeline()}

    @app.post("/lab/s/{sid}/frames/{at_ms}")
    async def upload(sid: str, at_ms: int, request: Request):
        world = sessions.world(sid)
        if world is None:
            return _err(404, "NO_SESSION", "start a scenario first")
        image = await request.body()
        if (refused := check_frame(image)) is not None:
            return refused
        try:
            world.upload(at_ms, image)
        except ValueError as exc:
            return _err(400, "NOT_A_TICK", str(exc))
        return Response(status_code=204)

    @app.post("/lab/s/{sid}/webhooks/ring")
    async def webhook(sid: str, request: Request, x_signature: str | None = Header(default=None)):
        try:
            return receive_webhook(await request.body(), x_signature)
        except InvalidWebhook as exc:
            return JSONResponse({"error": "bad_signature", "detail": str(exc)}, status_code=401)

    @app.post("/lab/s/{sid}/claim")
    def claim(sid: str, body: ClaimIn, request: Request):
        world = sessions.world(sid)
        if world is None:
            return _err(404, "NO_SESSION", "start a scenario first")
        house, witness = house_for(world, sid, request)
        record = Desk(house, house_store).submit(MERCHANT, body.order_id, _to_ms(body.claimed_at), body.carrier)
        view = _claim_view(record, int(time.time() * 1000))
        view["witness"] = witness
        return JSONResponse(view, status_code=202 if record.status is ClaimStatus.PENDING else 200)

    @app.get("/lab/s/{sid}/claims/{claim_id}")
    def poll(sid: str, claim_id: str, request: Request):
        world = sessions.world(sid)
        if world is None:
            return _err(404, "NO_SESSION", "start a scenario first")
        house, witness = house_for(world, sid, request)
        record = Desk(house, house_store).poll(claim_id, MERCHANT)
        if record is None:
            return _err(404, "NOT_FOUND", "no such claim")
        return {**_claim_view(record, int(time.time() * 1000)), "witness": witness}

    @app.get("/lab/s/{sid}/evidence/{receipt_id}/{kind}")
    def evidence(sid: str, receipt_id: str, kind: str):
        frames = house_store.get_frames(receipt_id)
        if kind not in frames:
            return _err(404, "NOT_FOUND", "no such frame")
        return Response(frames[kind], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/lab/s/{sid}/claims/{claim_id}/ask")
    def ask(sid: str, claim_id: str, request: Request):
        world = sessions.world(sid)
        record = session_claim(world, claim_id) if world is not None else None
        if record is None:
            return _err(404, "NOT_FOUND", "no such claim in this session")
        if record.verdict != "inconclusive":
            return _err(409, "NOT_INCONCLUSIVE", "only claims the camera could not settle go to the resident")
        disputes, agent = agent_for(request)
        if agent is not None:
            question, by_model = agent.ask(record)
        else:
            question, by_model = disputes.open(record, template_question(record)), False
        return {**_question_view(question), "asked_by": agent.model_id if by_model else "template",
                "agent": agent_label if agent is not None else None}

    @app.post("/lab/s/{sid}/questions/{question_id}/answer")
    def answer(sid: str, question_id: str, body: ReplyIn, request: Request):
        world = sessions.world(sid)
        question = house_store.get_question(question_id)
        if world is None or question is None or session_claim(world, question.claim_id) is None:
            return _err(404, "NOT_FOUND", "no such question in this session")
        if question.status != "open":
            return _err(409, "ALREADY_ANSWERED", "this question has already been answered")
        disputes, agent = agent_for(request)
        try:
            question = disputes.answer(question_id, body.reply)
        except DisputeError as exc:
            return _err(422, "INVALID_REPLY", str(exc))
        if agent is not None:
            question = agent.interpret(question_id)
        return {**_question_view(question), "agent": agent_label if agent is not None else None}

    @app.get("/lab/jwks.json")
    @app.get("/.well-known/jwks.json")
    def jwks():
        return JSONResponse({"keys": [key.public_jwk()]}, headers={"Cache-Control": "public, max-age=300"})

    ring_api(sessions.world_for_token, app)
    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main() -> None:
    import uvicorn

    from ..store import MemoryStore
    from .sessions import MemorySessions

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--witness", choices=["nova", "truth"], default="nova")
    parser.add_argument("--model", default="us.amazon.nova-2-lite-v1:0")
    args = parser.parse_args()

    nova = None
    if args.witness == "nova":
        import boto3

        from ..doorman import TextractOcr
        from ..witness import NovaWitness
        aws = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))
        runtime, textract = aws.client("bedrock-runtime"), aws.client("textract")
        nova = lambda: NovaWitness(runtime, args.model, TextractOcr(textract))  # noqa: E731

    base = f"http://127.0.0.1:{args.port}"
    app = create_lab(MemorySessions(), MemoryStore(), HouseKey.generate(), "lab-webhook-key", nova, base)
    print(f"Doorstep Lab on {base}  (witness: {args.witness})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
