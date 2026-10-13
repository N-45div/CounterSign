"""Give the house a resident key, which turns on the resident's question endpoints.

A new key is generated and appended to a file outside the repo
(~/.secrets/handshake.env); only its SHA-256 goes into the house secret, and
the key itself is never printed. The API picks it up on its next cold start
(a redeploy forces one).

    python scripts/set_resident_key.py [--region us-east-1] [--rotate]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handshake.api import hash_key  # noqa: E402

HOUSE_SECRET = "handshake/house"
LOCAL_KEYS = Path.home() / ".secrets" / "handshake.env"


def new_resident_key() -> str:
    return f"hsr_{secrets.token_urlsafe(24)}"


def save_locally(key: str) -> None:
    LOCAL_KEYS.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_KEYS.open("a", encoding="utf-8") as f:
        f.write(f"HANDSHAKE_RESIDENT_KEY={key}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--rotate", action="store_true", help="replace an existing resident key")
    args = parser.parse_args()
    sm = boto3.client("secretsmanager", region_name=args.region)
    house = json.loads(sm.get_secret_value(SecretId=HOUSE_SECRET)["SecretString"])
    if house.get("resident_key_sha256") and not args.rotate:
        print("the house already has a resident key; pass --rotate to replace it")
        return 1
    key = new_resident_key()
    house["resident_key_sha256"] = hash_key(key)
    sm.put_secret_value(SecretId=HOUSE_SECRET, SecretString=json.dumps(house))
    save_locally(key)
    print(f"resident key set on {HOUSE_SECRET}; saved to {LOCAL_KEYS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
