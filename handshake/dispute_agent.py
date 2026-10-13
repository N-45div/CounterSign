"""The dispute agent: a Strands agent on Amazon Nova that talks to the resident.

Two short jobs, each a pure proposal with narrow tools:

* propose_question(facts): read what the camera established about an
  inconclusive claim and propose one plain question for the resident.
* propose_interpretation(exchange): read the resident's free-text reply, in
  whatever language they wrote it, and propose an outcome with a quote of
  their own words. The tool refuses a quote the resident did not write, and
  the agent can try again.

The proposals take facts in and give text out: no store, no signing key. That
is what runs on Amazon Bedrock AgentCore Runtime (agentcore/app.py). The house
applies a proposal through disputes.py, which checks it again and signs. If
the model does not propose a question, a plain template question goes out, so
a claim is never stuck on it.

    DisputeAgent        proposals run in this process (lab, tests, local)
    RemoteDisputeAgent  proposals run on AgentCore Runtime (deployed house)
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Callable

from .disputes import Disputes, DisputeError, Outcome, Question, _normalise

REASON_TEXT = {
    "camera_not_recording": "the camera was offline for part of the delivery window",
    "unclassified_motion": "the camera saw motion it could not classify",
    "instruction_text_in_frame": "a note in view of the camera was written to the AI, so the frames were not trusted",
    "answer_depends_on_text": "the camera only saw a parcel while a note was readable, so it was not trusted",
    "package_already_there": "a parcel was already on the doorstep before the visit",
    "low_confidence": "the camera's view was not clear enough",
    "frames_unavailable": "the frames around the visit were not available",
    "package_state_unknown": "the camera could not tell whether a parcel was left",
}

ASK_PROMPT = """You help a household settle a disputed delivery. A merchant says a parcel was delivered;
the household's doorbell camera could not confirm or rule it out. Call claim_facts, then call
ask_resident exactly once with one short question for the resident.

The question must:
- name the merchant, the carrier if known, the order id and the day
- be neutral: never suggest the answer, never mention AI, cameras' doubts or receipts
- be answerable in a few words, at most two sentences and 200 characters
- be in {language}
Reply with one line saying the question was sent."""

INTERPRET_PROMPT = """You read a resident's reply about whether a parcel arrived. Call the_exchange to read the
question and the reply, then call record_interpretation exactly once:
- outcome: received, not_received, or unsure (use unsure when the reply does not say, or hedges)
- quote: copy the exact words from the reply that show the outcome, character for character,
  in the reply's own language. Never translate or rephrase the quote.
