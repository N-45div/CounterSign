"""Edge-case pass against a deployed house and the hosted Doorstep Lab.

Every failure path a merchant, Ring, the resident or a lab visitor can hit,
checked over HTTP against the real deployment, then CloudWatch for errors
during the run. Nothing here is faked: claims go through the real desk, the
witness is Amazon Nova, questions and readings come from the dispute agent on
AgentCore Runtime.

    python scripts/check_prod.py --house URL [--lab URL] [--dispute]

Keys come from the environment or ~/.secrets/countersign.env
(COUNTERSIGN_MERCHANT_KEY, COUNTERSIGN_RESIDENT_KEY). AWS credentials are needed
for the webhook key (house secret), --dispute and the log check.

--dispute settles a real claim on the Ring Playground device through the
resident and needs a live Ring token in the house's secret. The Playground
cannot take its device offline, so the check signs a device_offline and a
device_online event with the house's webhook key, the way Ring signs them.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from countersign.receipt import verify  # noqa: E402

MIN = 60_000
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}", flush=True)
    return ok


def local_keys() -> dict:
    keys = {}
    path = Path.home() / ".secrets" / "countersign.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    return {**keys, **{k: v for k, v in os.environ.items() if k.startswith("COUNTERSIGN_")}}


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def signed(key: str, kind: str, device_id: str, at_ms: int, request_id: str) -> tuple[bytes, dict]:
    body = json.dumps(
        {
            "meta": {
                "version": "1.1",
                "time": datetime.fromtimestamp(at_ms / 1000, timezone.utc).isoformat(),
                "request_id": request_id,
            },
            "data": {
                "id": request_id,
                "type": kind,
                "attributes": {"source": device_id, "source_type": "devices", "timestamp": at_ms},
            },
        },
        separators=(",", ":"),
    ).encode()
    sig = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Signature": sig}


# -- the house's API ----------------------------------------------------------------


def house_checks(url: str, keys: dict, webhook_key: str | None, run: str) -> None:
    merchant, resident = keys["COUNTERSIGN_MERCHANT_KEY"], keys.get("COUNTERSIGN_RESIDENT_KEY")
    now = int(time.time() * 1000)

    def claim(body, key=merchant):
        return requests.post(f"{url}/v1/claims", json=body, headers=bearer(key), timeout=60)

    r = requests.get(f"{url}/healthz", timeout=30)
    check("health", r.status_code == 200 and r.json().get("ok") is True, r.text[:60])
    r = requests.get(f"{url}/.well-known/jwks.json", timeout=30)
    jwk = (r.json().get("keys") or [{}])[0]
    check(
        "public key is Ed25519 and cacheable",
        jwk.get("crv") == "Ed25519" and "max-age" in r.headers.get("cache-control", ""),
    )

    body = {"order_id": f"CHECK-{run}-1", "claimed_at": now}
    r = requests.post(f"{url}/v1/claims", json=body, timeout=30)
    # Lambda Function URLs rename the reserved WWW-Authenticate header to x-amzn-Remapped-www-authenticate
    challenge = [h for h in ("www-authenticate", "x-amzn-remapped-www-authenticate") if r.headers.get(h) == "Bearer"]
    check(
        "claim without a key is 401 with a Bearer challenge",
        r.status_code == 401 and bool(challenge),
        f"challenge in {challenge[0] if challenge else 'no header'}",
    )
    check("claim with a wrong key is 401", claim(body, "hsk_wrong").status_code == 401)
    if resident:
        check("the resident key cannot file claims", claim(body, resident).status_code == 401)
    for label, bad in [
        ("naive ISO time", "2026-10-15T10:00:00"),
        ("time in the future", now + 60 * MIN),
        ("claim older than 30 days", now - 31 * 24 * 60 * MIN),
        ("unparseable time", "yesterday"),
    ]:
        r = claim({"order_id": f"CHECK-{run}-bad", "claimed_at": bad})
        check(
            f"refuses {label}",
            r.status_code == 422,
            r.json().get("detail", "")[:70] if r.status_code == 422 else r.text[:80],
        )
    check("refuses an empty order id", claim({"order_id": "", "claimed_at": now}).status_code == 422)
    check("refuses a 129-character order id", claim({"order_id": "x" * 129, "claimed_at": now}).status_code == 422)

    r = claim(body)
    fresh = r.json()
    check(
        "a fresh claim is 202 with Retry-After and Location",
        r.status_code == 202
        and int(r.headers.get("retry-after", "0")) > 0
        and r.headers.get("location") == f"/v1/claims/{fresh.get('claim_id')}",
        f"retry {r.headers.get('retry-after')} s",
    )
    again = claim(body).json()
    check("resubmitting returns the same claim", again.get("claim_id") == fresh.get("claim_id"))
    check(
        "an unknown claim is 404",
        requests.get(f"{url}/v1/claims/clm_nope", headers=bearer(merchant), timeout=30).status_code == 404,
    )

    past = claim({"order_id": f"CHECK-{run}-past", "claimed_at": now - 15 * MIN, "carrier": "Check"}).json()
    state = past.get("verdict") or past.get("last_error") or past.get("status")
    check(
        "a claim whose window has closed is decided or says why not",
        past.get("status") == "issued" or past.get("last_error") == "ring_token_expired",
        state,
    )

    if webhook_key:
        device = f"ava1.ring.device.CHECK{run}"
        raw, headers = signed(webhook_key, "device_online", device, now, f"check-{run}-online")
        r = requests.post(f"{url}/webhooks/ring", data=raw, headers=headers, timeout=30)
        check("a signed Ring webhook is accepted", r.status_code == 200 and r.json().get("type") == "device_online")
        r = requests.post(f"{url}/webhooks/ring", data=raw, headers=headers, timeout=30)
        check("a replayed webhook is recognised as a duplicate", r.json().get("duplicate") is True)
        r = requests.post(
            f"{url}/webhooks/ring", data=raw, headers={**headers, "X-Signature": "sha256=" + "0" * 64}, timeout=30
        )
        check("a webhook with a wrong signature is 401", r.status_code == 401)
        r = requests.post(f"{url}/webhooks/ring", data=raw, headers={"Content-Type": "application/json"}, timeout=30)
        check("a webhook without a signature is 401", r.status_code == 401)
        junk, headers = (
            b"not json",
            {"X-Signature": "sha256=" + hmac.new(webhook_key.encode(), b"not json", hashlib.sha256).hexdigest()},
        )
        check(
            "a signed body that is not a Ring event is 401",
            requests.post(f"{url}/webhooks/ring", data=junk, headers=headers, timeout=30).status_code == 401,
        )

    if resident:
        q = f"{url}/v1/resident/questions"
        check("resident routes refuse no key", requests.get(q, timeout=30).status_code == 401)
        check(
            "resident routes refuse the merchant key",
            requests.get(q, headers=bearer(merchant), timeout=30).status_code == 401,
        )
        r = requests.get(q, headers=bearer(resident), timeout=30)
        check(
            "the resident can list open questions",
            r.status_code == 200 and "questions" in r.json(),
            f"{len(r.json().get('questions', []))} open",
        )
        check(
            "an unknown question is 404",
            requests.get(f"{q}/q_nope", headers=bearer(resident), timeout=30).status_code == 404,
        )
        r = requests.post(f"{q}/q_nope/answer", json={"reply": "yes"}, headers=bearer(resident), timeout=30)
        check("answering an unknown question is 404", r.status_code == 404)


# -- a real dispute on the Ring Playground device -------------------------------------


def dispute_check(url: str, keys: dict, webhook_key: str, ring_token: str, run: str) -> None:
    from countersign.ring import RingClient
    from countersign.tokens import StaticToken

    merchant, resident = keys["COUNTERSIGN_MERCHANT_KEY"], keys["COUNTERSIGN_RESIDENT_KEY"]
    devices = RingClient(StaticToken(ring_token)).devices()
    if not check(
        "the Ring token sees the Playground device", bool(devices), devices[0].id[:40] if devices else "no devices"
    ):
        return
    device, now = devices[0].id, int(time.time() * 1000)
    claimed_at = now - 15 * MIN  # window: 35 to 5 minutes ago, closed and settled
    for kind, at in (("device_offline", now - 30 * MIN), ("device_online", now - 6 * MIN)):
        raw, headers = signed(webhook_key, kind, device, at, f"check-{run}-{kind}")
        r = requests.post(f"{url}/webhooks/ring", data=raw, headers=headers, timeout=30)
        check(f"Ring {kind} recorded", r.status_code == 200)

    order = f"CHECK-{run}-DISPUTE"
    r = requests.post(
        f"{url}/v1/claims",
        headers=bearer(merchant),
        timeout=90,
        json={"order_id": order, "claimed_at": claimed_at, "carrier": "Delhivery"},
    )
    body = r.json()
    if not check(
        "the house answers inconclusive because the camera was offline",
        (body.get("verdict"), body.get("reason")) == ("inconclusive", "camera_not_recording"),
        f"{body.get('verdict')}/{body.get('reason') or body.get('last_error')}",
    ):
        return
    claim_id, receipt_id = body["claim_id"], body["receipt_id"]

    started, question = time.time(), None
    while time.time() - started < 120 and question is None:
        time.sleep(4)
        listed = requests.get(f"{url}/v1/resident/questions", headers=bearer(resident), timeout=30).json()["questions"]
        question = next((q for q in listed if q["claim_id"] == claim_id), None)
    if not check(
        "the worker asked the resident through the agent",
        question is not None,
        f"{time.time() - started:.0f} s: {question['text'] if question else 'no question'}",
    ):
        return
    dispute = (
        requests.get(f"{url}/v1/claims/{claim_id}", headers=bearer(merchant), timeout=30).json().get("dispute", {})
    )
    check(
        "the merchant sees the open question",
        dispute.get("status") == "open" and dispute.get("question") == question["text"],
    )

    reply = "No, nothing came. I was home all day and checked with the guard."
    answer = f"{url}/v1/resident/questions/{question['question_id']}/answer"
    t = time.time()
    r = requests.post(answer, json={"reply": reply}, headers=bearer(resident), timeout=90)
    done = r.json()
    check(
        "the agent reads the reply and the house signs a resolution",
        done.get("status") == "resolved"
        and done.get("outcome") == "not_received"
        and done.get("quote", "").lower() in reply.lower(),
        f"{time.time() - t:.0f} s, quote: {done.get('quote')!r}, note: {done.get('note')!r}",
    )
    if done.get("resolution"):
        jwks = requests.get(f"{url}/.well-known/jwks.json", timeout=30).json()
        payload = verify(done["resolution"], jwks)
        check(
            "the resolution verifies and points at the receipt",
            payload["kind"] == "resolution"
            and payload["resolves"] == receipt_id
            and payload["statement_sha256"] == hashlib.sha256(reply.encode()).hexdigest(),
        )
    dispute = (
        requests.get(f"{url}/v1/claims/{claim_id}", headers=bearer(merchant), timeout=30).json().get("dispute", {})
    )
    check(
        "the merchant gets the resolution but not the reply",
        dispute.get("outcome") == "not_received"
        and dispute.get("resolution") == done.get("resolution")
        and reply not in json.dumps(dispute),
    )
    check(
        "a second answer is refused",
        requests.post(answer, json={"reply": "actually yes"}, headers=bearer(resident), timeout=30).status_code == 409,
    )


# -- the hosted lab -------------------------------------------------------------------


def lab_checks(lab: str) -> None:
    post = lambda path, **kw: requests.post(f"{lab}{path}", timeout=120, **kw)  # noqa: E731
    check("lab: an unknown scenario is 400", post("/lab/start", json={"scenario": "nope"}).status_code == 400)
    check(
        "lab: an unknown session is 404",
        post("/lab/s/nope/claim", json={"order_id": "x", "claimed_at": 1}).status_code == 404,
    )
    check(
        "lab: a bad Ring token is 401",
        requests.get(f"{lab}/v1/devices", headers=bearer("lab_nope"), timeout=30).status_code == 401,
    )

    offline, honest = (
        post("/lab/start", json={"scenario": "camera_offline"}).json(),
        post("/lab/start", json={"scenario": "honest_delivery"}).json(),
    )
    sid = offline["session"]
    tick = offline["start_ms"] + int(offline["ticks"][0] * 1000)
    check(
        "lab: a frame that is not an image is 415",
        post(f"/lab/s/{sid}/frames/{tick}", data=b"hello").status_code == 415,
    )
    check(
        "lab: a frame off the camera's ticks is 400",
        post(f"/lab/s/{sid}/frames/{tick + 1}", data=b"\xff\xd8\xff").status_code == 400,
    )
    check(
        "lab: a webhook with a wrong signature is 401",
        post(f"/lab/s/{sid}/webhooks/ring", data=b"{}", headers={"X-Signature": "sha256=00"}).status_code == 401,
    )

    receipt = post(
        f"/lab/s/{sid}/claim",
        json={"order_id": "CHECK-off", "claimed_at": offline["claim_at_ms"], "carrier": "Lab Courier"},
    ).json()
    check(
        "lab: camera offline is inconclusive",
        (receipt.get("verdict"), receipt.get("reason")) == ("inconclusive", "camera_not_recording"),
    )
    delivered = post(
        f"/lab/s/{honest['session']}/claim", json={"order_id": "CHECK-ok", "claimed_at": honest["claim_at_ms"]}
    ).json()
    check(
        "lab: an honest delivery is delivered by Nova",
        delivered.get("verdict") == "delivered",
        f"witness {delivered.get('witness')}",
    )
    check(
        "lab: a delivered claim does not go to the resident",
        post(f"/lab/s/{honest['session']}/claims/{delivered['claim_id']}/ask").status_code == 409,
    )
    check(
        "lab: another visitor cannot ask about this claim",
        post(f"/lab/s/{honest['session']}/claims/{receipt['claim_id']}/ask").status_code == 404,
    )

    q = post(f"/lab/s/{sid}/claims/{receipt['claim_id']}/ask", json={"timezone": "America/Los_Angeles"}).json()
    check("lab: the agent words the question", q.get("asked_by", "template") != "template", f"{q.get('text')!r}")
    answer = f"/lab/s/{sid}/questions/{q['question_id']}/answer"
    check(
        "lab: another visitor cannot answer it",
        post(f"/lab/s/{honest['session']}/questions/{q['question_id']}/answer", json={"reply": "no"}).status_code
        == 404,
    )
    check("lab: an empty reply is 422", post(answer, json={"reply": ""}).status_code == 422)
    done = post(answer, json={"reply": "haan, mil gaya. Guard ke paas chhoda tha"}).json()
    check("lab: a Hinglish yes resolves as received", done.get("outcome") == "received", f"quote {done.get('quote')!r}")
    jwks = requests.get(f"{lab}/lab/jwks.json", timeout=30).json()
    check(
        "lab: the resolution verifies against the lab key",
        bool(done.get("resolution")) and verify(done["resolution"], jwks)["resolves"] == receipt["receipt_id"],
    )
    check("lab: a second answer is 409", post(answer, json={"reply": "no"}).status_code == 409)


# -- CloudWatch ------------------------------------------------------------------------


def log_check(region: str, since_ms: int) -> None:
    import boto3

    cfn, logs = boto3.client("cloudformation", region_name=region), boto3.client("logs", region_name=region)
    for logical in ("ApiFunction", "WorkerFunction", "LabFunction"):
        name = cfn.describe_stack_resource(StackName="countersign", LogicalResourceId=logical)["StackResourceDetail"][
            "PhysicalResourceId"
        ]
        hits = []
        for pattern in ("ERROR", "Traceback", "Task timed out"):
            try:
                events = logs.filter_log_events(
                    logGroupName=f"/aws/lambda/{name}", startTime=since_ms, filterPattern=f'"{pattern}"'
                ).get("events", [])
            except logs.exceptions.ResourceNotFoundException:
                events = []
            hits += [e["message"].strip()[:160] for e in events]
        check(f"no errors in {logical} logs during the run", not hits, "; ".join(hits[:3]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--house", required=True)
    parser.add_argument("--lab")
    parser.add_argument("--dispute", action="store_true")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--no-logs", action="store_true")
    args = parser.parse_args()
    house, keys, run = args.house.rstrip("/"), local_keys(), str(int(time.time()))
    started = int(time.time() * 1000)

    webhook_key = ring_token = None
    try:
        import boto3

        sm = boto3.client("secretsmanager", region_name=args.region)
        webhook_key = json.loads(sm.get_secret_value(SecretId="countersign/house")["SecretString"])["webhook_signing_key"]
        ring_token = sm.get_secret_value(SecretId="countersign/ring-token")["SecretString"]
    except Exception as exc:  # no AWS credentials: the signed-webhook, dispute and log checks are skipped
        print(f"(no house secret: {type(exc).__name__}; skipping signed webhooks, --dispute and logs)")

    house_checks(house, keys, webhook_key, run)
    if args.dispute and webhook_key and ring_token:
        dispute_check(house, keys, webhook_key, ring_token, run)
    if args.lab:
        lab_checks(args.lab.rstrip("/"))
    if webhook_key and not args.no_logs:
        time.sleep(10)  # CloudWatch takes a few seconds to index
        log_check(args.region, started)

    failed = [name for name, ok, _ in results if not ok]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
