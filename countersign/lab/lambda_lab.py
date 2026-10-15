"""AWS Lambda entry point for the hosted Doorstep Lab (behind a Function URL).

Sessions and the daily Nova budget live in the house's DynamoDB table (the
lab house's records under LAB#, apart from the real house's), lab frames and
evidence in its S3 bucket under lab/ (kept for a day), and the lab house's
signing key in Secrets Manager. The house reaches the Ring emulator through the
same Function URL the visitor used, with the session's token. The resident's
question and reply go to the same dispute agent on AgentCore Runtime as the
real house's.

    COUNTERSIGN_TABLE, COUNTERSIGN_BUCKET   shared with the house stack
    COUNTERSIGN_LAB_SECRET                JSON: signing_key_pem, webhook_key
    COUNTERSIGN_WITNESS_MODEL             optional Bedrock model id
    COUNTERSIGN_LAB_DAILY_RUNS            Nova-judged runs per day before falling back to ground truth
    COUNTERSIGN_AGENT_RUNTIME_ARN         optional dispute agent on AgentCore Runtime
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
    secret = json.loads(aws.client("secretsmanager").get_secret_value(SecretId=env["COUNTERSIGN_LAB_SECRET"])["SecretString"])
    table = aws.resource("dynamodb").Table(env["COUNTERSIGN_TABLE"])
    s3 = aws.client("s3")
    bucket = env["COUNTERSIGN_BUCKET"]
    runtime, textract = aws.client("bedrock-runtime"), aws.client("textract")
    model = env.get("COUNTERSIGN_WITNESS_MODEL") or DEFAULT_MODEL
    agent = None
    if arn := env.get("COUNTERSIGN_AGENT_RUNTIME_ARN"):
        invoke = agentcore_invoker(aws.client("bedrock-agentcore"), arn)
        agent_model = env.get("COUNTERSIGN_AGENT_MODEL") or DEFAULT_MODEL
        agent = lambda disputes: RemoteDisputeAgent(invoke, disputes, agent_model)  # noqa: E731
    app = create_lab(
        sessions=DynamoSessions(table, s3, bucket, int(env.get("COUNTERSIGN_LAB_DAILY_RUNS", "300"))),
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
