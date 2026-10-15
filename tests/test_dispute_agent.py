import json

from countersign.desk import ClaimRecord, ClaimStatus
from countersign.dispute_agent import (DisputeAgent, RemoteDisputeAgent, claim_facts_for, exchange_for,
                                     interpretation_tools, question_tools, template_question)
from countersign.disputes import Disputes
from countersign.receipt import HouseKey, issue
from countersign.store import MemoryStore

KEY = HouseKey.generate()
T = 1_791_000_000_000


def claim(claim_id="clm_1", receipt_id="r1"):
    body = {"verdict": "inconclusive", "reason": "camera_not_recording", "visit": None,
            "device": "pseudonym", "evidence": []}
    token = issue(KEY, body, issuer="https://h", issued_at_s=T // 1000, receipt_id=receipt_id)
    return ClaimRecord(claim_id, "Meesho", "ORD-402", T, T, carrier="Delhivery", status=ClaimStatus.ISSUED,
                       receipt_id=receipt_id, verdict="inconclusive", reason="camera_not_recording", token=token)


class BrokenModel:
    """Stands in for Bedrock being down."""


def disputes():
    return Disputes(MemoryStore(), KEY, "https://h")


def test_claim_facts_carry_no_ring_ids_or_frames():
    facts = claim_facts_for(claim())
    assert facts["merchant"] == "Meesho" and facts["order_id"] == "ORD-402"
    assert "offline" in facts["why_the_camera_could_not_settle_it"]
    assert facts["someone_came_to_the_door"] is False
    assert "ava1.ring" not in json.dumps(facts) and "pseudonym" not in json.dumps(facts)


def test_ask_resident_takes_one_question():
    out = {}
    _, ask = question_tools(claim_facts_for(claim()), out)
    assert ask("Did your Delhivery parcel from Meesho (order ORD-402) arrive on Monday?") == "Sent."
    assert ask("And another?") == "Already sent; do not ask again."
    assert out["question"].startswith("Did your Delhivery parcel")


def test_a_model_outage_still_asks_with_the_template():
    q, by_model = DisputeAgent(BrokenModel(), disputes(), "m").ask(claim())
    assert not by_model and q.text == template_question(claim())
    assert "Delhivery" in q.text and "ORD-402" in q.text and "Meesho" in q.text


def test_the_interpretation_tool_refuses_invented_quotes_and_stops_after_three():
    exchange = {"question": "Did it arrive?", "reply": "haan mil gaya, guard ke paas tha"}
    out = {}
    _, record = interpretation_tools(exchange, out)
    assert record("received", "yes I got it").startswith("Refused")
    assert record("maybe", "mil gaya").startswith("Refused")
    assert record("received", "haan mil gaya", "left with the guard") == "Recorded received."
    assert out["interpretation"] == {"outcome": "received", "quote": "haan mil gaya", "note": "left with the guard"}
    assert record("received", "x") == "Too many attempts; stop."


class Proposing(DisputeAgent):
    """Skips the model: returns a fixed proposal, to test how the house applies it."""

    def __init__(self, d, question=None, interpretation=None):
        super().__init__(BrokenModel(), d, "m")
        self.q, self.i = question, interpretation

    def _question(self, facts):
        return self.q

    def _interpretation(self, exchange):
        return self.i


def test_the_house_rechecks_a_proposal_before_signing():
    d = disputes()
    agent = Proposing(d, question="Did your Meesho parcel arrive?",
                      interpretation={"outcome": "received", "quote": "words nobody wrote"})
    q, by_model = agent.ask(claim())
    assert by_model and q.text == "Did your Meesho parcel arrive?"
    d.answer(q.question_id, "nothing came")
    assert agent.interpret(q.question_id).status == "answered"  # refused by disputes.py, never signed
    agent.i = {"outcome": "not_received", "quote": "nothing came", "note": ""}
    done = agent.interpret(q.question_id)
    assert done.status == "resolved" and done.outcome == "not_received"


def test_remote_agent_applies_agentcore_proposals_and_survives_an_outage():
    calls = []

    def invoke(payload):
        calls.append(payload["task"])
        if payload["task"] == "question":
            return {"question": "Did your Delhivery parcel for ORD-402 arrive?"}
        return {"interpretation": {"outcome": "received", "quote": payload["exchange"]["reply"], "note": ""}}

    d = disputes()
    remote = RemoteDisputeAgent(invoke, d, "us.amazon.nova-2-lite-v1:0")
    q, by_model = remote.ask(claim())
    assert by_model and calls == ["question"]
    d.answer(q.question_id, "yes it came")
    assert remote.interpret(q.question_id).outcome == "received"

    def down(payload):
        raise ConnectionError("AgentCore unavailable")

    other = claim("clm_2", "r2")
    q2, by_model2 = RemoteDisputeAgent(down, d, "m").ask(other)
    assert not by_model2 and q2.text == template_question(other)


def test_exchange_carries_only_the_question_and_reply():
    d = disputes()
    q = d.open(claim(), "Did it arrive?")
    d.answer(q.question_id, "yes")
    assert set(exchange_for(d.store.get_question(q.question_id))) == {"question", "reply", "merchant", "order_id"}


def test_the_house_side_loads_without_strands(monkeypatch):
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "strands", None)  # the Lambda bundle has no Strands
    monkeypatch.delitem(sys.modules, "countersign.dispute_agent")
    module = importlib.import_module("countersign.dispute_agent")
    assert module.RemoteDisputeAgent and module.agentcore_invoker


def test_questions_name_the_day_in_the_residents_timezone():
    from countersign.dispute_agent import _day

    late = 1_791_491_400_000  # 2026-10-08 20:30 UTC, already 9 October in India
    assert _day(late) == "Thursday 08 October 2026"
    assert _day(late, "Asia/Kolkata") == "Friday 09 October 2026"
    assert _day(late, "Not/AZone") == _day(late, "../../etc/passwd") == "Thursday 08 October 2026"
    record = claim()
    assert claim_facts_for(record, "Asia/Kolkata")["claimed_delivered_on"] == _day(record.claimed_at_ms, "Asia/Kolkata")
