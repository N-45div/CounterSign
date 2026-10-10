"""Create the hosted lab's secret in AWS Secrets Manager (run once).

    handshake/lab   JSON: signing_key_pem (the lab house's Ed25519 key), webhook_key

An existing secret is left alone, so receipts the lab has already issued
keep verifying. Prints the secret's ARN for scripts/deploy.py --lab-secret.

    python scripts/bootstrap_lab.py [--region us-east-1]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handshake.receipt import HouseKey  # noqa: E402

NAME = "handshake/lab"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    sm = boto3.client("secretsmanager", region_name=args.region)
    try:
        arn = sm.describe_secret(SecretId=NAME)["ARN"]
        print(f"{NAME} already exists, leaving it alone")
    except sm.exceptions.ResourceNotFoundException:
        value = {"signing_key_pem": HouseKey.generate().to_pem().decode(), "webhook_key": secrets.token_hex(32)}
        arn = sm.create_secret(Name=NAME, SecretString=json.dumps(value),
                               Description="Handshake Doorstep Lab house key and webhook key")["ARN"]
        print(f"created {NAME}")
    print(json.dumps({"LabSecretArn": arn}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
