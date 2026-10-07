"""Put a Ring access token into the deployed house (Playground tokens last 30 minutes).

    python scripts/set_ring_token.py --token "eyJ..."
    python scripts/set_ring_token.py   # reads RING_TOKEN

The whole `export TOKEN="..."` line from the Playground also works. The token
is never printed. The house rereads it within a minute.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handshake.tokens import clean_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=os.environ.get("RING_TOKEN", ""))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--secret", default="handshake/ring-token")
    args = parser.parse_args()
    token = clean_token(args.token)
    if not token.startswith("eyJ") or token.count(".") != 2:
        print("That doesn't look like a Ring token (it starts with eyJ and has two dots).")
        return 2
    boto3.client("secretsmanager", region_name=args.region).put_secret_value(SecretId=args.secret, SecretString=token)
    print(f"updated {args.secret}; the house picks it up within a minute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
