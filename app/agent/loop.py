"""The agent loop.

One user message in, one structured reply out. In between, the model may call
tools as many times as it needs. The loop executes them, feeds the results
back, and repeats until the model produces prose.

Nothing here decides whether an action is allowed -- the tools do that. This
module's jobs are: inject the session, keep the transcript, cap the loop, and
shape the response the UI needs.
"""

import logging
import re
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app import tools
from app.agent.declarations import CALLABLE_TOOLS, DECLARATIONS, SESSION_SCOPED
from app.agent.prompt import SYSTEM_PROMPT
from app.config import get_settings
from app.db import SessionLocal
from app.models import ConversationState
from app.rules import HIGH_VALUE_THRESHOLD_INR

logger = logging.getLogger(__name__)

# A turn that has not settled after this many rounds is stopped, so a model
# that loops on a failing tool cannot spin indefinitely.
MAX_TOOL_ROUNDS = 8

# Gemini can return a STOP with no content; ask again before giving up.
EMPTY_RESPONSE_RETRIES = 2

_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise ModelUnavailable(
                "GEMINI_API_KEY is not set.", reason="misconfigured"
            )
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


class ModelUnavailable(Exception):
    """The model could not be reached. The draft is untouched and still valid.

    `reason` distinguishes a misconfiguration from a busy service, because
    "try again in a moment" is useless advice when the API key is wrong.
    """

    def __init__(self, message: str, reason: str = "unavailable"):
        super().__init__(message)
        self.reason = reason


# The free tier allows only a handful of requests per minute, and every tool
# round costs one. Rather than failing the turn, wait for the window the API
# itself names and try again.
RATE_LIMIT_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 45


def _retry_delay_from(error: genai_errors.APIError) -> float | None:
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(error))
    return float(match.group(1)) if match else None


# Turning thinking off saves latency and tokens, but only some models accept
# the setting -- others reject the whole request. Which is which is discovered
# once, on the first rejection, rather than hard-coded per model name.
_thinking_budget_supported: bool | None = None


def _generate(client, model, contents, build_config):
    """One model call.

    Retries while the API says the quota will free up, and drops the thinking
    setting if this model rejects it.
    """
    global _thinking_budget_supported

    for attempt in range(RATE_LIMIT_RETRIES + 1):
        include_thinking = _thinking_budget_supported is not False
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=build_config(include_thinking)
            )
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                if attempt == RATE_LIMIT_RETRIES:
                    raise ModelUnavailable(
                        "The language model is rate limited right now.",
                        reason="rate_limited",
                    ) from exc
                wait = min(_retry_delay_from(exc) or 20.0, MAX_RETRY_WAIT_SECONDS) + 1
                logger.warning("Rate limited by Gemini; waiting %.0fs", wait)
                time.sleep(wait)
                continue

            if exc.code == 400 and include_thinking and _thinking_budget_supported is None:
                logger.info("%s rejects thinking_budget; retrying without it", model)
                _thinking_budget_supported = False
                continue

            # Anything else is a real fault. Log it in full -- masking a 400 as
            # "service unavailable" once cost an afternoon.
            logger.error("Gemini rejected the request (%s): %s", exc.code, exc)
            text = str(exc).lower()
            misconfigured = (
                "api key not valid" in text
                or "api_key_invalid" in text
                or "permission" in text
            )
            raise ModelUnavailable(
                f"The model rejected the request: {exc}",
                reason="misconfigured" if misconfigured else "rejected",
            ) from exc

        except genai_errors.ServerError as exc:
            if attempt == RATE_LIMIT_RETRIES:
                raise ModelUnavailable("The language model is unavailable.") from exc
            logger.warning("Gemini server error, retrying: %s", exc)
            time.sleep(2 * (attempt + 1))

    raise ModelUnavailable("The language model is unavailable.")


@dataclass
class ToolCallRecord:
    """One executed tool call, surfaced to the UI for transparency."""

    name: str
    args: dict
    ok: bool
    result: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transcript persistence
# ---------------------------------------------------------------------------

def load_history(session_id: str) -> list[types.Content]:
    db = SessionLocal()
    try:
        state = db.get(ConversationState, session_id)
        raw = (state.history_json if state else None) or []
    finally:
        db.close()
    return [types.Content.model_validate(item) for item in raw]


def save_history(session_id: str, history: list[types.Content]) -> None:
    db = SessionLocal()
    try:
        state = db.get(ConversationState, session_id)
        if state is None:
            state = ConversationState(session_id=session_id)
            db.add(state)
        state.history_json = [c.model_dump(mode="json", exclude_none=True) for c in history]
        db.commit()
    finally:
        db.close()


