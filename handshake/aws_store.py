"""The deployed house's store: DynamoDB for records, S3 for evidence frames.

One table, keyed by `pk` and `sk`:

    CLAIM#<claim_id>                   CLAIM                 the claim record
    RECEIPT#<receipt_id>               RECEIPT               the receipt record
    ORDER#<merchant>#<order_id>        <issued_at>#<id>      receipts per order
    RECENT                             <issued_at>#<id>      receipts, newest first
    DEVICE#<device_id>                 STATUS#<at_ms>        online history
    WEBHOOK#<request_id>               SEEN                  webhook dedupe (TTL)

Frames go to s3://<bucket>/frames/<receipt_id>/<name>, encrypted at rest.
The bucket is private; frames never leave the house's account.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .desk import ClaimRecord, ClaimStatus
from .episode import Window
from .store import StoredReceipt, coverage_from_log
from .verdict import Coverage

WEBHOOK_TTL_S = 7 * 24 * 3600


def _ts(value: int) -> str:
    return f"{value:015d}"


class DynamoStore:
    def __init__(self, table, s3_client, bucket: str):
        self.table = table  # a boto3 DynamoDB Table resource
        self.s3 = s3_client
        self.bucket = bucket

    # -- claims --------------------------------------------------------------

    def put_claim(self, record: ClaimRecord) -> None:
        self.table.put_item(Item={"pk": f"CLAIM#{record.claim_id}", "sk": "CLAIM", "data": json.dumps(asdict(record))})

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        item = self.table.get_item(Key={"pk": f"CLAIM#{claim_id}", "sk": "CLAIM"}).get("Item")
        if not item:
            return None
        data = json.loads(item["data"])
        data["status"] = ClaimStatus(data["status"])
        return ClaimRecord(**data)

    # -- receipts ------------------------------------------------------------

    def put_receipt(self, receipt: StoredReceipt) -> None:
        order_sk = f"{_ts(receipt.issued_at_s)}#{receipt.receipt_id}"
        with self.table.batch_writer() as batch:
            batch.put_item(Item={"pk": f"RECEIPT#{receipt.receipt_id}", "sk": "RECEIPT",
                                 "data": json.dumps(asdict(receipt))})
            batch.put_item(Item={"pk": f"ORDER#{receipt.merchant}#{receipt.order_id}", "sk": order_sk,
                                 "receipt_id": receipt.receipt_id})
            batch.put_item(Item={"pk": "RECENT", "sk": order_sk, "receipt_id": receipt.receipt_id})

    def get_receipt(self, receipt_id: str) -> StoredReceipt | None:
        item = self.table.get_item(Key={"pk": f"RECEIPT#{receipt_id}", "sk": "RECEIPT"}).get("Item")
        return StoredReceipt(**json.loads(item["data"])) if item else None

    def _receipts_under(self, pk: str, newest_first: bool, limit: int | None) -> list[StoredReceipt]:
        kwargs = {"KeyConditionExpression": Key("pk").eq(pk), "ScanIndexForward": not newest_first}
        if limit:
            kwargs["Limit"] = limit
        ids = [item["receipt_id"] for item in self.table.query(**kwargs).get("Items", [])]
        return [r for r in (self.get_receipt(i) for i in ids) if r is not None]

    def receipts_for_order(self, merchant: str, order_id: str) -> list[StoredReceipt]:
        return self._receipts_under(f"ORDER#{merchant}#{order_id}", newest_first=False, limit=None)

    def recent_receipts(self, limit: int = 50) -> list[StoredReceipt]:
        return self._receipts_under("RECENT", newest_first=True, limit=limit)

    # -- frames --------------------------------------------------------------

    def put_frames(self, receipt_id: str, frames: dict[str, bytes]) -> None:
        for name, data in frames.items():
            self.s3.put_object(Bucket=self.bucket, Key=f"frames/{receipt_id}/{name}", Body=data,
                               ServerSideEncryption="AES256")

    def get_frames(self, receipt_id: str) -> dict[str, bytes]:
        prefix = f"frames/{receipt_id}/"
        listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix).get("Contents", [])
        return {
            obj["Key"][len(prefix):]: self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()
            for obj in listing
        }

    # -- camera online history -----------------------------------------------

    def record_status(self, device_id: str, at_ms: int, online: bool) -> None:
        self.table.put_item(Item={"pk": f"DEVICE#{device_id}", "sk": f"STATUS#{_ts(at_ms)}", "online": online})

    def coverage(self, device_id: str, window: Window, online_now: bool | None) -> Coverage:
        pk = Key("pk").eq(f"DEVICE#{device_id}")
        before = self.table.query(
            KeyConditionExpression=pk & Key("sk").between("STATUS#", f"STATUS#{_ts(window.start_ms)}"),
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
        inside = self.table.query(
            KeyConditionExpression=pk & Key("sk").between(f"STATUS#{_ts(window.start_ms + 1)}",
                                                          f"STATUS#{_ts(window.end_ms)}"),
        ).get("Items", [])
        log = [(int(item["sk"].split("#", 1)[1]), bool(item["online"])) for item in before + inside]
        return coverage_from_log(log, window, online_now)

    # -- webhook dedupe ------------------------------------------------------

    def first_sighting(self, request_id: str) -> bool:
        try:
            self.table.put_item(
                Item={"pk": f"WEBHOOK#{request_id}", "sk": "SEEN", "expires_at": int(time.time()) + WEBHOOK_TTL_S},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
