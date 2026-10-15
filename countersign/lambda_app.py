"""AWS Lambda entry points for the deployed house.

    handler         the HTTP API behind a Lambda Function URL (via Mangum)
    worker_handler  drains the SQS queue: claims due for another attempt, and
                    dispute tasks (ask the resident, read their reply)

Configuration comes from environment variables set by the SAM template, and
secrets from AWS Secrets Manager:

    COUNTERSIGN_TABLE              DynamoDB table
    COUNTERSIGN_BUCKET             private S3 bucket for evidence frames
    COUNTERSIGN_QUEUE_URL          SQS queue for scheduled attempts
    COUNTERSIGN_HOUSE_SECRET       JSON: house_id, signing_key_pem, salt,
                                 webhook_signing_key, merchants {name: sha256}
    COUNTERSIGN_RING_TOKEN_SECRET  the Ring access token (Playground tokens last 30 min)
    COUNTERSIGN_PUBLIC_URL         the house's public URL, used as receipt issuer
    COUNTERSIGN_DEVICE_ID          optional; otherwise the first Ring device
    COUNTERSIGN_WITNESS_MODEL      optional Bedrock model id for the witness
    COUNTERSIGN_AGENT_RUNTIME_ARN  the dispute agent on AgentCore Runtime; without
                                 it residents get a template question and replies
                                 wait for the agent
    COUNTERSIGN_AGENT_MODEL        the model the dispute agent runs, named in resolutions
    COUNTERSIGN_TIMEZONE           the household's IANA timezone, for days in questions (UTC)

The house secret may also hold resident_key_sha256, which turns on the
resident's question endpoints.
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
from .desk import ClaimRecord, Desk
from .dispute_agent import RemoteDisputeAgent, agentcore_invoker
from .disputes import Disputes
from .doorman import TextractOcr
from .house import House, HouseConfig
from .receipt import HouseKey
from .ring import RingClient, RingError
from .tokens import SecretToken
from .witness import DEFAULT_MODEL, NovaWitness

MAX_SQS_DELAY_S = 900
INTERPRET_RETRY_S = 60
DEFAULT_AGENT_MODEL = "us.amazon.nova-2-lite-v1:0"


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
        self.enqueue({"claim_id": claim_id}, math.ceil((at_ms - self._now()) / 1000))

    def enqueue(self, task: dict, delay_s: int = 0) -> None:
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(task),
                              DelaySeconds=min(MAX_SQS_DELAY_S, max(0, delay_s)))


def ask_later(queue: SqsScheduler, record: ClaimRecord) -> None:
    """An inconclusive receipt goes to the resident; the worker asks."""
    if record.verdict == "inconclusive":
        queue.enqueue({"task": "ask", "claim_id": record.claim_id})


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
    house_secret = json.loads(secrets.get_secret_value(SecretId=env["COUNTERSIGN_HOUSE_SECRET"])["SecretString"])

    store = DynamoStore(session.resource("dynamodb").Table(env["COUNTERSIGN_TABLE"]), session.client("s3"),
                        env["COUNTERSIGN_BUCKET"])
    ring = RingClient(SecretToken(secrets, env["COUNTERSIGN_RING_TOKEN_SECRET"]))
    witness = NovaWitness(session.client("bedrock-runtime"), env.get("COUNTERSIGN_WITNESS_MODEL") or DEFAULT_MODEL,
                          TextractOcr(session.client("textract")))
    house_id = house_secret["house_id"]
    config = HouseConfig(
        house_id=house_id,
        issuer=env.get("COUNTERSIGN_PUBLIC_URL") or f"urn:countersign:house:{house_id}",
        device_id=env.get("COUNTERSIGN_DEVICE_ID", ""),
        salt=house_secret["salt"],
    )
    house = House(config, ring, witness, HouseKey.from_pem(house_secret["signing_key_pem"].encode()), store,
                  device_resolver=lambda: _first_device(ring))
    queue = SqsScheduler(session.client("sqs"), env["COUNTERSIGN_QUEUE_URL"])
    desk = Desk(house, store, queue, on_issued=lambda record: ask_later(queue, record))
    return desk, house, store, house_secret


def _no_agent(payload: dict) -> dict:
    raise RuntimeError("COUNTERSIGN_AGENT_RUNTIME_ARN is not set")


@lru_cache(maxsize=1)
def dispute_agent() -> RemoteDisputeAgent:
    desk, house, store, _ = build()
    arn = os.environ.get("COUNTERSIGN_AGENT_RUNTIME_ARN", "")
    invoke = agentcore_invoker(boto3.client("bedrock-agentcore"), arn) if arn else _no_agent
    return RemoteDisputeAgent(invoke, Disputes(store, house.key, house.config.issuer),
                              os.environ.get("COUNTERSIGN_AGENT_MODEL") or DEFAULT_AGENT_MODEL)


def interpret_now_or_later(question_id: str) -> None:
    """Read the reply while the resident waits; if the agent is not there, the worker retries."""
    question = dispute_agent().interpret(question_id)
    if question is not None and question.status == "answered":
        desk = build()[0]
        desk.scheduler.enqueue({"task": "interpret", "question_id": question_id}, INTERPRET_RETRY_S)


@lru_cache(maxsize=1)
def _mangum() -> Mangum:
    desk, house, store, secret = build()
    app = create_app(desk, house, store, secret.get("merchants", {}), secret.get("webhook_signing_key"),
                     disputes=dispute_agent().disputes, resident_key_hash=secret.get("resident_key_sha256"),
                     interpret_reply=interpret_now_or_later)
    return Mangum(app, lifespan="off")


def handler(event, context):
    return _mangum()(event, context)


def run_task(task: dict) -> None:
    kind = task.get("task", "attempt")
    if kind == "ask":
        claim = build()[2].get_claim(task["claim_id"])
        if claim is None:
            raise KeyError(task["claim_id"])
        dispute_agent().ask(claim, os.environ.get("COUNTERSIGN_TIMEZONE"))
    elif kind == "interpret":
        question = dispute_agent().interpret(task["question_id"])
        if question is not None and question.status == "answered":
            raise RuntimeError("no interpretation backed by the reply yet")  # SQS retries, then the DLQ
    else:
        build()[0].attempt(task["claim_id"])


def worker_handler(event, context):
    """SQS batch: run each task; report failures so SQS retries only those."""
    failures = []
    for record in event.get("Records", []):
        try:
            run_task(json.loads(record["body"]))
        except Exception:  # one bad task must not sink the batch
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
