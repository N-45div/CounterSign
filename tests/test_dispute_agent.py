import json

from handshake.desk import ClaimRecord, ClaimStatus
from handshake.dispute_agent import DisputeAgent, template_question
from handshake.disputes import Disputes
from handshake.receipt import HouseKey, issue
from handshake.store import MemoryStore

KEY = HouseKey.generate()
T = 1_791_000_000_000


def claim():
    body = {"verdict": "inconclusive", "reason": "camera_not_recording", "visit": None,
            "device": "pseudonym", "evidence": []}
    token = issue(KEY, body, issuer="https://h", issued_at_s=T // 1000, receipt_id="r1")
    return ClaimRecord("clm_1", "Meesho", "ORD-402", T, T, carrier="Delhivery", status=ClaimStatus.ISSUED,
                       receipt_id="r1", verdict="inconclusive", reason="camera_not_recording", token=token)


class BrokenModel:
    """Stands in for Bedrock being down."""


def agent():
    return DisputeAgent(BrokenModel(), Disputes(MemoryStore(), KEY, "https://h"), "us.amazon.nova-2-lite-v1:0")


def test_claim_facts_carry_no_ring_ids_or_frames():
    a = agent()
    facts_tool, _ = a.ask_tools(claim(), {})
    facts = facts_tool()
    assert facts["merchant"] == "Meesho" and facts["order_id"] == "ORD-402"
    assert "offline" in facts["why_the_camera_could_not_settle_it"]
    assert facts["someone_came_to_the_door"] is False
    assert "ava1.ring" not in json.dumps(facts) and "pseudonym" not in json.dumps(facts)


def test_ask_resident_sends_once():
    a = agent()
    sent = {}
    _, ask = a.ask_tools(claim(), sent)
    assert ask("Did your Delhivery parcel from Meesho (order ORD-402) arrive on Monday?").startswith("Sent as")
    assert ask("And another?") == "Already sent; do not ask again."
    assert a.disputes.store.open_questions()[0].text.startswith("Did your Delhivery parcel")


def test_a_model_outage_still_asks_with_the_template():
    a = agent()
    q, by_model = a.ask(claim())
    assert not by_model and q.text == template_question(claim())
    assert "Delhivery" in q.text and "ORD-402" in q.text and "Meesho" in q.text


def test_record_interpretation_refuses_invented_quotes_and_stops_after_three():
    a = agent()
    q, _ = a.ask(claim())
    a.disputes.answer(q.question_id, "haan mil gaya, guard ke paas tha")
    done = {}
    _, record = a.interpret_tools(a.disputes.store.get_question(q.question_id), done)
    assert record("received", "yes I got it").startswith("Refused")
    assert record("received", "haan mil gaya", "left with the guard") == "Recorded received."
    assert done["question"].status == "resolved" and done["question"].note == "left with the guard"


def test_interpret_leaves_the_question_answered_when_the_model_is_down():
    a = agent()
    q, _ = a.ask(claim())
    a.disputes.answer(q.question_id, "not sure, I was out")
    assert a.interpret(q.question_id).status == "answered"


def test_the_agent_gets_three_tries_at_a_quote():
    a = agent()
    q, _ = a.ask(claim())
    a.disputes.answer(q.question_id, "nothing came")
    done = {}
    _, record = a.interpret_tools(a.disputes.store.get_question(q.question_id), done)
    results = [record("not_received", f"invented {i}") for i in range(4)]
    assert all(r.startswith("Refused") for r in results[:3])
    assert results[3] == "Too many attempts; stop."
    assert a.disputes.store.get_question(q.question_id).status == "answered"
