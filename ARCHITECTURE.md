# Countersign architecture

How a delivery claim becomes a signed receipt, and how the pieces are deployed. The [README](README.md) covers what the project is and how to run it; this file covers how it works inside. Numbers here are the values in the code.

## 1. The parts

```mermaid
flowchart LR
    M["Merchant or courier"] -->|"claim: order, time, carrier"| H
    subgraph H["The house: one household's AWS deployment"]
        API["House API"]
        DESK["Desk: claims and retries"]
        HOUSE["House: reads Ring, judges, signs"]
        DIS["Disputes: questions and resolutions"]
    end
    RING["Ring Partner API"] -->|"event history, snapshots"| HOUSE
    RING -->|"signed webhooks: device online / offline"| API
    HOUSE -->|"frames"| NOVA["Amazon Bedrock: Nova 2 Lite"]
    HOUSE -->|"frames"| TX["Amazon Textract"]
    DIS <-->|"facts in, proposal out"| AGENT["Dispute agent: Strands on AgentCore Runtime, no keys"]
    DIS <-->|"one question, their reply"| RES["Resident"]
    H -->|"signed receipt and resolution"| M
    V["Anyone verifying"] -->|"/.well-known/jwks.json"| API
```

The house belongs to the household. It holds the Ring token, the frames and the Ed25519 signing key, and it is the only thing that signs. A merchant gets a receipt; anyone can check it with the house's public key. Frames never leave the house's own S3 bucket; a receipt carries their SHA-256 hashes and a per-house pseudonym for the camera, never the Ring device id.

## 2. On AWS

```mermaid
flowchart TB
    APIURL["House API Function URL"] --> APIFN["ApiFunction: FastAPI via Mangum"]
    LABURL["Doorstep Lab Function URL"] --> LABFN["LabFunction: 3D page, Ring emulator, lab house"]
    APIFN -->|"attempt later / ask / interpret"| Q["SQS ClaimsQueue"]
    Q --> WORKER["WorkerFunction: runs queued tasks"]
    WORKER -->|"next attempt"| Q
    Q -.->|"after 5 receives"| DLQ["SQS dead-letter queue"]
    APIFN & WORKER & LABFN --> USES
    subgraph USES["What every function uses"]
        direction TB
        DDB[("DynamoDB: one table")]
        S3[("S3: frames, private, encrypted")]
        SM["Secrets Manager: signing key, key hashes, Ring token"]
        TXT["Textract"]
        AC["AgentCore Runtime: dispute agent"]
        BR["Bedrock: Nova 2 Lite"]
        AC --> BR
    end
```

Everything except the agent is one CloudFormation (SAM) stack, `infra/template.yaml`: three arm64 Python 3.12 Lambdas, the queue and its dead-letter queue, the table and the bucket. The agent is deployed separately as direct code (`scripts/deploy_agent.py`), and the stack's functions may call only that runtime. Lambdas reach AgentCore with `InvokeAgentRuntime`; the agent's own role may only call Nova and write logs.

## 3. A claim, end to end

```mermaid
sequenceDiagram
    autonumber
    participant M as Merchant
    participant API as House API
    participant D as Desk
    participant Q as SQS
    participant W as Worker
    participant H as House
    participant R as Ring Partner API
    participant N as Nova + Textract

    M->>API: POST /v1/claims {order_id, claimed_at, carrier}
    API->>D: submit (same merchant, order and time = same claim id)
    D->>H: verify
    H-->>D: not ready: Ring needs 2 minutes to file events
    D->>Q: attempt again at the time the house named (delay up to 900 s)
    API-->>M: 202 pending, Retry-After, Location
    Q->>W: attempt
    W->>D: attempt
    D->>H: verify
    H->>R: motion history, 20 min before to 10 min after the claim, padded 15 min
    H->>H: group person events within 90 s into a visit, attach the vehicle within 3 min
    H->>R: snapshot before the visit, snapshot after it (after the vehicle leaves)
    H->>N: the two frames
    N-->>H: report: parcel before? parcel after? where? confidence, text seen
    H->>H: rules decide the verdict, Ed25519 signs the receipt
    H-->>D: receipt (or not ready until the window closes)
    M->>API: GET /v1/claims/{id}
    API-->>M: 200 issued: verdict, reason, signed receipt
```

