"""The Doorstep Lab server: the Ring emulator, a lab house, and the 3D page.

    python -m handshake.lab.server [--port 8800] [--witness nova|truth] [--model MODEL]

One process serves three things:

    /                  the 3D doorstep page (static files)
    /v1/..., /lab/...  the Ring Partner API emulator the page drives
    /house/...         a house (claims, receipts, public keys, Ring webhooks)
                       whose Ring client points back at the emulator over HTTP

Starting a scenario plays it, delivers its online/offline webhooks to the
house signed like Ring's, and gives the house a fresh store, so one run can
never leak into the next. The house key stays the same for the whole
session so receipts verify against one key set.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..api import create_app, hash_key
from ..desk import Desk
from ..house import House, HouseConfig
from ..receipt import HouseKey
from ..ring import RingClient
from ..store import MemoryStore
from ..verdict import WitnessReport
from .emulator import LabState, create_emulator, send_status_webhooks
from .scenarios import SCENARIOS

STATIC = Path(__file__).parent / "static"
RING_TOKEN = "lab-ring-token"
WEBHOOK_KEY = "lab-ring-webhook-key"
MERCHANT = "lab-courier"
MERCHANT_KEY = "hsk_lab_courier_demo"  # a sandbox merchant; the page uses it to submit claims


class TruthWitness:
    """Reports what the scenario put in each frame. Used when there is no AWS access."""

    def __init__(self, state: LabState):
        self.state = state

    def compare(self, before, after):
        scenes = self.state.world.scenes_by_hash
        b = scenes[hashlib.sha256(before.image).hexdigest()]
        a = scenes[hashlib.sha256(after.image).hexdigest()]
        notes = tuple(a.note) + tuple(b.note)
        return WitnessReport(b.parcel, a.parcel, 1.0, "lab-truth", bool(notes), notes)


class Swappable:
    """An ASGI app whose target is replaced on every scenario run."""

    def __init__(self):
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            response = JSONResponse({"error": "not_playing", "detail": "start a scenario first"}, status_code=409)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def make_witness(kind: str, model: str, state: LabState):
    if kind == "truth":
        return TruthWitness(state)
    import boto3

    from ..doorman import TextractOcr
    from ..witness import NovaWitness
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return NovaWitness(session.client("bedrock-runtime"), model, TextractOcr(session.client("textract")))


def create_lab(base_url: str, witness_kind: str = "truth", model: str = "us.amazon.nova-lite-v1:0") -> FastAPI:
    state = LabState()
    app = create_emulator(state, tokens={RING_TOKEN})
    key = HouseKey.generate()
    witness = make_witness(witness_kind, model, state)
    house_slot = Swappable()
    current: dict = {}

    def fresh_house() -> None:
        store = MemoryStore()
        ring = RingClient(RING_TOKEN, base_url=base_url)
        house = House(HouseConfig("lab-house", f"{base_url}/house", "", "lab-salt"), ring, witness, key, store,
                      device_resolver=lambda: ring.devices()[0].id)
        desk = Desk(house, store)
        current.update(store=store, house=house)
        house_slot.app = create_app(desk, house, store, {MERCHANT: hash_key(MERCHANT_KEY)}, WEBHOOK_KEY)

    @app.get("/lab/config")
    def config():
        return {"witness": getattr(witness, "model_id", "lab-truth"), "merchant_key": MERCHANT_KEY,
                "scenarios": [s.name for s in SCENARIOS.values()]}

    @app.post("/lab/start")
    def start(body: dict):
        if body.get("scenario") not in SCENARIOS:
            return JSONResponse({"error": "unknown_scenario", "choices": sorted(SCENARIOS)}, status_code=400)
        world = state.play(body["scenario"])
        fresh_house()
        sent = send_status_webhooks(world, f"{base_url}/house/webhooks/ring", WEBHOOK_KEY, post=requests.post)
        timeline = world.timeline()
        timeline["webhooks_sent"] = sent
        return timeline

    @app.get("/lab/evidence/{receipt_id}/{kind}")
    def evidence(receipt_id: str, kind: str):
        """The resident's view of the frames behind a receipt (lab only; frames never leave a real house)."""
        frames = current["store"].get_frames(receipt_id) if current else {}
        if kind not in frames:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return Response(frames[kind], media_type="image/jpeg")

    app.mount("/house", house_slot)
    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--witness", choices=["nova", "truth"], default="nova")
    parser.add_argument("--model", default="us.amazon.nova-lite-v1:0")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    app = create_lab(base, args.witness, args.model)
    print(f"Doorstep Lab on {base}  (witness: {args.witness})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
