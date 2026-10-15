import json
import time

import boto3
import pytest
from moto import mock_aws

from countersign import lambda_app
from countersign.api import hash_key
from countersign.receipt import HouseKey

MERCHANT_KEY = "hsk_shop_abc"
RESIDENT_KEY = "hsr_resident_abc"


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
            "resident_key_sha256": hash_key(RESIDENT_KEY),
        }))
        sm.create_secret(Name="hs/ring-token", SecretString="not-set")
        for k, v in {"COUNTERSIGN_TABLE": "hs", "COUNTERSIGN_BUCKET": "hs-frames", "COUNTERSIGN_QUEUE_URL": queue_url,
                     "COUNTERSIGN_HOUSE_SECRET": "hs/house", "COUNTERSIGN_RING_TOKEN_SECRET": "hs/ring-token",
                     "COUNTERSIGN_PUBLIC_URL": "https://abc.lambda-url.us-east-1.on.aws"}.items():
            monkeypatch.setenv(k, v)
        clear_caches()
        yield key, queue_url
        clear_caches()


def clear_caches():
    for cached in (lambda_app.build, lambda_app._mangum, lambda_app.dispute_agent):
        cached.cache_clear()


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


def inconclusive_claim(key, claim_id="clm_7"):
    from countersign.desk import ClaimRecord, ClaimStatus
    from countersign.receipt import issue

    now = int(time.time() * 1000)
    token = issue(key, {"verdict": "inconclusive", "reason": "camera_not_recording", "visit": None},
                  issuer="https://abc.lambda-url.us-east-1.on.aws", issued_at_s=now // 1000, receipt_id="r_7")
    record = ClaimRecord(claim_id, "shop", "ORD-7", now, now, carrier="Delhivery", status=ClaimStatus.ISSUED,
                         receipt_id="r_7", verdict="inconclusive", reason="camera_not_recording", token=token)
    lambda_app.build()[2].put_claim(record)
    return record


def fake_agentcore(monkeypatch, up=True):
    """Stands in for the dispute agent on AgentCore Runtime."""
    payloads = []

    def invoker(client, arn):
        def invoke(payload):
            payloads.append(payload)
            if not up:
                raise ConnectionError("AgentCore unavailable")
            if payload["task"] == "question":
                return {"question": f"Did your Delhivery parcel for {payload['facts']['order_id']} arrive?"}
            return {"interpretation": {"outcome": "not_received", "quote": payload["exchange"]["reply"], "note": ""}}
        return invoke

    monkeypatch.setenv("COUNTERSIGN_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123:runtime/agent-x")
    monkeypatch.setattr(lambda_app, "agentcore_invoker", invoker)
    lambda_app.dispute_agent.cache_clear()
    return payloads


def worker(*tasks):
    return lambda_app.worker_handler({"Records": [
        {"messageId": f"m{i}", "body": json.dumps(t)} for i, t in enumerate(tasks)]}, None)


def resident(method, path, body=None):
    resp = lambda_app.handler(url_event(method, path, body, {"authorization": f"Bearer {RESIDENT_KEY}"}), None)
    return resp["statusCode"], json.loads(resp["body"])


def test_only_inconclusive_receipts_are_sent_to_the_resident():
    calls = []

    class FakeSqs:
        def send_message(self, **kw):
            calls.append(json.loads(kw["MessageBody"]))

    queue = lambda_app.SqsScheduler(FakeSqs(), "q")
    for verdict in ("delivered", "not_observed", "inconclusive"):
        lambda_app.ask_later(queue, type("R", (), {"verdict": verdict, "claim_id": f"clm_{verdict}"})())
    assert calls == [{"task": "ask", "claim_id": "clm_inconclusive"}]


def test_without_the_agent_the_worker_asks_with_the_template(deployed):
    key, _ = deployed
    inconclusive_claim(key)
    assert worker({"task": "ask", "claim_id": "clm_7"}) == {"batchItemFailures": []}
    question = lambda_app.build()[2].question_for_claim("clm_7")
    assert question.text.startswith("Did your Delhivery parcel from shop (order ORD-7)")


def test_the_agent_asks_and_a_resident_answer_is_signed_while_they_wait(deployed, monkeypatch):
    key, _ = deployed
    payloads = fake_agentcore(monkeypatch)
    inconclusive_claim(key)
    assert worker({"task": "ask", "claim_id": "clm_7"}) == {"batchItemFailures": []}
    status, body = resident("GET", "/v1/resident/questions")
    [question] = body["questions"]
    assert status == 200 and question["text"] == "Did your Delhivery parcel for ORD-7 arrive?"

    status, body = resident("POST", f"/v1/resident/questions/{question['question_id']}/answer",
                            {"reply": "No, nothing came"})
    assert status == 200 and body["status"] == "resolved" and body["outcome"] == "not_received"
    assert [p["task"] for p in payloads] == ["question", "interpret"]
    assert "Delhivery" in json.dumps(payloads[0]["facts"]) and "r_7" not in json.dumps(payloads)

    merchant = lambda_app.handler(url_event("GET", "/v1/claims/clm_7", None,
                                            {"authorization": f"Bearer {MERCHANT_KEY}"}), None)
    dispute = json.loads(merchant["body"])["dispute"]
    assert dispute["outcome"] == "not_received" and dispute["resolution"].count(".") == 2


def test_when_the_agent_is_down_the_reply_waits_for_the_worker(deployed, monkeypatch):
    key, queue_url = deployed
    fake_agentcore(monkeypatch, up=False)
    inconclusive_claim(key)
    worker({"task": "ask", "claim_id": "clm_7"})
    qid = lambda_app.build()[2].question_for_claim("clm_7").question_id
    status, body = resident("POST", f"/v1/resident/questions/{qid}/answer", {"reply": "not sure"})
    assert status == 202 and body["status"] == "answered"
    counts = boto3.client("sqs").get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessagesDelayed"])["Attributes"]
    assert counts["ApproximateNumberOfMessagesDelayed"] == "1"
    assert worker({"task": "interpret", "question_id": qid}) == {"batchItemFailures": [{"itemIdentifier": "m0"}]}

    fake_agentcore(monkeypatch, up=True)
    assert worker({"task": "interpret", "question_id": qid}) == {"batchItemFailures": []}
    assert lambda_app.build()[2].get_question(qid).status == "resolved"
