> **Note:** This is the original IndiaShipments AI Shipment Agent Challenge brief with one addition — a "Conversational UX Flow" section describing how the agent should behave turn-by-turn, from the candidate's own perspective. Everything else (architecture freedom, required outcome, business rules, lifecycle, integration requirement, evaluation criteria) is unchanged from the original brief.

# IndiaShipments AI Shipment Agent Challenge

## Candidate brief

Build a full-stack AI agent that makes domestic shipment management easier for an end user.

The user should be able to describe what they want in natural language, and the agent should guide the request through to a completed shipment and understandable tracking information.

This is an open implementation challenge. You start from an empty repository and choose the architecture, programming languages, frameworks, database, model provider, user interface, and external services.

The goal is not to build a generic chatbot. The goal is to build an agent that understands a request, uses tools and application data, performs safe actions, and helps the user reach an outcome.

## Timebox

- Build time: six hours.
- Start from an empty repository.
- You may use AI coding assistants and any development tools.
- You may use your own LLM/API key. Keep credentials out of source control.
- Submit a working local or deployed application, source code, setup instructions, and a short design note.

## Required outcome

At minimum, your application must support this complete journey:

1. A user asks the agent to book a domestic shipment.
2. The agent identifies the shipment information that is missing or ambiguous.
3. The agent asks focused follow-up questions.
4. The agent validates the information and explains any problem.
5. The agent presents a complete shipment summary for review.
6. The user confirms the booking.
7. The backend persists the shipment and returns a shipment/tracking reference.
8. The user asks for the shipment status.
9. The agent retrieves the shipment's tracking events and explains the current status in plain language.

The booking must be a real application action backed by persistence, not a response that only looks successful in the interface.

## What "agent" means in this challenge

The agent should be able to:

- Interpret natural language rather than requiring a rigid command sequence.
- Decide which information or tool it needs next.
- Use backend functions or APIs to read and change shipment data.
- Distinguish a draft from a confirmed booking.
- Ask for confirmation before creating or changing a shipment.
- Refuse, pause, or request evidence when a business rule requires it.
- Never invent a price, reference, tracking event, or delivery status.
- Recover clearly when an API or internal operation fails.

You may implement this with tool calling, a workflow, a state machine, a planner, or another design. The implementation choice is part of the evaluation.

## Conversational UX Flow (candidate's UX notes)

This section describes the intended feel and turn-by-turn interaction pattern for the agent, as envisioned by the candidate. It does not change the required outcome, business rules, or architecture above — it describes how those requirements should surface to the user in conversation.

- The agent should read like a support assistant, not a form: warm, guiding, and transparent about why it is asking something.
- When the user states an intent in natural language (e.g., "I want to send a parcel from Chennai to Bengaluru"), the agent identifies what's missing and asks one focused follow-up question at a time, rather than presenting a long form all at once.
- Where the missing information maps to a fixed set of choices (service type, contents category, insurance choice, and similar), the agent should present the user with selectable options drawn from the application's known lists — for example, a shortlist of common/acceptable content categories — rather than leaving the question fully open-ended.
- Each user response is processed on its own turn: the agent validates it, updates its understanding of the shipment, and decides the next question based on that specific answer, rather than following a fixed scripted sequence regardless of what the user says.
- When a response triggers a business rule — for example, the user selects "medicines" as the shipment contents — the agent should explain the applicable rule in plain language before proceeding (e.g., that a prescription or supporting document is required to ship medicines), rather than silently blocking or silently allowing the booking.
- The user should be able to attach a supporting file (PDF or similar) directly in the conversation in response to such a request.
- After the file is attached, the agent should acknowledge receipt, state that it is reviewing/processing the document, and then report the outcome (accepted, pending, or rejected) before the booking proceeds — consistent with the "Supporting-document workflow" section below (verification may be simulated for the six-hour build).
- Throughout the conversation, the agent should be able to state which constraint or rule it is applying when asked, or when it's relevant to a decision — the user should never be left wondering why a question was asked or why a booking was paused.

