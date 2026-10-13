import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from handshake.api import hash_key
from handshake.receipt import HouseKey

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing",
                 "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")


def test_bootstrap_creates_both_secrets_once_and_keeps_the_key_out_of_aws(aws, tmp_path, monkeypatch, capsys):
    boot = load("bootstrap_house")
    monkeypatch.setattr(boot, "LOCAL_KEYS", tmp_path / "handshake.env")
    monkeypatch.setattr(sys, "argv", ["bootstrap_house.py", "--merchant", "shop"])
    assert boot.main() == 0
    out = capsys.readouterr().out
    arns = json.loads(out.strip().splitlines()[-1])

    house = json.loads(aws.get_secret_value(SecretId=arns["HouseSecretArn"])["SecretString"])
    local = dict(line.split("=", 1) for line in (tmp_path / "handshake.env").read_text().split())
    assert house["merchants"] == {"shop": hash_key(local["HANDSHAKE_MERCHANT_KEY"])}
    assert house["resident_key_sha256"] == hash_key(local["HANDSHAKE_RESIDENT_KEY"])
    assert local["HANDSHAKE_RESIDENT_KEY"] not in out
    assert local["HANDSHAKE_MERCHANT_KEY"] not in out and local["HANDSHAKE_MERCHANT_KEY"] not in json.dumps(house)
    assert HouseKey.from_pem(house["signing_key_pem"].encode()).kid
    assert aws.get_secret_value(SecretId=arns["RingTokenSecretArn"])["SecretString"] == "not-set"

    # A second run must not replace the house key (receipts already issued depend on it).
    assert boot.main() == 0
    again = json.loads(aws.get_secret_value(SecretId=arns["HouseSecretArn"])["SecretString"])
    assert again["signing_key_pem"] == house["signing_key_pem"]


def test_set_ring_token_cleans_and_stores_without_printing(aws, monkeypatch, capsys):
    aws.create_secret(Name="handshake/ring-token", SecretString="not-set")
    setter = load("set_ring_token")
    monkeypatch.setattr(sys, "argv", ["set_ring_token.py", "--token", 'export TOKEN="eyJh.payload.sig"'])
    assert setter.main() == 0
    assert aws.get_secret_value(SecretId="handshake/ring-token")["SecretString"] == "eyJh.payload.sig"
    assert "eyJh" not in capsys.readouterr().out


def test_set_ring_token_refuses_placeholder_text(aws, monkeypatch):
    setter = load("set_ring_token")
    monkeypatch.setattr(sys, "argv", ["set_ring_token.py", "--token", "paste it here"])
    assert setter.main() == 2


def test_set_resident_key_adds_a_hash_and_refuses_to_replace_one_unasked(aws, tmp_path, monkeypatch, capsys):
    aws.create_secret(Name="handshake/house", SecretString=json.dumps({"house_id": "h1", "merchants": {}}))
    setter = load("set_resident_key")
    monkeypatch.setattr(setter, "LOCAL_KEYS", tmp_path / "handshake.env")
    monkeypatch.setattr(sys, "argv", ["set_resident_key.py"])
    assert setter.main() == 0
    house = json.loads(aws.get_secret_value(SecretId="handshake/house")["SecretString"])
    key = (tmp_path / "handshake.env").read_text().split("=", 1)[1].strip()
    assert house["resident_key_sha256"] == hash_key(key) and house["house_id"] == "h1"
    assert key not in capsys.readouterr().out
    assert setter.main() == 1
    monkeypatch.setattr(sys, "argv", ["set_resident_key.py", "--rotate"])
    assert setter.main() == 0
    rotated = json.loads(aws.get_secret_value(SecretId="handshake/house")["SecretString"])
    assert rotated["resident_key_sha256"] != house["resident_key_sha256"]
