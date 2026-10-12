"""Run the dispute agent live: Nova (Bedrock, through Strands) asks and interprets.

For each sample reply, a fresh inconclusive claim (camera offline during the
window) goes to the agent, which writes the resident's question; the reply is
recorded; the agent interprets it; and the signed resolution is verified.

    python scripts/dispute_demo.py [--model us.amazon.nova-2-lite-v1:0] [--language Hindi]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from strands.models import BedrockModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handshake.desk import ClaimRecord, ClaimStatus  # noqa: E402
from handshake.dispute_agent import DisputeAgent  # noqa: E402
from handshake.disputes import Disputes  # noqa: E402
from handshake.receipt import HouseKey, issue, verify  # noqa: E402
from handshake.store import MemoryStore  # noqa: E402

REPLIES = [
    "haan mil gaya, guard ke paas tha",
    "No, nothing came today. I was home all afternoon.",
    "Got it but the box was torn open",
    "not sure, I was travelling. my mother might know",
    "nahi aaya abhi tak",
]


def inconclusive_claim(key: HouseKey, i: int) -> ClaimRecord:
    now = int(time.time() * 1000)
    body = {"verdict": "inconclusive", "reason": "camera_not_recording", "visit": None, "evidence": []}
    token = issue(key, body, issuer="https://lab", issued_at_s=now // 1000, receipt_id=f"r{i}")
    return ClaimRecord(f"clm_demo_{i}", "Meesho", f"ORD-40{i}", now - 3_600_000, now, carrier="Delhivery",
                       status=ClaimStatus.ISSUED, receipt_id=f"r{i}", verdict="inconclusive",
                       reason="camera_not_recording", token=token)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="us.amazon.nova-2-lite-v1:0")
    parser.add_argument("--language", default="English")
    args = parser.parse_args()
    key = HouseKey.generate()
    model = BedrockModel(model_id=args.model, boto_session=boto3.Session(region_name="us-east-1"), temperature=0.2)
    jwks = {"keys": [key.public_jwk()]}
    for i, reply in enumerate(REPLIES):
        agent = DisputeAgent(model, Disputes(MemoryStore(), key, "https://lab"), args.model, args.language)
        claim = inconclusive_claim(key, i)
        t = time.time()
        q, by_model = agent.ask(claim)
        agent.disputes.answer(q.question_id, reply)
        done = agent.interpret(q.question_id)
        row = {"question": q.text, "asked_by_model": by_model, "reply": reply, "status": done.status,
               "outcome": done.outcome, "quote": done.quote, "note": done.note, "seconds": round(time.time() - t, 1)}
        if done.resolution:
            row["resolution_verifies"] = verify(done.resolution, jwks)["resolves"] == claim.receipt_id
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