## IndiaShipments business briefing

IndiaShipments is a domestic logistics service. A shipper sends a parcel to a recipient. The platform collects shipment details, checks whether the parcel can be accepted, creates a booking, and provides tracking updates as the parcel moves through the delivery lifecycle.

The end user should not need to understand logistics terminology. The agent is expected to translate between a conversational request and structured shipment operations.

### Shipment concepts

- **Shipper:** person sending the parcel.
- **Recipient:** person receiving the parcel.
- **Shipment:** one delivery request from a shipper to a recipient.
- **Package/box:** a physical parcel within a shipment.
- **Shipment reference:** an identifier created by your application for this challenge.
- **Tracking event:** a timestamped event describing a shipment's progress.
- **Serviceability:** whether the chosen service can deliver to the destination PIN.

### Information normally needed

- Sender name, phone, address, city, state, and six-digit Indian PIN.
- Recipient name, phone, address, city, state, and six-digit Indian PIN.
- Service type, such as Standard or Express.
- Contents description.
- Declared value in INR.
- Package weight in grams.
- Length, width, and height in millimetres.
- Insurance choice where relevant.
- Supporting documents or delivery instructions where relevant.

The agent should reuse information already supplied in the conversation or stored for the user. It should not ask the user to repeat information unnecessarily.

## Challenge business rules

These are simplified rules for the interview exercise. They are not a complete statement of India Post regulations and must not be presented as production legal advice.

### Basic validation

- Domestic shipments only.
- Indian PIN codes contain exactly six digits.
- Weight, dimensions, and declared value must be positive.
- Minimum package dimensions are 140 mm × 90 mm × 10 mm.
- Total package dimensions must not exceed 3,000 mm.
- A destination that your serviceability check cannot support must not be silently booked.
- A city and PIN mismatch must be surfaced and resolved or explicitly accepted according to your documented design.

### Contents and compliance

The following items must be blocked in the challenge:

- Currency, cash, or coins.
- Explosives, fireworks, flammable liquids, or compressed gases.
- Firearms, ammunition, or weapons.
- Narcotics, poisons, or radioactive material.
- Live animals.
- Counterfeit or otherwise prohibited goods.

The following items require a condition or warning:

- Lithium batteries: allowed only when installed in the equipment they power; loose or spare batteries must be blocked.
- Liquids: require leak-proof packaging.
- Fragile goods: require suitable cushioning.
- Perishables: require suitable packaging and an appropriate service.
- Medicines: may require a prescription or supporting document.
- High-value goods above ₹50,000: require an insurance warning and explicit user acknowledgement.

When a rule blocks or pauses a shipment, the agent should explain what happened and what the user can do next. It must not invent a regulatory threshold that is not stated here.

## Supporting-document workflow

Some shipment requests may need evidence before booking can continue. Your application should model this possibility, even if you keep the implementation lightweight.

Use these challenge examples:

| Situation | Expected behaviour |
|---|---|
| Medicine | Request a prescription or supporting document before final booking. |
| Commercial shipment | Request an invoice when your documented scenario requires one. |
| Restricted item | Explain the condition and collect the required confirmation or evidence. |
| High-value shipment | Show the insurance warning and obtain explicit acknowledgement. |
| Missing document | Pause the booking and state exactly what is missing. |
| Uploaded image/PDF | Extract only the fields you need, show them to the user, and ask for confirmation. |

For the six-hour version, document verification may be simulated or implemented with a simple upload and review state. OCR is optional. A document must never be treated as valid merely because a model produced text from it.

## Shipment lifecycle and tracking

Your system must persist the shipment's current state and its event history.

The normal lifecycle is:

`Draft → Booked → Picked up → In transit → Out for delivery → Delivered`

Exceptional states are:

- `Delivery failed`
- `Returned`
- `Cancelled`

Every status change should create a timestamped tracking event. The tracking response should include the latest known status, the latest event time, and a human-readable explanation.

