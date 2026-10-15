"""Lab sessions: one scenario run per visitor, so several people can use the hosted lab at once.

A session is just (scenario, start time): the world is deterministic, so it is
rebuilt from those two values on every request. That is what lets the lab run
on Lambda, where no two requests are promised the same process. Each session
has its own Ring device id and its own Ring token, the way each Ring user's
token only sees that user's doorbell, and its own uploaded frames.

    MemorySessions   one process, for the local lab
    DynamoSessions   DynamoDB for sessions and the daily Nova budget, S3 for frames
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, MutableMapping

from botocore.exceptions import ClientError

from .emulator import LabDevice, LabWorld
from .scenarios import SCENARIOS

SESSION_TTL_S = 24 * 3600
TOKEN_PREFIX = "lab_"


@dataclass(frozen=True)
class LabSession:
    sid: str
    scenario: str
    start_ms: int
    created_ms: int

    @property
    def token(self) -> str:
        return TOKEN_PREFIX + self.sid

    @property
    def device(self) -> LabDevice:
        tag = self.sid.upper()
        return LabDevice(id=f"ava1.ring.device.LAB{tag}", account_id=f"ava1.ring.account.LAB{tag}")


def sid_from_token(token: str) -> str | None:
    return token[len(TOKEN_PREFIX):] if token.startswith(TOKEN_PREFIX) else None


def start_in_the_past(scenario: str, now_ms: int) -> int:
    """Far enough back that the claim window has closed and Ring has settled."""
    return now_ms - int((SCENARIOS[scenario].duration_s + 900) * 1000)


class _Sessions:
    clock_ms: Callable[[], int]

    def get(self, sid: str) -> LabSession | None: ...  # pragma: no cover
    def frames(self, sid: str) -> MutableMapping[int, bytes]: ...  # pragma: no cover

    def world(self, sid: str) -> LabWorld | None:
        session = self.get(sid)
        if session is None:
            return None
        return LabWorld(SCENARIOS[session.scenario], session.start_ms, device=session.device,
                        clock_ms=self.clock_ms, uploaded=self.frames(sid))

    def world_for_token(self, token: str) -> LabWorld | None:
        sid = sid_from_token(token)
        return self.world(sid) if sid else None


class MemorySessions(_Sessions):
    def __init__(self, max_sessions: int = 40, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.max_sessions = max_sessions
        self.clock_ms = clock_ms
        self._sessions: OrderedDict[str, LabSession] = OrderedDict()
        self._frames: dict[str, dict[int, bytes]] = {}

    def create(self, scenario: str) -> LabSession:
        now = self.clock_ms()
        session = LabSession(secrets.token_hex(8), scenario, start_in_the_past(scenario, now), now)
        self._sessions[session.sid] = session
        self._frames[session.sid] = {}
        while len(self._sessions) > self.max_sessions:  # oldest go first
            old, _ = self._sessions.popitem(last=False)
            self._frames.pop(old, None)
        return session

    def get(self, sid: str) -> LabSession | None:
        return self._sessions.get(sid)

    def frames(self, sid: str) -> MutableMapping[int, bytes]:
        return self._frames.setdefault(sid, {})

    def take_witness_run(self) -> bool:
        return True  # no budget locally


class S3Frames(MutableMapping):
    """Frames for one session as S3 objects: lab/<sid>/frames/<at_ms>.jpg"""

    def __init__(self, s3, bucket: str, sid: str):
        self.s3, self.bucket, self.prefix = s3, bucket, f"lab/{sid}/frames/"

    def __getitem__(self, at_ms: int) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}{at_ms}.jpg")["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(at_ms) from exc
            raise

    def get(self, at_ms, default=None):
        try:
            return self[at_ms]
        except KeyError:
            return default

    def __setitem__(self, at_ms: int, image: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}{at_ms}.jpg", Body=image,
                           ContentType="image/jpeg", ServerSideEncryption="AES256")

    def __delitem__(self, at_ms: int) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=f"{self.prefix}{at_ms}.jpg")

    def __iter__(self):
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                yield int(obj["Key"].rsplit("/", 1)[1].split(".")[0])

    def __len__(self) -> int:
        return sum(1 for _ in self)


class DynamoSessions(_Sessions):
    def __init__(self, table, s3, bucket: str, daily_witness_runs: int = 300,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.table, self.s3, self.bucket = table, s3, bucket
        self.daily_witness_runs = daily_witness_runs
        self.clock_ms = clock_ms

    def create(self, scenario: str) -> LabSession:
        now = self.clock_ms()
        session = LabSession(secrets.token_hex(8), scenario, start_in_the_past(scenario, now), now)
        self.table.put_item(Item={
            "pk": f"LABSESSION#{session.sid}", "sk": "SESSION", "scenario": scenario,
            "start_ms": session.start_ms, "created_ms": now, "expires_at": now // 1000 + SESSION_TTL_S,
        })
        return session

    def get(self, sid: str) -> LabSession | None:
        if not sid or not sid.isalnum():
            return None
        item = self.table.get_item(Key={"pk": f"LABSESSION#{sid}", "sk": "SESSION"}).get("Item")
        if not item or item["scenario"] not in SCENARIOS:
            return None
        return LabSession(sid, item["scenario"], int(item["start_ms"]), int(item["created_ms"]))

    def frames(self, sid: str) -> MutableMapping[int, bytes]:
        return S3Frames(self.s3, self.bucket, sid)

    def take_witness_run(self) -> bool:
        """Count one Nova run against today's budget; False once it is spent."""
        day = datetime.fromtimestamp(self.clock_ms() / 1000, timezone.utc).strftime("%Y-%m-%d")
        try:
            self.table.update_item(
                Key={"pk": f"LABBUDGET#{day}", "sk": "BUDGET"},
                UpdateExpression="ADD runs :one SET expires_at = if_not_exists(expires_at, :exp)",
                ConditionExpression="attribute_not_exists(runs) OR runs < :limit",
                ExpressionAttributeValues={":one": 1, ":limit": self.daily_witness_runs,
                                           ":exp": self.clock_ms() // 1000 + 3 * 24 * 3600},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
