"""The dispute agent: a Strands agent on Amazon Nova that talks to the resident.

Two short jobs, each with narrow tools:

* ask: read what the camera established about an inconclusive claim, then
  send the resident one plain question (ask_resident, once).
* interpret: read the resident's free-text reply, in whatever language they
  wrote it, and record an outcome with a quote of their own words
  (record_interpretation). The house refuses a quote the resident did not
  write, and the agent can try again.

The model words and reads; it never signs. Resolutions are issued by
disputes.py from a checked (outcome, quote) pair. If the model does not ask,
a plain template question goes out, so a claim is never stuck on it.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from strands import Agent, tool

from .disputes import Disputes, DisputeError, Outcome, Question

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


class DisputeAgent:
    def __init__(self, model, disputes: Disputes, model_id: str, language: str = "English"):
        self.model = model  # a Strands model, e.g. BedrockModel(model_id="us.amazon.nova-2-lite-v1:0")
        self.disputes = disputes
        self.model_id = model_id
        self.language = language

    # -- ask -----------------------------------------------------------------

    def ask_tools(self, claim, sent: dict):
        receipt = _payload(claim.token)

        @tool
        def claim_facts() -> dict:
            """What the merchant claims and what the doorbell camera established about it."""
            visit = receipt.get("visit")
            return {
                "merchant": claim.merchant,
                "carrier": claim.carrier,
                "order_id": claim.order_id,
                "claimed_delivered_on": _day(claim.claimed_at_ms),
                "why_the_camera_could_not_settle_it": REASON_TEXT.get(claim.reason, claim.reason),
                "someone_came_to_the_door": visit is not None,
            }

        @tool
        def ask_resident(question: str) -> str:
            """Send one question to the resident. Call this exactly once.

            Args:
                question: the question, at most 200 characters
            """
            if "question" in sent:
                return "Already sent; do not ask again."
            try:
                sent["question"] = self.disputes.open(claim, question[:200])
            except DisputeError as exc:
                return f"Refused: {exc}"
            return f"Sent as {sent['question'].question_id}."

        return [claim_facts, ask_resident]

    def ask(self, claim) -> tuple[Question, bool]:
        """(question, asked_by_model). Falls back to a template so a claim never stalls."""
        existing = self.disputes.store.question_for_claim(claim.claim_id)
        if existing is not None:
            return existing, False
        sent: dict = {}
        try:
            agent = Agent(model=self.model, tools=self.ask_tools(claim, sent),
                          system_prompt=ASK_PROMPT.format(language=self.language), callback_handler=None)
            agent("Ask the resident about this claim.")
        except Exception:  # a model outage must not leave the claim without a question
            pass
        if "question" in sent:
            return sent["question"], True
        return self.disputes.open(claim, template_question(claim)), False

    # -- interpret -------------------------------------------------------------

    def interpret_tools(self, question: Question, done: dict):
        @tool
        def the_exchange() -> dict:
            """The question the resident was asked, and their reply exactly as written."""
            return {"question": question.text, "reply": question.reply,
                    "merchant": question.merchant, "order_id": question.order_id}

        @tool
        def record_interpretation(outcome: str, quote: str, note: str = "") -> str:
            """Record what the reply says. Call once; retry only if refused.

            Args:
                outcome: received, not_received or unsure
                quote: the exact words from the reply that show the outcome, copied character for character
                note: a few English words on any detail that matters
            """
            attempts = done.setdefault("attempts", 0) + 1
            done["attempts"] = attempts
            if attempts > 3:
                return "Too many attempts; stop."
            try:
                done["question"] = self.disputes.resolve(question.question_id, outcome, quote, self.model_id, note)
            except DisputeError as exc:
                return f"Refused: {exc}. Valid outcomes: {[o.value for o in Outcome]}."
            return f"Recorded {outcome}."

        return [the_exchange, record_interpretation]

    def interpret(self, question_id: str) -> Question:
        """Resolve an answered question. Returns it unresolved if the model could not back an outcome."""
        question = self.disputes.store.get_question(question_id)
        if question is None or question.status != "answered":
            return question
        done: dict = {}
        try:
            agent = Agent(model=self.model, tools=self.interpret_tools(question, done),
                          system_prompt=INTERPRET_PROMPT, callback_handler=None)
            agent("Interpret the resident's reply.")
        except Exception:
            pass
        return done.get("question") or self.disputes.store.get_question(question_id)
