"""Create the house's secrets in AWS Secrets Manager (run once per house).

    countersign/house       JSON: house id, Ed25519 signing key, device salt,
                          webhook signing key, merchants {name: sha256(api key)},
                          resident_key_sha256
    countersign/ring-token  the Ring access token; starts as a placeholder

A demo merchant API key and the resident's key are generated and written to a
file outside the repo (~/.secrets/countersign.env). Only their SHA-256 goes into
the secret, and the keys themselves are never printed.

    python scripts/bootstrap_house.py [--region us-east-1] [--merchant demo-shop]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from countersign.api import hash_key  # noqa: E402
from countersign.receipt import HouseKey  # noqa: E402
from countersign.tokens import PLACEHOLDER  # noqa: E402

HOUSE_SECRET = "countersign/house"
TOKEN_SECRET = "countersign/ring-token"
LOCAL_KEYS = Path.home() / ".secrets" / "countersign.env"


def _arn_if_exists(sm, name: str) -> str | None:
    try:
        return sm.describe_secret(SecretId=name)["ARN"]
    except sm.exceptions.ResourceNotFoundException:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--merchant", default="demo-shop")
    args = parser.parse_args()
    sm = boto3.client("secretsmanager", region_name=args.region)

    house_arn = _arn_if_exists(sm, HOUSE_SECRET)
    if house_arn:
        print(f"{HOUSE_SECRET} already exists, leaving it alone")
    else:
        api_key = f"hsk_{args.merchant}_{secrets.token_urlsafe(24)}"
        resident_key = f"hsr_{secrets.token_urlsafe(24)}"
        house = {
            "house_id": f"house-{secrets.token_hex(4)}",
            "signing_key_pem": HouseKey.generate().to_pem().decode(),
            "salt": secrets.token_hex(16),
            "webhook_signing_key": secrets.token_hex(32),  # replace with Ring's key when the app is registered
            "merchants": {args.merchant: hash_key(api_key)},
            "resident_key_sha256": hash_key(resident_key),
        }
        house_arn = sm.create_secret(Name=HOUSE_SECRET, SecretString=json.dumps(house),
                                     Description="Countersign house keys and merchant key hashes")["ARN"]
        LOCAL_KEYS.parent.mkdir(parents=True, exist_ok=True)
        with LOCAL_KEYS.open("a", encoding="utf-8") as f:
            f.write(f"COUNTERSIGN_MERCHANT={args.merchant}\nCOUNTERSIGN_MERCHANT_KEY={api_key}\n"
                    f"COUNTERSIGN_RESIDENT_KEY={resident_key}\n")
        print(f"created {HOUSE_SECRET} for {house['house_id']}; merchant and resident keys saved to {LOCAL_KEYS}")

    token_arn = _arn_if_exists(sm, TOKEN_SECRET)
    if not token_arn:
        token_arn = sm.create_secret(Name=TOKEN_SECRET, SecretString=PLACEHOLDER,
                                     Description="Ring access token for the Countersign house")["ARN"]
        print(f"created {TOKEN_SECRET} (placeholder; set it with scripts/set_ring_token.py)")

    print(json.dumps({"HouseSecretArn": house_arn, "RingTokenSecretArn": token_arn}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
