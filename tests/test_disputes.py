import hashlib

import boto3
import pytest
from moto import mock_aws

from handshake.aws_store import DynamoStore
from handshake.desk import ClaimRecord, ClaimStatus
from handshake.disputes import Disputes, DisputeError
from handshake.receipt import HouseKey, verify
from handshake.store import MemoryStore

KEY = HouseKey.generate()


def claim(verdict="inconclusive", claim_id="clm_1"):
    return ClaimRecord(claim_id, "shop", "ORD-402", 1_000, 900, carrier="Delhivery", status=ClaimStatus.ISSUED,
                       receipt_id="r_original", verdict=verdict, reason="camera_not_recording", token="jws")


def disputes(store=None, now=10_000):
    ids = iter(f"{i:032x}" for i in range(100))
    return Disputes(store or MemoryStore(), KEY, "https://house.example", clock_ms=lambda: now, new_id=lambda: next(ids))


def test_only_inconclusive_claims_go_to_the_resident():
    with pytest.raises(DisputeError):
        disputes().open(claim("delivered"), "Did it arrive?")


def test_one_question_per_claim():
    d = disputes()
    first = d.open(claim(), "Did your Delhivery parcel for order ORD-402 arrive?")
    again = d.open(claim(), "Something else?")
    assert again.question_id == first.question_id and again.text == first.text
    assert [q.question_id for q in d.store.open_questions()] == [first.question_id]


def test_a_resolution_needs_the_residents_own_words():
    d = disputes()
    q = d.open(claim(), "Did your parcel for order ORD-402 arrive?")
    d.answer(q.question_id, "haan mil gaya, guard ke paas tha")
    with pytest.raises(DisputeError, match="actually wrote"):
        d.resolve(q.question_id, "received", "yes I received it", "us.amazon.nova-2-lite-v1:0")
    with pytest.raises(DisputeError, match="outcome"):
        d.resolve(q.question_id, "maybe", "mil gaya", "m")
    done = d.resolve(q.question_id, "received", "Mil  gaya", "us.amazon.nova-2-lite-v1:0", "left with the guard")
    assert done.status == "resolved" and done.outcome == "received"
    assert d.store.open_questions() == []


def test_the_resolution_is_signed_and_points_at_the_original():
    d = disputes()
    q = d.open(claim(), "Did your parcel for order ORD-402 arrive?")
    reply = "No, nothing came today."
    d.answer(q.question_id, reply)
    done = d.resolve(q.question_id, "not_received", "nothing came", "m")
    payload = verify(done.resolution, {"keys": [KEY.public_jwk()]})
    assert payload["kind"] == "resolution" and payload["resolves"] == "r_original"
    assert payload["outcome"] == "not_received" and payload["basis"] == "resident_statement"
    assert payload["statement_sha256"] == hashlib.sha256(reply.encode()).hexdigest()
    assert reply not in done.resolution  # the reply itself never leaves the house


def test_answers_and_resolutions_happen_once():
    d = disputes()
    q = d.open(claim(), "Did it arrive?")
    with pytest.raises(DisputeError, match="not replied"):
        d.resolve(q.question_id, "received", "yes", "m")
    d.answer(q.question_id, "yes")
    with pytest.raises(DisputeError, match="already been answered"):
        d.answer(q.question_id, "actually no")
    first = d.resolve(q.question_id, "received", "yes", "m")
    assert d.resolve(q.question_id, "not_received", "yes", "m").resolution == first.resolution


@pytest.mark.parametrize("text", ["", "x" * 281])
def test_question_length_is_bounded(text):
    with pytest.raises(DisputeError):
        disputes().open(claim(), text)


def test_questions_round_trip_through_dynamodb(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="hs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        store = DynamoStore(table, boto3.client("s3"), "unused")
        d = disputes(store)
        q = d.open(claim(), "Did it arrive?")
        assert store.question_for_claim("clm_1") == q and store.open_questions() == [q]
        d.answer(q.question_id, "yes it did")
        d.resolve(q.question_id, "received", "yes it did", "m")
        assert store.get_question(q.question_id).status == "resolved" and store.open_questions() == []