- note: a few English words on any detail that matters (left with a guard, damaged, wrong item)
If the tool refuses your quote, copy the words again exactly as written and retry.
Reply with one line saying what you recorded."""


def _payload(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%A %d %B %Y")


def template_question(claim) -> str:
    who = f"{claim.carrier} " if claim.carrier else ""
    return f"Did your {who}parcel from {claim.merchant} (order {claim.order_id}) arrive on {_day(claim.claimed_at_ms)}?"


def claim_facts_for(claim) -> dict:
    """What the agent may know about a claim: no Ring ids, no frames, no keys."""
    visit = _payload(claim.token).get("visit") if claim.token else None
    return {
        "merchant": claim.merchant,
        "carrier": claim.carrier,
        "order_id": claim.order_id,
        "claimed_delivered_on": _day(claim.claimed_at_ms),
        "why_the_camera_could_not_settle_it": REASON_TEXT.get(claim.reason, claim.reason),
        "someone_came_to_the_door": visit is not None,
    }


def exchange_for(question: Question) -> dict:
    return {"question": question.text, "reply": question.reply, "merchant": question.merchant,
            "order_id": question.order_id}


# -- proposals (pure: facts in, text out) -----------------------------------------


def question_tools(facts: dict, out: dict):
    from strands import tool  # imported here: the house's Lambda applies proposals without Strands

    @tool
    def claim_facts() -> dict:
        """What the merchant claims and what the doorbell camera established about it."""
        return facts

    @tool
    def ask_resident(question: str) -> str:
        """Send one question to the resident. Call this exactly once.

        Args:
            question: the question, at most 200 characters
        """
        if "question" in out:
            return "Already sent; do not ask again."
        text = " ".join((question or "").split())[:200]
        if not text:
            return "Refused: the question is empty."
        out["question"] = text
        return "Sent."

    return [claim_facts, ask_resident]


def interpretation_tools(exchange: dict, out: dict):
    from strands import tool

    @tool
    def the_exchange() -> dict:
        """The question the resident was asked, and their reply exactly as written."""
        return exchange

    @tool
    def record_interpretation(outcome: str, quote: str, note: str = "") -> str:
        """Record what the reply says. Call once; retry only if refused.

        Args:
            outcome: received, not_received or unsure
            quote: the exact words from the reply that show the outcome, copied character for character
            note: a few English words on any detail that matters
        """
        out["attempts"] = out.get("attempts", 0) + 1
        if out["attempts"] > 3:
            return "Too many attempts; stop."
        if outcome not in {o.value for o in Outcome}:
            return f"Refused: outcome must be one of {[o.value for o in Outcome]}."
        if not quote or _normalise(quote) not in _normalise(exchange.get("reply") or ""):
            return "Refused: the quote must be words the resident actually wrote."
        out["interpretation"] = {"outcome": outcome, "quote": quote.strip(), "note": (note or "")[:300]}
        return f"Recorded {outcome}."

    return [the_exchange, record_interpretation]


def propose_question(model, facts: dict, language: str = "English") -> str | None:
    from strands import Agent

    out: dict = {}
    try:
        Agent(model=model, tools=question_tools(facts, out), system_prompt=ASK_PROMPT.format(language=language),
              callback_handler=None)("Ask the resident about this claim.")
    except Exception:  # a model outage must not take the claim down with it
        return None
    return out.get("question")


def propose_interpretation(model, exchange: dict) -> dict | None:
    from strands import Agent

    out: dict = {}
    try:
        Agent(model=model, tools=interpretation_tools(exchange, out), system_prompt=INTERPRET_PROMPT,
              callback_handler=None)("Interpret the resident's reply.")
    except Exception:
        return None
    return out.get("interpretation")


# -- applying proposals in the house ---------------------------------------------


class _Applies:
    disputes: Disputes
    model_id: str

    def _question(self, facts: dict) -> str | None: ...  # pragma: no cover
    def _interpretation(self, exchange: dict) -> dict | None: ...  # pragma: no cover

    def ask(self, claim) -> tuple[Question, bool]:
        """(question, asked_by_model). Falls back to a template so a claim never stalls."""
        existing = self.disputes.store.question_for_claim(claim.claim_id)
        if existing is not None:
            return existing, False
        proposed = self._question(claim_facts_for(claim))
        if proposed:
            try:
                return self.disputes.open(claim, proposed), True
            except DisputeError:
                pass
        return self.disputes.open(claim, template_question(claim)), False

    def interpret(self, question_id: str) -> Question:
        """Resolve an answered question. Left answered when no outcome could be backed by a quote."""
        question = self.disputes.store.get_question(question_id)
        if question is None or question.status != "answered":
            return question
        proposal = self._interpretation(exchange_for(question))
        if not proposal:
            return question
        try:
            return self.disputes.resolve(question_id, proposal["outcome"], proposal["quote"], self.model_id,
                                         proposal.get("note", ""))
        except (DisputeError, KeyError):  # checked again here: the house never trusts the proposal
            return question


class DisputeAgent(_Applies):
    """Proposals run in this process."""

    def __init__(self, model, disputes: Disputes, model_id: str, language: str = "English"):
        self.model = model  # a Strands model, e.g. BedrockModel(model_id="us.amazon.nova-2-lite-v1:0")
        self.disputes = disputes
        self.model_id = model_id
        self.language = language

    def _question(self, facts):
        return propose_question(self.model, facts, self.language)

    def _interpretation(self, exchange):
        return propose_interpretation(self.model, exchange)


class RemoteDisputeAgent(_Applies):
    """Proposals run on Amazon Bedrock AgentCore Runtime; this side only applies them."""

    def __init__(self, invoke: Callable[[dict], dict], disputes: Disputes, model_id: str, language: str = "English"):
        self.invoke = invoke  # payload -> response, e.g. agentcore_invoker(client, runtime_arn)
        self.disputes = disputes
        self.model_id = model_id
        self.language = language

    def _question(self, facts):
        try:
            return self.invoke({"task": "question", "facts": facts, "language": self.language}).get("question")
        except Exception:
            return None

    def _interpretation(self, exchange):
        try:
            return self.invoke({"task": "interpret", "exchange": exchange}).get("interpretation")
        except Exception:
            return None


def agentcore_invoker(client, runtime_arn: str) -> Callable[[dict], dict]:
    """Call the deployed agent through bedrock-agentcore InvokeAgentRuntime."""
    import uuid

    def invoke(payload: dict) -> dict:
        resp = client.invoke_agent_runtime(agentRuntimeArn=runtime_arn, runtimeSessionId=uuid.uuid4().hex * 2,
                                           payload=json.dumps(payload).encode(), contentType="application/json",
                                           accept="application/json")
        body = resp["response"].read() if hasattr(resp.get("response"), "read") else resp.get("response", b"")
        return json.loads(body or b"{}")

    return invoke
