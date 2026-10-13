import json

import boto3
import pytest
from moto import mock_aws

from handshake.lab import lambda_lab
from handshake.receipt import HouseKey

from .test_lambda_app import url_event


@pytest.fixture
def hosted(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.resource("dynamodb").create_table(
            TableName="hs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        boto3.client("s3").create_bucket(Bucket="frames")
        key = HouseKey.generate()
        boto3.client("secretsmanager").create_secret(
            Name="handshake/lab", SecretString=json.dumps({"signing_key_pem": key.to_pem().decode(), "webhook_key": "wk"}))
        for k, v in {"HANDSHAKE_TABLE": "hs", "HANDSHAKE_BUCKET": "frames", "HANDSHAKE_LAB_SECRET": "handshake/lab"}.items():
            monkeypatch.setenv(k, v)
        lambda_lab._app.cache_clear()
        yield key
        lambda_lab._app.cache_clear()


def call(event):
    resp = lambda_lab.handler(event, None)
    return resp["statusCode"], resp


def test_the_hosted_lab_serves_config_page_and_its_key(hosted):
    status, resp = call(url_event("GET", "/lab/config"))
    assert status == 200 and "Nova" in json.loads(resp["body"])["witness"]
    status, resp = call(url_event("GET", "/"))
    assert status == 200 and "Doorstep Lab" in resp["body"]
    status, resp = call(url_event("GET", "/lab/jwks.json"))
    assert json.loads(resp["body"])["keys"][0]["kid"] == hosted.kid


def test_a_hosted_session_is_stored_and_its_ring_token_works(hosted):
    status, resp = call(url_event("POST", "/lab/start", {"scenario": "camera_offline"}))
    tl = json.loads(resp["body"])
    assert status == 200 and tl["webhooks_sent"] == 2
    status, resp = call(url_event("GET", "/v1/devices", headers={"authorization": f"Bearer lab_{tl['session']}"}))
    assert status == 200 and json.loads(resp["body"])["data"][0]["id"] == tl["device_id"]
    status, _ = call(url_event("GET", "/v1/devices", headers={"authorization": "Bearer lab_unknown"}))
    assert status == 401


def test_the_hosted_lab_uses_the_agentcore_agent_when_configured(hosted, monkeypatch):
    status, resp = call(url_event("GET", "/lab/config"))
    assert json.loads(resp["body"])["dispute_agent"] is None
    monkeypatch.setenv("HANDSHAKE_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x")
    lambda_lab._app.cache_clear()
    status, resp = call(url_event("GET", "/lab/config"))
    assert json.loads(resp["body"])["dispute_agent"] == lambda_lab.AGENT_LABEL
