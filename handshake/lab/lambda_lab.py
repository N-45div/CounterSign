"""AWS Lambda entry point for the hosted Doorstep Lab (behind a Function URL).

Sessions and the daily Nova budget live in the house's DynamoDB table (the
lab house's records under LAB#, apart from the real house's), lab frames and
evidence in its S3 bucket under lab/ (kept for a day), and the lab house's
signing key in Secrets Manager. The house reaches the Ring emulator through the
same Function URL the visitor used, with the session's token. The resident's
question and reply go to the same dispute agent on AgentCore Runtime as the
real house's.

    HANDSHAKE_TABLE, HANDSHAKE_BUCKET   shared with the house stack
    HANDSHAKE_LAB_SECRET                JSON: signing_key_pem, webhook_key
    HANDSHAKE_WITNESS_MODEL             optional Bedrock model id
    HANDSHAKE_LAB_DAILY_RUNS            Nova-judged runs per day before falling back to ground truth
    HANDSHAKE_AGENT_RUNTIME_ARN         optional dispute agent on AgentCore Runtime
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3
from mangum import Mangum

from ..aws_store import DynamoStore
from ..dispute_agent import RemoteDisputeAgent, agentcore_invoker
from ..doorman import TextractOcr
from ..receipt import HouseKey
from ..witness import DEFAULT_MODEL, NovaWitness
from .server import create_lab
from .sessions import DynamoSessions

AGENT_LABEL = "Strands agent on Amazon Bedrock AgentCore Runtime"


@lru_cache(maxsize=1)
def _app() -> Mangum:
    env = os.environ
    aws = boto3.Session()
    secret = json.loads(aws.client("secretsmanager").get_secret_value(SecretId=env["HANDSHAKE_LAB_SECRET"])["SecretString"])
    table = aws.resource("dynamodb").Table(env["HANDSHAKE_TABLE"])
    s3 = aws.client("s3")
    bucket = env["HANDSHAKE_BUCKET"]
    runtime, textract = aws.client("bedrock-runtime"), aws.client("textract")
    model = env.get("HANDSHAKE_WITNESS_MODEL") or DEFAULT_MODEL
    agent = None
    if arn := env.get("HANDSHAKE_AGENT_RUNTIME_ARN"):
        invoke = agentcore_invoker(aws.client("bedrock-agentcore"), arn)
        agent_model = env.get("HANDSHAKE_AGENT_MODEL") or DEFAULT_MODEL
        agent = lambda disputes: RemoteDisputeAgent(invoke, disputes, agent_model)  # noqa: E731
    app = create_lab(
        sessions=DynamoSessions(table, s3, bucket, int(env.get("HANDSHAKE_LAB_DAILY_RUNS", "300"))),
        house_store=DynamoStore(table, s3, bucket, namespace="LAB#", frames_prefix="lab/evidence/"),
        key=HouseKey.from_pem(secret["signing_key_pem"].encode()),
        webhook_key=secret["webhook_key"],
        nova=lambda: NovaWitness(runtime, model, TextractOcr(textract)),
        base_url=None,  # the URL the visitor came in on
        dispute_agent=agent,
        agent_label=AGENT_LABEL,
    )
    return Mangum(app, lifespan="off")


def handler(event, context):
    return _app()(event, context)