You may seed shipments or provide a simple internal simulator so the reviewer can see several states, including at least one delivered shipment and one shipment with a problem or delay. Do not present a locally generated reference as an official carrier AWB; call it a shipment or tracking reference.

## External integration requirement

Use at least one meaningful external API or service of your choice. It must materially affect the workflow or answer, not merely appear in the codebase.

Examples include address/PIN validation, geocoding, maps or distance calculation, document extraction, notifications, weather-related delivery guidance, or another service you can justify.

You own the shipment database and booking backend for this challenge; no carrier sandbox is provided. If an external service is unavailable, the application should fail clearly and preserve the user's draft where possible.

In your design note, state:

- Why you selected the integration.
- What data enters and leaves it.
- How you handle authentication, failure, latency, and rate limits.
- What the application can still do if the service is unavailable.

## Example conversations to support

### Complete booking

> "Book a 2 kg parcel from Kochi to Bengaluru for Anil. It contains documents."

The agent should collect any missing addresses, PIN codes, dimensions, service choice, and declared value; show a summary; wait for confirmation; persist the booking; and return a reference.

### Missing information

> "Send this package to Priya in Chennai."

The agent should ask for the missing sender, recipient contact/address, package, and service details instead of guessing.

### Restricted contents

> "I want to send a spare lithium battery."

The agent should block the booking and explain that loose or spare batteries are not allowed under the challenge rules.

### Tracking

> "Where is shipment IS-1042 now?"

The agent should retrieve the persisted tracking events and answer with the latest known status, time, location or milestone if available, and the next useful action.

### Tracking problem

> "Why hasn't my parcel arrived?"

The agent should inspect the event history, explain whether the shipment is delayed, failed, or returned, and avoid claiming a delivery date that the data does not support.

## Out of scope for the six-hour submission

These are welcome only as extensions after the required journey works:

- Live carrier or payment integration.
- International shipping or customs.
- Production-grade KYC, tax, or legal compliance.
- Voice input.
- Autonomous booking without confirmation.
- A large admin dashboard.
- Multi-carrier optimisation.

## What we evaluate

We evaluate the working outcome and the quality of the decisions behind it:

- Does the agent complete booking and tracking end to end?
- Does it ask good questions instead of guessing?
- Are actions and state transitions backed by a real backend and persistence?
- Does it use the LLM for reasoning while keeping business rules and data in reliable application code?
- Is the external integration meaningful and resilient?
- Does the UI make the next action obvious to the end user?
- Are failures, missing documents, restricted items, and stale tracking handled honestly?
- Can you explain your architecture, trade-offs, and what you would build next?

Polish is useful, but a beautiful chat window without a functioning shipment workflow will not satisfy the brief.

## Submission checklist

- Working application that a reviewer can run.
- Source code and setup instructions.
- Environment-variable example with secrets removed.
- At least one meaningful external integration.
- Persistent shipment and tracking data.
- Booking confirmation step.
- At least three seeded or demonstrable tracking scenarios.
- Short design note covering architecture, model choice, external service, assumptions, and known limitations.
- A two-minute walkthrough or live demonstration of booking, confirmation, and tracking.


## IndiaShipments Agent — Build Plan

Revised: the agent design is now a **tool-calling loop with guardrailed tools**, matching your intent — the brief lists tool calling as a valid design, and the earlier "pure state machine" framing wasn't necessary to get reliability. Reliability comes from putting business-rule checks *inside* the tools, not from taking the decision-making away from the LLM.

## Decisions locked in

| Decision | Choice |
|---|---|
| LLM | Google Gemini (function-calling / tool-use mode) |
| Agent design | LLM tool-calling loop, with guardrailed tools — business rules run deterministically inside each tool, and state-changing tools refuse to run unless their preconditions are already true |
| Backend | FastAPI |
| Frontend | Streamlit chat UI |
| Database | MySQL (already installed locally) |
| External integration | India Post PIN lookup — `api.postalpincode.in` (free, no key) |

## Why tool-calling + guardrails (not a rigid script)