The desk tries once on arrival and then whenever the house says when to come back. A "delivered" is issued as soon as it is seen; "not observed" and "inconclusive" wait until the claim window has closed plus 2 minutes, so a late visit is not missed. An expired Ring token keeps the claim pending (retry in 5 minutes) and says why; other Ring errors retry in 2 minutes; after 12 failed attempts the claim is marked failed.

```mermaid
stateDiagram-v2
    [*] --> pending: claim submitted
    pending --> pending: not ready yet, or Ring error (retry scheduled)
    pending --> issued: the house signed a receipt
    pending --> failed: 12 attempts ended in errors
    issued --> [*]
    failed --> [*]
```

## 4. The witness: what the model may and may not decide

```mermaid
flowchart TB
    F["Frames before and after the visit"] --> OCR["Textract: whole frame + four overlapping quarters, merged"]
    F --> ASK["Nova 2 Lite, Converse, forced tool call, temperature 0"]
    ASK --> REP["Report: parcel before, parcel after, every place it sees one, confidence, text it can read"]
    OCR --> RULES["Text rules: lines addressed to a machine?"]
    REP --> RULES
    REP --> PLACE{"Any place = on the ground in front of the door?"}
    PLACE -->|"no"| NOP["Not a delivery: a note or sign on the door"]
    PLACE -->|"yes, and the frame holds text"| MASK["Paint every text region with the colour around it"]
    MASK --> ASK2["Ask Nova again on the masked frames"]
    ASK2 --> DEP{"Still a parcel on the ground?"}
    DEP -->|"no"| TD["text_dependent: the parcel was only words"]
    DEP -->|"yes"| OK["Report stands"]
```

Nova reports; it never decides. Textract cannot be instructed, so its reading of the frame is what the text rules trust. Each source of text is scanned on its own, so lines from two frames are never joined into an instruction that neither holds.

## 5. The rules that decide

```mermaid
flowchart TB
    START["Observation + coverage + witness report"] --> VISIT{"Was there a visit?"}
    VISIT -->|"no"| COV{"Camera recording the whole window?"}
    COV -->|"no"| I1["inconclusive · camera_not_recording"]
    COV -->|"yes"| OTHER{"Unclassified motion?"}
    OTHER -->|"yes"| I2["inconclusive · unclassified_motion"]
    OTHER -->|"no"| N1["not observed · no_one_at_door"]
    VISIT -->|"yes"| WIT{"Witness report?"}
    WIT -->|"no"| I3["inconclusive · frames_unavailable"]
    WIT -->|"yes"| TXT{"Text addressed to the AI?"}
    TXT -->|"yes"| I4["inconclusive · instruction_text_in_frame"]
    TXT -->|"no"| TD{"Parcel only while text was visible?"}
    TD -->|"yes"| I5["inconclusive · answer_depends_on_text"]
    TD -->|"no"| CONF{"Confidence at least 0.8?"}
    CONF -->|"no"| I6["inconclusive · low_confidence"]
    CONF -->|"yes"| BOTH{"Parcel before and after?"}
    BOTH -->|"yes"| I7["inconclusive · package_already_there"]
    BOTH -->|"no"| APP{"Parcel after, none before?"}
    APP -->|"yes"| D1["delivered · package_appeared"]
    APP -->|"no"| AFT{"No parcel after?"}
    AFT -->|"yes"| N2["not observed · visit_without_package"]
    AFT -->|"no"| I8["inconclusive · package_state_unknown"]
```

This is `verdict.decide()`, plain code. Coverage comes from Ring's `device_online` / `device_offline` webhooks: the last status before the window and every change inside it. With no history, the house uses the camera's status now and the receipt says so.

## 6. When the camera cannot settle it

