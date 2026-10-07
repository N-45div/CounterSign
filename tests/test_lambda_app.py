import json
import time

import boto3
import pytest
from moto import mock_aws

from handshake import lambda_app
from handshake.api import hash_key
from handshake.receipt import HouseKey

MERCHANT_KEY = "hsk_shop_abc"


def url_event(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    """A Lambda Function URL (payload v2.0) event."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc.lambda-url.us-east-1.on.aws", "content-type": "application/json", **(headers or {})},
        "requestContext": {"http": {"method": method, "path": path, "sourceIp": "1.2.3.4", "protocol": "HTTP/1.1",
                                    "userAgent": "test"},
                           "domainName": "abc.lambda-url.us-east-1.on.aws", "stage": "$default", "requestId": "r",
                           "accountId": "123", "apiId": "abc", "time": "08/Oct/2026:10:00:00 +0000",
                           "timeEpoch": 0},
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def deployed(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing",
                 "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(TableName="hs", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                    {"AttributeName": "sk", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                               {"AttributeName": "sk", "AttributeType": "S"}])
        boto3.client("s3").create_bucket(Bucket="hs-frames")
        queue_url = boto3.client("sqs").create_queue(QueueName="hs-claims")["QueueUrl"]
        sm = boto3.client("secretsmanager")
        key = HouseKey.generate()
        sm.create_secret(Name="hs/house", SecretString=json.dumps({
            "house_id": "h1", "signing_key_pem": key.to_pem().decode(), "salt": "s",
            "webhook_signing_key": "wh", "merchants": {"shop": hash_key(MERCHANT_KEY)},
        }))
        sm.create_secret(Name="hs/ring-token", SecretString="not-set")
        for k, v in {"HANDSHAKE_TABLE": "hs", "HANDSHAKE_BUCKET": "hs-frames", "HANDSHAKE_QUEUE_URL": queue_url,
                     "HANDSHAKE_HOUSE_SECRET": "hs/house", "HANDSHAKE_RING_TOKEN_SECRET": "hs/ring-token",
                     "HANDSHAKE_PUBLIC_URL": "https://abc.lambda-url.us-east-1.on.aws"}.items():
            monkeypatch.setenv(k, v)
        lambda_app.build.cache_clear()
        lambda_app._mangum.cache_clear()
        yield key, queue_url
        lambda_app.build.cache_clear()
        lambda_app._mangum.cache_clear()


def test_the_function_url_serves_health_and_keys(deployed):
    key, _ = deployed
    health = lambda_app.handler(url_event("GET", "/healthz"), None)
    assert health["statusCode"] == 200 and json.loads(health["body"]) == {"ok": True, "house": "h1"}
    jwks = json.loads(lambda_app.handler(url_event("GET", "/.well-known/jwks.json"), None)["body"])
    assert jwks["keys"][0]["kid"] == key.kid


def claim(claimed_at_ms):
    return lambda_app.handler(url_event("POST", "/v1/claims", {"order_id": "ORD-1", "claimed_at": claimed_at_ms},
                                        {"authorization": f"Bearer {MERCHANT_KEY}"}), None)


def test_a_fresh_claim_is_accepted_and_queued(deployed):
    _, queue_url = deployed
    resp = claim(int(time.time() * 1000))
    assert resp["statusCode"] == 202 and json.loads(resp["body"])["status"] == "pending"
    attrs = boto3.client("sqs").get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessagesDelayed", "ApproximateNumberOfMessages"])
    counts = attrs["Attributes"]
    # delayed until Ring has settled (2 minutes), so not visible yet
    assert int(counts["ApproximateNumberOfMessagesDelayed"]) + int(counts["ApproximateNumberOfMessages"]) == 1


def test_with_no_ring_token_the_claim_stays_pending_and_says_why(deployed):
    resp = claim(int(time.time() * 1000) - 10 * 60_000)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 202 and body["last_error"] == "ring_token_expired"


def test_claims_from_long_ago_are_refused(deployed):
    assert claim(1_000)["statusCode"] == 422


def test_the_worker_reports_only_failed_messages(deployed):
    result = lambda_app.worker_handler({"Records": [
        {"messageId": "m1", "body": json.dumps({"claim_id": "clm_missing"})},
    ]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_scheduler_delay_values(monkeypatch):
    calls = []

    class FakeSqs:
        def send_message(self, **kw):
            calls.append(kw)

    sched = lambda_app.SqsScheduler(FakeSqs(), "q", clock_ms=lambda: 0)
    sched.schedule("a", 30 * 60_000)
    sched.schedule("b", 120_500)
    sched.schedule("c", -1)
    assert [c["DelaySeconds"] for c in calls] == [900, 121, 0]
