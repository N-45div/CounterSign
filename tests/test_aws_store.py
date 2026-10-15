import boto3
import pytest
from moto import mock_aws

from countersign.aws_store import DynamoStore
from countersign.desk import ClaimRecord, ClaimStatus
from countersign.episode import Window
from countersign.store import StoredReceipt


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="countersign",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="countersign-frames")
        yield DynamoStore(table, s3, "countersign-frames")


def test_claims_round_trip(store):
    record = ClaimRecord("clm_1", "shop", "ORD-1", 1000, 900, carrier="Delhivery",
                         status=ClaimStatus.ISSUED, attempts=2, receipt_id="r1", verdict="delivered",
                         reason="package_appeared", token="jws")
    store.put_claim(record)
    assert store.get_claim("clm_1") == record
    assert store.get_claim("missing") is None


def test_receipts_by_order_and_recent(store):
    for i, (merchant, verdict) in enumerate([("shop", "inconclusive"), ("shop", "delivered"), ("other", "delivered")]):
        store.put_receipt(StoredReceipt(f"r{i}", "ORD-1", merchant, verdict, "x", f"jws{i}", 100 + i))
    assert [r.receipt_id for r in store.receipts_for_order("shop", "ORD-1")] == ["r0", "r1"]
    assert [r.receipt_id for r in store.recent_receipts(2)] == ["r2", "r1"]
    assert store.get_receipt("r1").token == "jws1"


def test_frames_are_private_encrypted_objects(store):
    store.put_frames("r1", {"before": b"\xff\xd8a", "after": b"\xff\xd8b"})
    assert store.get_frames("r1") == {"before": b"\xff\xd8a", "after": b"\xff\xd8b"}
    assert store.get_frames("r2") == {}
    head = store.s3.head_object(Bucket="countersign-frames", Key="frames/r1/after")
    assert head["ServerSideEncryption"] == "AES256"


def test_coverage_uses_the_state_before_the_window(store):
    store.record_status("dev", 500, False)  # offline before the window starts
    store.record_status("dev", 4_000, True)
    store.record_status("dev", 50_000, False)  # after the window, ignored
    cov = store.coverage("dev", Window(1_000, 11_000), True)
    assert cov.source == "webhooks" and cov.missing_ms == 3_000


def test_coverage_without_history_falls_back_to_status_now(store):
    assert store.coverage("dev", Window(0, 10), True).source == "status_now"


def test_webhook_request_ids_are_seen_once(store):
    assert store.first_sighting("req-1") is True
    assert store.first_sighting("req-1") is False
    assert store.first_sighting("req-2") is True


def test_a_namespaced_store_shares_the_table_without_sharing_records(store):
    from countersign.disputes import Question

    lab = DynamoStore(store.table, store.s3, store.bucket, namespace="LAB#", frames_prefix="lab/evidence/")
    lab.put_question(Question("q_lab", "clm_lab", "r_lab", "ORD-L", "lab-courier", "Did it arrive?", 1))
    store.put_question(Question("q_home", "clm_home", "r_home", "ORD-H", "shop", "Did it arrive?", 2))
    assert [q.question_id for q in lab.open_questions()] == ["q_lab"]
    assert [q.question_id for q in store.open_questions()] == ["q_home"]
    assert store.get_question("q_lab") is None and store.question_for_claim("clm_lab") is None
    lab.put_frames("r_lab", {"before": b"jpg"})
    keys = [o["Key"] for o in store.s3.list_objects_v2(Bucket=store.bucket)["Contents"]]
    assert keys == ["lab/evidence/r_lab/before"] and store.get_frames("r_lab") == {}
    assert lab.get_frames("r_lab") == {"before": b"jpg"}