```mermaid
sequenceDiagram
    autonumber
    participant D as Desk
    participant Q as SQS
    participant W as Worker
    participant A as Dispute agent (AgentCore)
    participant H as Disputes (house)
    participant R as Resident
    participant M as Merchant

    D->>Q: inconclusive receipt issued: {task: ask}
    Q->>W: ask
    W->>A: {task: question, facts}: merchant, carrier, order, day, why the camera could not tell
    A-->>W: one plain question (or none: a template question is used)
    W->>H: open the question for this claim (one per claim)
    R->>H: GET /v1/resident/questions
    R->>H: POST answer {reply}, in any language
    H->>A: {task: interpret, exchange: question + reply}
    A-->>H: outcome + quote + note
    H->>H: the quote must appear word for word in the reply, or it is refused
    H->>H: sign a resolution: kind resolution, resolves the receipt id, SHA-256 of the reply
    M->>H: GET /v1/claims/{id}
    H-->>M: dispute: outcome and signed resolution, never the reply
    Note over H,Q: If the agent cannot be reached, {task: interpret} is queued for 60 s later. SQS retries, then the dead-letter queue.
```

The agent never sees frames, Ring ids or keys, and it never signs. The question names the day in the household's timezone.

## 7. What is stored

One DynamoDB table, keyed by `pk` and `sk`:

| Partition key | Sort key | Holds |
|---|---|---|
| `CLAIM#<claim_id>` | `CLAIM` | the claim record and its status |
| `RECEIPT#<receipt_id>` | `RECEIPT` | the signed receipt |
| `ORDER#<merchant>#<order_id>` | `<issued_at>#<id>` | receipts per order |
| `RECENT` | `<issued_at>#<id>` | receipts, newest first |
| `DEVICE#<device_id>` | `STATUS#<at_ms>` | the camera's online history, from webhooks |
| `WEBHOOK#<request_id>` | `SEEN` | webhook dedupe, expires after 7 days |
| `QUESTION#<question_id>` | `QUESTION` | a question for the resident and its resolution |
| `CLAIMQ#<claim_id>` | `QUESTION` | the claim's question id |
| `OPENQ` | `<asked_at>#<id>` | open questions, oldest first |
| `LABSESSION#<sid>` | `SESSION` | a Doorstep Lab visitor's session, expires after a day |
| `LABBUDGET#<day>` | `BUDGET` | Nova runs used by the lab today |

The lab's own house writes the same keys with a `LAB#` prefix, so lab claims and questions never reach the real resident. S3 holds evidence frames under `frames/<receipt_id>/` (kept 90 days) and everything of the lab under `lab/` (kept 1 day).

## 8. The Doorstep Lab

```mermaid
flowchart TB
    B["Visitor's browser: Three.js doorstep"]
    B -->|"POST /lab/start"| S["Session: scenario, start time, device, token"]
    B -->|"each recording moment: the doorbell's rendered frame"| EMU["Ring-shaped emulator: same routes, JSON:API shapes, 303 media redirects"]
    B -->|"the courier's claim, then the resident's reply"| LH["Lab house: the same house code"]
    S -->|"signed device_offline / online webhooks"| LH
    LH -->|"unchanged Ring client over HTTP, session token"| EMU
    LH --> W["Nova 2 Lite + Textract, or ground truth after the daily budget"]
    LH -->|"signed receipt, or a question when the camera was offline"| V["Browser checks the Ed25519 signature against /lab/jwks.json"]
```

The lab is a simulation, built because the Ring Developer Playground cannot stage a delivery. Each visitor gets their own session, device and token, so visitors never see each other's doorstep. The lab house runs the same house code as the real one against the emulator, with Nova judging up to a daily budget of runs.

## 9. Where to look in the code

| Concern | Files |
|---|---|
| Ring client and webhooks | `countersign/ring.py`, `countersign/webhooks.py`, `countersign/tokens.py` |
| Claim window, visits, frames | `countersign/episode.py`, `countersign/house.py` |
| Witness and text guard | `countersign/witness.py`, `countersign/doorman.py` |
| Rules and receipts | `countersign/verdict.py`, `countersign/receipt.py` |
| Claims, API, storage | `countersign/desk.py`, `countersign/api.py`, `countersign/store.py`, `countersign/aws_store.py` |
| Disputes | `countersign/disputes.py`, `countersign/dispute_agent.py`, `agentcore/app.py` |
| AWS entry points | `countersign/lambda_app.py`, `countersign/lab/lambda_lab.py`, `infra/template.yaml` |
| Doorstep Lab | `countersign/lab/` |

The text guard in `countersign/doorman.py` also lives on as its own open-source library, [Doorman](https://github.com/N-45div/Doorman), generalised to any camera question and any vision model.
