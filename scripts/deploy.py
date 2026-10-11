"""Build and deploy the house with CloudFormation (no SAM CLI needed).

    python scripts/deploy.py --house-secret <arn> --ring-token-secret <arn> [--public-url URL]

Steps: build the Lambda bundle, make sure an artifact bucket exists, then
`aws cloudformation package` and `aws cloudformation deploy` the SAM template.
Prints the stack outputs, including the API URL.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
STACK = "handshake"


def run(*cmd: str) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def artifact_bucket(region: str) -> str:
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    name = f"handshake-artifacts-{account}-{region}"
    s3 = boto3.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=name)
    except s3.exceptions.ClientError:
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=name, **kwargs)
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    return name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--house-secret", required=True)
    parser.add_argument("--ring-token-secret", required=True)
    parser.add_argument("--lab-secret", required=True)
    parser.add_argument("--public-url", default="")
    parser.add_argument("--device-id", default="")
    parser.add_argument("--witness-model", default="us.amazon.nova-2-lite-v1:0")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--skip-build", action="store_true", help="reuse build/lambda from a previous build")
    args = parser.parse_args()

    if not args.skip_build or not (ROOT / "build" / "lambda" / "handshake").exists():
        run(sys.executable, "scripts/build_lambda.py")
    bucket = artifact_bucket(args.region)
    run("aws", "cloudformation", "package", "--region", args.region, "--template-file", "infra/template.yaml",
        "--s3-bucket", bucket, "--s3-prefix", STACK, "--output-template-file", "build/packaged.yaml")
    run("aws", "cloudformation", "deploy", "--region", args.region, "--template-file", "build/packaged.yaml",
        "--stack-name", STACK, "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND",
        "--no-fail-on-empty-changeset", "--parameter-overrides",
        f"HouseSecretArn={args.house_secret}", f"RingTokenSecretArn={args.ring_token_secret}",
        f"PublicUrl={args.public_url}", f"DeviceId={args.device_id}", f"WitnessModel={args.witness_model}",
        f"LabSecretArn={args.lab_secret}")

    stack = boto3.client("cloudformation", region_name=args.region).describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    print(json.dumps(outputs, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
