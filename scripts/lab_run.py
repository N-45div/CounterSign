"""Run every Doorstep Lab scenario through a local house and report the verdicts.

Starts the Ring emulator on a local port, points an in-memory house at it with
Handshake's unchanged Ring client, plays each scenario, submits the courier's
claim, and prints the verdict next to the one the scenario expects.

    python scripts/lab_run.py                   # Amazon Nova + Textract (needs AWS credentials)
    python scripts/lab_run.py --witness truth   # the lab's ground truth, no AWS
    python scripts/lab_run.py --model us.amazon.nova-2-lite-v1:0 --scenario planted_note

Frames for each scenario are written to probe-out/lab-run/ (gitignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handshake.desk import Desk  # noqa: E402
from handshake.house import House, HouseConfig  # noqa: E402
from handshake.lab.emulator import LabState, create_emulator, send_status_webhooks  # noqa: E402
from handshake.lab.scenarios import SCENARIOS  # noqa: E402
from handshake.receipt import HouseKey  # noqa: E402
from handshake.ring import RingClient  # noqa: E402
from handshake.store import MemoryStore  # noqa: E402
from handshake.verdict import WitnessReport  # noqa: E402
from handshake.webhooks import verified_event  # noqa: E402

TOKEN = "lab-run"
KEY = "lab-run-hmac"


class TruthWitness:
    def __init__(self, state):
        self.state = state

    def compare(self, before, after):
        scenes = self.state.world.scenes_by_hash
        b = scenes[hashlib.sha256(before.image).hexdigest()]
        a = scenes[hashlib.sha256(after.image).hexdigest()]
        notes = tuple(a.note) + tuple(b.note)
        return WitnessReport(b.parcel, a.parcel, 1.0, "lab-truth", bool(notes), notes)


def start_emulator(state: LabState) -> tuple[str, uvicorn.Server]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_emulator(state, tokens={TOKEN}), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--witness", choices=["nova", "truth"], default="nova")
    parser.add_argument("--model", default="us.amazon.nova-lite-v1:0")
    parser.add_argument("--scenario", action="append", help="run only these (repeatable)")
    args = parser.parse_args()

    state = LabState()
    url, server = start_emulator(state)
    if args.witness == "nova":
        import boto3

        from handshake.doorman import TextractOcr
        from handshake.witness import NovaWitness
        session = boto3.Session(region_name="us-east-1")
        witness = NovaWitness(session.client("bedrock-runtime"), args.model, TextractOcr(session.client("textract")))
    else:
        witness = TruthWitness(state)

    out = ROOT / "probe-out" / "lab-run"
    out.mkdir(parents=True, exist_ok=True)
    rows, passed = [], 0
    for name in args.scenario or list(SCENARIOS):
        world = state.play(name)
        store = MemoryStore()
        ring = RingClient(TOKEN, base_url=url)
        house = House(HouseConfig("lab-house", url, "", "lab-salt"), ring, witness, HouseKey.generate(), store,
                      device_resolver=lambda: ring.devices()[0].id)
        send_status_webhooks(world, "unused", KEY, post=lambda u, data, headers, timeout: (
            lambda ev: store.record_status(ev.device_id, ev.timestamp_ms, ev.type == "device_online")
        )(verified_event(KEY, data, headers["X-Signature"])))
        started = time.time()
        record = Desk(house, store).submit("lab-merchant", f"ORD-{name}", world.at(world.scenario.claim_at_s))
        took = time.time() - started
        for kind, image in store.get_frames(record.receipt_id or "").items():
            (out / f"{name}_{kind}.jpg").write_bytes(image)
        ok = (record.verdict, record.reason) == world.scenario.expected
        passed += ok
        rows.append({"scenario": name, "expected": "/".join(world.scenario.expected),
                     "got": f"{record.verdict}/{record.reason}", "ok": ok, "seconds": round(took, 1)})
        print(json.dumps(rows[-1]))

    server.should_exit = True
    print(f"\n{passed}/{len(rows)} scenarios reached the expected verdict with witness={args.witness}"
          + (f" ({args.model})" if args.witness == "nova" else ""))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