- Matches the brief directly — tool calling is explicitly listed as an acceptable design.
- Gives you the behavior you described: the LLM reads whatever the user says, in any order, and decides on the spot which tool(s) it needs — no fixed question sequence forced on the user.
- The reliability requirement is met by the tools, not by restricting the LLM: every tool that changes real state is a thin wrapper around plain Python, and the tool itself checks its preconditions before doing anything. If the model calls something out of order, the tool returns an error, the model sees it, and the conversation self-corrects on the next turn.
- Nothing user-facing (price, reference, tracking status) is ever stated unless a tool actually returned it — the model summarizes tool output, it doesn't generate facts. This is exactly the brief's "never invent a price, reference, tracking event, or delivery status" rule.

## Architecture

```
Streamlit (chat UI, holds session_id)
        │  HTTP
        ▼
FastAPI backend
   ├─ Gemini (tool-calling loop: picks which tool to call, then phrases the reply)
   ├─ Tool layer (each tool = deterministic Python + precondition checks)
   ├─ Business rule engine (validation, contents rules — lives inside validate_shipment)
   ├─ PIN lookup client → api.postalpincode.in
   └─ MySQL: shipments, tracking_events, documents, conversation_state
```

## Data model (MySQL)

- **shipments**: id, reference, status, sender_json, recipient_json, package_json (weight/dims), service_type, contents, declared_value, validated (bool), insurance_ack (bool), created_at, updated_at
- **tracking_events**: id, shipment_id, status, timestamp, note
- **documents**: id, shipment_id, filename, type, status (pending/accepted/rejected), uploaded_at
- **conversation_state**: session_id, draft_shipment_id, last_tool_error (for self-correction context)

## Tools the agent can call

The LLM decides *when* to call which tool, based on the conversation. It never decides *what happens inside* one.

| Tool | What it does | Guardrail |
|---|---|---|
| `extract_fields(text, known_fields)` | Pulls structured fields (weight, city, PIN, contents...) from free text | none — read-only |
| `save_draft(session_id, fields)` | Upserts the draft shipment | none — always safe |
| `check_pin_serviceability(pin)` | Calls the PIN API for city/state | returns an "unavailable" flag on failure, never blocks silently |
| `validate_shipment(session_id)` | Runs the full rule engine (dimensions, weight, value, blocked/conditional contents) | sets `validated=true` only on a real pass; returns specific failure reasons otherwise |
| `request_document(session_id, doc_type)` | Marks the draft as waiting on a document | blocks `confirm_booking` while pending |
| `submit_document(session_id, file)` | Stores the upload, marks it reviewed (simulated) | clears the document block |
| `acknowledge_insurance(session_id)` | Records explicit user ack for value > ₹50,000 | required before confirm when triggered |
| `get_summary(session_id)` | Returns the full draft for the model to present | none — read-only |
| `calculate_distance(pin_a, pin_b)` | Looks up both PINs in a bundled lat/long dataset and returns straight-line distance (haversine) | returns "unknown" if either PIN isn't in the dataset — never guesses |
| `estimate_price(weight, distance, service_type)` | Computes an estimate with your own formula (base + per-km + per-kg, by service type) | always labeled to the user as an app estimate, not a carrier quote |
| `confirm_booking(session_id)` | Persists the shipment, generates a reference, writes the first tracking event | **errors unless `validated=true`, no pending document, and insurance acked if required** |
| `get_tracking(reference)` | Returns the shipment's real stored tracking events | model may only describe what this returns — no invented status |

The system prompt states the intended order (collect → validate → resolve document/insurance if needed → summarize → confirm) as *guidance* for the model — but the table above is what actually enforces it. Even if the model responds immediately and flexibly to whatever the user says, it cannot book an unvalidated or unconfirmed shipment; the tool call itself would fail.

## Gemini's role

- Read the user's message and decide which tool(s) to call next — this is the actual agent loop.
- Extract structured fields via `extract_fields`.
- Turn tool results into natural-language questions, explanations, and summaries.
- Never state a price, reference, status, or rule outcome that didn't come back from a tool call.

