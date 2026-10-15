"""Deploy the dispute agent to Amazon Bedrock AgentCore Runtime (direct code deployment).

Uploads build/agent.zip, makes sure the runtime's IAM role exists (it may only
invoke Amazon Nova and write its logs), then creates or updates the runtime and
waits until it is READY. Prints the runtime ARN for the house to call.

    python scripts/build_agent.py && python scripts/deploy_agent.py [--model us.amazon.nova-2-lite-v1:0]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
NAME = "countersign_dispute_agent"
ROLE = "countersign-dispute-agent-runtime"


def ensure_role(iam, account: str, region: str) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "Nova", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                      f"arn:aws:bedrock:{region}:{account}:inference-profile/us.amazon.nova-*"]},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams",
                    "logs:DescribeLogGroups"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"},
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        {"Sid": "Traces", "Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
         "Resource": "*"},
    ]}
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Countersign dispute agent on AgentCore: Nova and logs only")["Role"]["Arn"]
        time.sleep(10)  # let the new role propagate before AgentCore assumes it
    iam.put_role_policy(RoleName=ROLE, PolicyName="nova-and-logs", PolicyDocument=json.dumps(policy))
    return arn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--model", default="us.amazon.nova-2-lite-v1:0")
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"countersign-artifacts-{account}-{args.region}"
    key = f"agentcore/agent-{int(time.time())}.zip"
    session.client("s3").upload_file(str(ROOT / "build" / "agent.zip"), bucket, key)
    role = ensure_role(session.client("iam"), account, args.region)

    control = session.client("bedrock-agentcore-control")
    spec = {
        "agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": bucket, "prefix": key}},
            "runtime": "PYTHON_3_12", "entryPoint": ["app.py"]}},
        "roleArn": role,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "environmentVariables": {"COUNTERSIGN_AGENT_MODEL": args.model},
        "description": "Countersign dispute agent: proposes resident questions and reads replies. Holds no keys.",
    }
    existing = [r for r in control.list_agent_runtimes().get("agentRuntimes", []) if r["agentRuntimeName"] == NAME]
    if existing:
        runtime_id = existing[0]["agentRuntimeId"]
        control.update_agent_runtime(agentRuntimeId=runtime_id, **spec)
    else:
        runtime_id = control.create_agent_runtime(agentRuntimeName=NAME, **spec)["agentRuntimeId"]

    for _ in range(60):
        info = control.get_agent_runtime(agentRuntimeId=runtime_id)
        if info["status"] in ("READY", "CREATE_FAILED", "UPDATE_FAILED"):
            break
        time.sleep(5)
    print(json.dumps({"status": info["status"], "agentRuntimeArn": info["agentRuntimeArn"],
                      "version": info.get("agentRuntimeVersion"), "reason": info.get("failureReason")}, indent=2))
    return 0 if info["status"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
