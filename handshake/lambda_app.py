"""AWS Lambda entry points for the deployed house.

    handler         the HTTP API behind a Lambda Function URL (via Mangum)
    worker_handler  drains the SQS queue of claims due for another attempt

Configuration comes from environment variables set by the SAM template, and
secrets from AWS Secrets Manager:

    HANDSHAKE_TABLE              DynamoDB table
    HANDSHAKE_BUCKET             private S3 bucket for evidence frames
    HANDSHAKE_QUEUE_URL          SQS queue for scheduled attempts
    HANDSHAKE_HOUSE_SECRET       JSON: house_id, signing_key_pem, salt,
                                 webhook_signing_key, merchants {name: sha256}
    HANDSHAKE_RING_TOKEN_SECRET  the Ring access token (Playground tokens last 30 min)
    HANDSHAKE_PUBLIC_URL         the house's public URL, used as receipt issuer
    HANDSHAKE_DEVICE_ID          optional; otherwise the first Ring device
    HANDSHAKE_WITNESS_MODEL      optional Bedrock model id for the witness
"""

from __future__ import annotations

import json
import math
import os
import time
from functools import lru_cache

import boto3
from mangum import Mangum

from .api import create_app
from .aws_store import DynamoStore
from .desk import Desk
from .doorman import TextractOcr
from .house import House, HouseConfig
from .receipt import HouseKey
from .ring import RingClient, RingError
from .tokens import SecretToken
from .witness import DEFAULT_MODEL, NovaWitness

MAX_SQS_DELAY_S = 900


class SqsScheduler:
    """Schedule an attempt with an SQS delivery delay (at most 15 minutes).

    A claim due further out arrives early, gets "not ready" from the house
    again, and is rescheduled; nothing is lost by the cap.
    """

    def __init__(self, sqs_client, queue_url: str, clock_ms=lambda: int(time.time() * 1000)):
        self.sqs = sqs_client
        self.queue_url = queue_url
        self._now = clock_ms

    def schedule(self, claim_id: str, at_ms: int) -> None:
        delay = min(MAX_SQS_DELAY_S, max(0, math.ceil((at_ms - self._now()) / 1000)))
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({"claim_id": claim_id}),
                              DelaySeconds=delay)


def _first_device(ring: RingClient) -> str:
    devices = ring.devices()
    if not devices:
        raise RingError(404, "No devices", "the Ring token can see no devices")
    return devices[0].id


@lru_cache(maxsize=1)
def build() -> tuple[Desk, House, DynamoStore, dict]:
    env = os.environ
    session = boto3.Session()
    secrets = session.client("secretsmanager")
    house_secret = json.loads(secrets.get_secret_value(SecretId=env["HANDSHAKE_HOUSE_SECRET"])["SecretString"])

    store = DynamoStore(session.resource("dynamodb").Table(env["HANDSHAKE_TABLE"]), session.client("s3"),
                        env["HANDSHAKE_BUCKET"])
    ring = RingClient(SecretToken(secrets, env["HANDSHAKE_RING_TOKEN_SECRET"]))
    witness = NovaWitness(session.client("bedrock-runtime"), env.get("HANDSHAKE_WITNESS_MODEL") or DEFAULT_MODEL,
                          TextractOcr(session.client("textract")))
    house_id = house_secret["house_id"]
    config = HouseConfig(
        house_id=house_id,
        issuer=env.get("HANDSHAKE_PUBLIC_URL") or f"urn:handshake:house:{house_id}",
        device_id=env.get("HANDSHAKE_DEVICE_ID", ""),
        salt=house_secret["salt"],
    )
    house = House(config, ring, witness, HouseKey.from_pem(house_secret["signing_key_pem"].encode()), store,
                  device_resolver=lambda: _first_device(ring))
    desk = Desk(house, store, SqsScheduler(session.client("sqs"), env["HANDSHAKE_QUEUE_URL"]))
    return desk, house, store, house_secret


@lru_cache(maxsize=1)
def _mangum() -> Mangum:
    desk, house, store, secret = build()
    app = create_app(desk, house, store, secret.get("merchants", {}), secret.get("webhook_signing_key"))
    return Mangum(app, lifespan="off")


def handler(event, context):
    return _mangum()(event, context)


def worker_handler(event, context):
    """SQS batch: attempt each due claim; report failures so SQS retries only those."""
    desk = build()[0]
    failures = []
    for record in event.get("Records", []):
        try:
            desk.attempt(json.loads(record["body"])["claim_id"])
        except Exception:  # one bad claim must not sink the batch
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