def reset_conversation(session_id: str) -> None:
    db = SessionLocal()
    try:
        state = db.get(ConversationState, session_id)
        if state is not None:
            db.delete(state)
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, args: dict, session_id: str) -> dict:
    """Run one tool. A failure becomes a readable result, never an exception."""
    function = CALLABLE_TOOLS.get(name)
    if function is None:
        return {"ok": False, "error": f"There is no tool called '{name}'."}

    call_args = dict(args)
    if name in SESSION_SCOPED:
        call_args["session_id"] = session_id

    try:
        result = function(**call_args)
    except TypeError as exc:
        return {"ok": False, "error": f"{name} was called with wrong arguments: {exc}"}
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return {
            "ok": False,
            "error": (
                f"{name} could not complete because of an internal error: {exc}. "
                "Tell the user plainly and do not claim it succeeded."
            ),
        }

    return result if isinstance(result, dict) else {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _quote(draft: dict) -> dict:
    """Distance and an estimated price, whenever the draft allows one.

    Worked out by the application rather than left to the model to remember:
    it treated both tools as optional, so the customer saw a price on some
    bookings and not on others. Both lookups are cached, so this costs nothing
    after the first time. An unavailable distance yields no price at all --
    never a guessed one.
    """
    sender = (draft.get("sender") or {}).get("pin")
    recipient = (draft.get("recipient") or {}).get("pin")
    if not (sender and recipient):
        return {}

    # Distance as soon as both PIN codes are known. Waiting for the weight and
    # the service meant it only appeared near the end of the conversation,
    # which is not when someone wants to know how far their parcel is going.
    distance = tools.calculate_distance(sender, recipient)
    if distance.get("status") != "ok":
        return {"distance_km": None, "estimated_price_inr": None}

    quote = {"distance_km": distance["distance_km"], "estimated_price_inr": None}

    weight = (draft.get("package") or {}).get("weight_g")
    service = draft.get("service_type")
    if weight and service:
        price = tools.estimate_price(
            weight_g=weight, service_type=service,
            distance_km=distance["distance_km"],
        )
        if price.get("status") == "ok":
            quote["estimated_price_inr"] = price["estimated_price_inr"]
    return quote


# The document a sentence can ask for, and the contents that require it. Only
# rule-driven documents belong here: a prescription is required by medicines and
# by nothing else, so the word in a request is enough to know what is being sent.
DOCUMENT_PROMISES = {"prescription": "medicines"}

# A reply only promises an upload if it actually asks for one. Merely naming a
# document -- "books need no prescription" -- is not a request.
ASKS_FOR_IT = ("attach", "upload", "provide", "share", "supply", "send me",
               "send us", "give me")


def _honour_document_promise(session_id: str, reply: str) -> None:
    """Make sure a document the agent just asked for is one we are really holding.

    The upload control is drawn from the pending documents in the database. A
    model that asks for a prescription without having screened the contents --
    resuming a conversation, say, where it already knows what is being sent --
    leaves the customer looking for a control that was never put on screen, and
    the honest-sounding explanation it reaches for next ("I cannot receive files
    here") is simply untrue.

    Screening through check_contents rather than writing the row directly keeps
    every guard that tool has: a draft already holding different contents is
    left alone, and a booked shipment is never reopened.
    """
    spoken = reply.lower()
    if not any(phrase in spoken for phrase in ASKS_FOR_IT):
        return

    for doc_type, contents in DOCUMENT_PROMISES.items():
        if doc_type not in spoken:
            continue
        summary = tools.get_summary(session_id)
        if summary.get("ok") and summary.get("pending_documents"):
            return  # already holding it
        recorded = tools.check_contents(contents, session_id=session_id)
        if recorded.get("recorded_on_draft"):
            logger.info(
                "Recorded '%s' for session %s: the reply asked for a %s that "
                "nothing had raised.", contents, session_id, doc_type
            )
        return


def _build_state(session_id: str, reference: str | None) -> dict:
    """The live draft panel's data, read straight from the database."""
    summary = tools.get_summary(session_id)
    if not summary.get("ok"):
        return {
            "has_draft": False,
            "validated": False,
            "blockers": [],
            "still_missing": [],
            "ready_to_book": False,
            "reference": reference,
        }

    blockers = [f"{d} required" for d in summary.get("pending_documents", [])]
    if not summary.get("insurance_acknowledged"):
        draft = summary.get("draft") or {}
        value = draft.get("declared_value")
        if value is not None and float(value) > HIGH_VALUE_THRESHOLD_INR:
            blockers.append("insurance acknowledgement required")

    return {
        "has_draft": True,
        "draft": summary.get("draft"),
        **_quote(summary.get("draft") or {}),
        # Which fixed-choice list, if any, belongs with the next question.
        "ask_next_options": summary.get("ask_next_options"),
        "validated": summary.get("validated"),
        "blockers": blockers,
        "still_missing": summary.get("still_missing", []),
        "ready_to_book": summary.get("ready_to_book"),
        "reference": reference,
    }


# What a question about each fixed-choice field actually sounds like. The
# draft says which field comes next, but the model does not always ask about
# that field -- and buttons under an unrelated question are worse than no
# buttons, because tapping one answers something nobody asked.
FIELD_LANGUAGE = {
    "contents_category": (
        "contain", "inside", "sending", "what kind", "what are you", "items",
        "parcel hold", "packing",
    ),
    "service_type": (
        "service", "standard", "express", "how quickly", "how fast", "speed",
        "delivery option",
    ),
    "insurance": ("acknowledge", "insur", "liability", "declared value is"),
    "sender_address": ("saved", "sender", "collecting", "sending it", "from your"),
    "sender_previous": ("last shipment", "previous", "sender", "collecting"),
}


def _fits_the_question(field: str | None, reply: str, options: list[str]) -> bool:
    """Do these buttons answer the question that was actually asked?"""
    if not field or not options:
        return False
    spoken = reply.lower()
    if any(option.lower() in spoken for option in options):
        return True
    return any(phrase in spoken for phrase in FIELD_LANGUAGE.get(field, ()))


def _options_for(calls: list[ToolCallRecord], state: dict,
                 reply: str = "") -> tuple[list[str], str | None]:
    """The choices to show as buttons.

    Taken from the model's own `list_options` call when it made one, and
    otherwise from the field the draft says is next. Deciding this here rather
    than relying on the model to ask means the buttons appear whether or not
    it remembered -- and it frequently does not.
    """
    # The draft's own next question wins. The model sometimes fetches the list
    # for a field it is thinking ahead to rather than the one it just asked
    # about, which put Standard/Express under a question about the address.
    field = state.get("ask_next_options")
    if field:
        listed = tools.list_options(field)
        options = listed.get("options", []) if listed.get("ok") else []
        if options and _fits_the_question(field, reply, options):
            return options, field
        if options:
            logger.info(
                "Not offering %s buttons: the reply asks about something else",
                field,
            )
        return [], None

    if state.get("has_draft"):
        # The draft knows what is being asked, and it says this question has no
        # fixed choices. Showing the model's last list here would offer answers
        # to a question nobody asked.
        return [], None

    for call in reversed(calls):
        if call.name == "list_options" and call.ok:
            return call.result.get("options", []), call.result.get("field")
    return [], None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

# "Can I send X?" is an acceptance question even though no booking has started,
# and the model has been observed answering it from its own knowledge -- saying
# a spare power bank is fine when our rules block it. The screening is run here
# so the verdict is in front of it before it writes a word.
ASKS_IF_ALLOWED = re.compile(
    r"\b(can|could|may|am i able to)\s+(i|we|you)\s+(send|ship|post|carry)\b"
    r"|\b(is|are)\s+.{0,40}\b(allowed|permitted|prohibited|banned|acceptable)\b"
    r"|\bdo you (accept|carry|take)\b",
    re.IGNORECASE,
)


def _record_chosen_option(session_id: str, message: str) -> None:
    """Save an answer the customer picked from our own list.

    The buttons come from the application, so their labels map onto fields we
    own. Relying on the model to pass the choice through to save_draft meant a
    customer could tap "Medicines", be told a prescription was needed, and
    still have the contents recorded as blank.
    """
    from app.rules import CONTENTS_CATEGORIES

    answer = message.strip()
    lowered = answer.lower()

    if any(lowered == option.lower() for option in CONTENTS_CATEGORIES):
        if lowered != "other":  # "Other" says nothing about the contents
            tools.save_draft(session_id=session_id, contents=answer)
        return

    if lowered in {"standard", "express"}:
        tools.save_draft(session_id=session_id, service_type=answer.capitalize())
        return

    # Answers to the sender question. Acting on them here means the offer is
    # never repeated after it has been answered.
    if lowered == "use my saved details":
        tools.prefill_sender_from_profile(session_id)
    elif lowered == "use my last shipment's details":
        tools.prefill_sender_from_last_shipment(session_id)
    elif lowered in {"a different sender", "someone else is sending"}:
        tools.decline_sender_offer(session_id)


def _acceptance_note(message: str) -> str:
    """Screen an acceptance question before the model answers it."""
    if not ASKS_IF_ALLOWED.search(message):
        return ""

    decision = tools.check_contents(message)
    reasons = " ".join(decision.get("reasons") or []) or "No condition applies."
    return (
        "\n\n[IndiaShipments acceptance check on the message above -- verdict: "
        f"{decision['decision']}. {reasons} Answer using this verdict and these "
        "words only. Do not contradict it or add rules of your own.]"
    )


DEGRADED_REPLIES = {
    "misconfigured": (
        "The assistant isn't set up correctly on this deployment -- its "
        "language model credentials are missing or invalid. Nothing you've "
        "told me has been lost, and everything else on the site still works. "
        "Please let us know so it can be fixed."
    ),
    "rate_limited": (
        "The assistant has reached its usage limit for the moment, so I "
        "couldn't process that message. Nothing you've told me has been lost "
        "-- your draft is saved. Please try again shortly."
    ),
}
DEFAULT_DEGRADED = (
    "I can't reach the assistant service at the moment, so I couldn't process "
    "that message. Nothing you've told me has been lost -- your draft is "
    "saved. Please try again in a moment."
)


def _degraded(session_id: str, exc: "ModelUnavailable") -> dict:
    """A turn that could not run, reported for what it actually was."""
    reason = getattr(exc, "reason", "unavailable")
    logger.error("Turn abandoned (%s): %s", reason, exc)
    return {
        "reply": DEGRADED_REPLIES.get(reason, DEFAULT_DEGRADED),
        "options": [],
        "expects": None,
        "allow_free_text": True,
        "state": _build_state(session_id, None),
        "tool_calls": [],
        "degraded": True,
    }


def run_turn(session_id: str, message: str) -> dict:
    """Process one user message and return the structured reply."""
    settings = get_settings()

    if settings.agent_mode.lower() == "stub":
        logger.warning(
            "AGENT_MODE=stub -- replies are canned and no model is being called"
        )
        from app.agent.stub import stub_turn

        return stub_turn(session_id, message)

    try:
        client = get_client()
    except ModelUnavailable as exc:
        return _degraded(session_id, exc)

    _record_chosen_option(session_id, message)

    history = load_history(session_id)
    history.append(
        types.Content(
            role="user", parts=[types.Part(text=message + _acceptance_note(message))]
        )
    )

    def build_config(include_thinking: bool) -> types.GenerateContentConfig:
        options = dict(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=DECLARATIONS)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            temperature=0.3,
        )
        if include_thinking:
            # Extended thinking adds latency and token cost without helping
            # here: the reasoning this agent needs lives in the tools.
            options["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**options)

    executed: list[ToolCallRecord] = []
    booked_reference: str | None = None
    reply = ""
    empty_responses = 0

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = _generate(client, settings.gemini_model, history, build_config)
        except ModelUnavailable as exc:
            # Everything the user has told us is already in the database, so the
            # draft survives; only this turn is lost.
            return _degraded(session_id, exc)

        candidate = response.candidates[0] if response.candidates else None
        parts = (candidate.content.parts if candidate and candidate.content else None) or []
        function_calls = [p.function_call for p in parts if p.function_call]

        if not parts:
            # Gemini occasionally returns a STOP with no content at all. An empty
            # turn must not be written into the transcript, so ask again rather
            # than corrupting the history with a blank model message.
            empty_responses += 1
            logger.warning(
                "Empty model response (%d) for session %s", empty_responses, session_id
            )
            if empty_responses <= EMPTY_RESPONSE_RETRIES:
                continue
            reply = (
                "Sorry, I didn't manage to put that into words. Could you say "
                "that again?"
            )
            break

        history.append(candidate.content)

        if not function_calls:
            reply = "".join(p.text for p in parts if p.text).strip()
            break

        response_parts = []
        for call in function_calls:
            args = dict(call.args or {})
            result = execute_tool(call.name, args, session_id)
            executed.append(
                ToolCallRecord(
                    name=call.name,
                    args=args,
                    ok=bool(result.get("ok")),
                    result=result,
                )
            )
            if call.name == "confirm_booking" and result.get("ok"):
                booked_reference = result.get("reference")

            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result)
            )

        history.append(types.Content(role="user", parts=response_parts))
    else:
        # The model never settled on a reply within the cap.
        reply = (
            "I'm having trouble completing that step right now. Could you tell me "
            "again what you'd like to do, and I'll pick it up from there?"
        )

    if not reply:
        reply = (
            "Sorry, I didn't manage to put that into words. Could you say that "
            "again?"
        )

    save_history(session_id, history)

    last_error = next(
        (c.result.get("error") for c in reversed(executed) if not c.ok), None
    )
    if last_error:
        db = SessionLocal()
        try:
            state = db.get(ConversationState, session_id)
            if state:
                state.last_tool_error = last_error
                db.commit()
        finally:
            db.close()

    _honour_document_promise(session_id, reply)

    state = _build_state(session_id, booked_reference)
    options, expects = _options_for(executed, state, reply)

    return {
        "reply": reply,
        "options": options,
        "expects": expects,
        "allow_free_text": True,
        "state": state,
        "tool_calls": [
            {"name": c.name, "args": c.args, "ok": c.ok, "result": c.result}
            for c in executed
        ],
    }