## External integration handling

`check_pin_serviceability` calls the PIN API with a ~3s timeout. On failure it returns an "unavailable" result rather than raising — the model tells the user PIN validation is temporarily unavailable and lets them proceed with an explicit unverified-PIN note, per the brief's "fail clearly, preserve the user's draft."

## Distance and price estimation

I checked: there is no free, key-less public API that returns a real domestic courier price quote in India. What comes up (edesy.in, ShipMitra, FlexiCommerce, ShipPrime, Easyship's rates API) are business-facing calculators/platforms sitting on top of contracted courier accounts — not something you can call anonymously in a few hours. Distance-between-pincodes is similar: the closest real option (logitax.in's E-Way Bill distance API) is a paid, GST-registration-linked service, not a quick public API either.

Given the timebox, this is the practical design:

- **Distance** — bundle a static, open PIN-code → latitude/longitude dataset with the app (public Indian-pincode lat/long CSVs exist on GitHub) and compute sender-to-recipient distance locally with the haversine formula in `calculate_distance`. No live API call, so it can't fail at runtime the way a third-party call could.
- **Serviceability** — keep `check_pin_serviceability` (the PIN lookup API) as your one required "meaningful external integration," confirming the PIN exists and returning its city/state. A PIN the API can't resolve is "not serviceable." If you want distance to affect serviceability (e.g. "beyond X km = not serviceable"), that threshold is your own product decision, not a documented India Post rule — state it explicitly as an assumption in the design note.
- **Price** — `estimate_price` computes a number yourself: base fee + per-km rate × distance + per-kg rate × weight, varied by Standard/Express. Always label it in the UI as "estimated price," never as an official carrier quote — same principle the brief already applies to the shipment reference (don't present a locally generated figure as if it were authoritative).

This keeps the PIN lookup as your genuine external integration while avoiding a second live dependency (distance/pricing) that you'd have to authenticate, rate-limit-handle, and failure-handle in the same few hours.

## Document workflow (simulated, as the brief allows)

Streamlit `file_uploader` → `submit_document` stores the file and moves it to `accepted` after a fixed review step (no real OCR needed). `confirm_booking` won't run while a document is still pending.

## Seeded tracking scenarios (need at least 3)

1. One fully **Delivered** shipment with a complete event history.
2. One **In transit / Out for delivery** (in progress).
3. One **Delivery failed** or **Returned** (problem case).

## Suggested build order (~8 hours)

1. **Hr 1** — MySQL schema (using your existing local instance), seed data, FastAPI CRUD skeleton (SQLAlchemy or a similar ORM/driver for the MySQL connection).
2. **Hr 2** — Write the tools as plain Python functions first, *no LLM involved yet*. Test them directly (a script calling them in the wrong order) and confirm `confirm_booking` genuinely refuses until preconditions are met. This is the part worth getting right before adding the model.
3. **Hr 3** — Wire Gemini's function-calling API to the tool set; system prompt describing the tools and the intended order as guidance.
4. **Hr 4** — Streamlit chat UI wired to the backend loop.
5. **Hr 5** — PIN lookup integration, insurance ack, document upload step end-to-end.
6. **Hr 6** — Error-path testing: what happens when the model calls things out of order, when the PIN API is down, when validation fails — confirm the conversation self-corrects instead of breaking.
7. **Hr 7 (buffer)** — 3 seeded tracking scenarios, polish.
8. **Hr 8 (buffer)** — Design note, README/setup instructions, 2-minute demo recording.

If you run short on time, stop after Hour 5 — everything through there covers the required journey and the core rules; the buffer hours push you from "passes" to "polished."

## Design note must cover (per brief)

- Architecture and why (tool-calling loop with guardrailed tools — LLM decides *when*, code decides *what's allowed*).
- Why Gemini; why the PIN lookup API — what data goes in/out, how you handle its failure/latency, what the app can still do without it.
- Assumptions you made (e.g. your policy on city/PIN mismatch).
- Known limitations (OCR simulated, single external integration, no auth, etc.).