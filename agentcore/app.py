"""Handshake's dispute agent on Amazon Bedrock AgentCore Runtime.

Keyless and stateless by design: a payload of facts comes in, a proposal goes
out. The house that calls this holds the claims, the resident's reply and the
signing key, checks every proposal again, and signs. Nothing deployed here can
issue a receipt.

    {"task": "question",  "facts": {...}, "language": "English"}  -> {"question": "..."}
    {"task": "interpret", "exchange": {"question": "...", "reply": "..."}} -> {"interpretation": {...}}
"""

from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.models import BedrockModel

from handshake.dispute_agent import propose_interpretation, propose_question

MODEL_ID = os.environ.get("HANDSHAKE_AGENT_MODEL", "us.amazon.nova-2-lite-v1:0")
app = BedrockAgentCoreApp()
_model = None


def model():
    global _model
    if _model is None:
        _model = BedrockModel(model_id=MODEL_ID, region_name=os.environ.get("AWS_REGION", "us-east-1"), temperature=0.2)
    return _model


@app.entrypoint
def invoke(payload: dict) -> dict:
    task = payload.get("task")
    if task == "question" and isinstance(payload.get("facts"), dict):
        return {"question": propose_question(model(), payload["facts"], payload.get("language", "English")),
                "model": MODEL_ID}
    if task == "interpret" and isinstance(payload.get("exchange"), dict):
        return {"interpretation": propose_interpretation(model(), payload["exchange"]), "model": MODEL_ID}
    return {"error": "send task=question with facts, or task=interpret with exchange"}


if __name__ == "__main__":
    app.run()
