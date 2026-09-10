"""A scripted stand-in for the language model, for testing without quota.

Enabled only by AGENT_MODE=stub. It exists so the user interface can be built
and exercised without spending free-tier model calls on layout work.

What it replaces: the language model, and nothing else. Every tool call below
is the real tool, hitting the real database, enforcing the real rules. A
booking made in stub mode is a genuine booking; validation, blocked contents,
document requirements, the insurance acknowledgement and the city/PIN conflict
all behave exactly as they do in real mode. Only the phrasing is canned.

It is not an agent, and it is not as capable as one. It reads a PIN code out of
an address and settles a city/PIN conflict, but a real model reads a whole
paragraph and fills six fields from it. Judge the conversation in real mode;
use this to judge the screens.
"""

import random
import re

from app import tools
from app.rules import HIGH_VALUE_THRESHOLD_INR

# Per-session scratch state. Process-local on purpose: this is a testing aid
# and does not need to survive a restart.
_last_asked: dict[str, str] = {}
_seen_warnings: dict[str, set[str]] = {}
_pending_conflict: dict[str, dict] = {}
_pending_ack: dict[str, bool] = {}

TRACKING_REFERENCE = re.compile(r"\b(IS-\d+)\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
PIN_IN_TEXT = re.compile(r"\b(\d{6})\b")

# Asking about an order without quoting a reference.
TRACKING_INTENT = re.compile(
    r"\b(track|tracking|status|where is|my order|my orders|my shipment|"
    r"my shipments|delivered|arrive[d]?)\b",
    re.IGNORECASE,
)

# Any of these, once the draft is bookable, means "go ahead".
CONFIRMING = re.compile(
    r"\b(yes|yeah|confirm|book|go ahead|proceed|ok|okay)\b", re.IGNORECASE
)

# Several ways to ask for the same thing, so the conversation does not read
# like a form being recited back.
PHRASINGS = {
    "contents": [
        "What are you sending?",
        "What's going in the parcel?",
        "Tell me what's inside and I'll check we can carry it.",
    ],
    "package.weight_g": [
        "Roughly how much does it weigh?",
        "What's the weight? An approximate figure is fine.",
        "How heavy is it?",
    ],
    "package.dimensions": [
        "What size is the box -- length, width and height?",
        "Could you give me the parcel's measurements?",
        "How big is it? Length, width and height in centimetres is ideal.",
    ],
    "declared_value": [
        "What are the contents worth, in rupees? That's not the postage -- it "
        "sets the compensation limit if the parcel is lost or damaged.",
        "How would you value the contents? We use it to work out cover if "
        "something goes wrong, not to price the delivery.",
    ],
    "service_type": [
        "Which service would you like?",
        "How quickly does it need to get there?",
    ],
    "sender.name": ["Who's sending it?", "What name should I put for the sender?"],
    "sender.phone": [
        "What's the best number for the sender?",
        "Which number should we use for the pickup?",
    ],
    "sender.address": [
        "What's the pickup address? Include the PIN code and I'll do the rest.",
        "Where are we collecting from? A full address with the PIN code is ideal.",
    ],
    "sender.city": ["Which city is that in?", "And the city?"],
    "sender.state": ["Which state?", "And the state?"],
    "sender.pin": ["What's the PIN code there?", "And the six-digit PIN code?"],
    "recipient.name": ["Who's receiving it?", "What's the recipient's name?"],
    "recipient.phone": [
        "And a contact number for them?",
        "What number can the courier reach them on?",
    ],
    "recipient.address": [
        "Where's it going? Include the PIN code if you have it.",
        "What's the delivery address, with the PIN code?",
    ],
    "recipient.city": ["Which city is that?", "And which city?"],
    "recipient.state": ["Which state?", "And the state?"],
    "recipient.pin": ["What's the PIN code?", "And the six-digit PIN code there?"],
}


def _phrase(field: str, fallback: str) -> str:
    return random.choice(PHRASINGS.get(field) or [fallback])


def _record(calls: list, name: str, args: dict, result: dict) -> dict:
    calls.append(
        {"name": name, "args": args, "ok": bool(result.get("ok")), "result": result}
    )
    return result


def _to_grams(text: str) -> float | None:
    match = NUMBER.search(text)
    if not match:
        return None
    value = float(match.group())
    return value * 1000 if re.search(r"\bkgs?\b|kilo", text, re.I) else value


def _dimensions_mm(text: str) -> dict:
    numbers = [float(n) for n in NUMBER.findall(text)]
    if len(numbers) < 3:
        return {}
    factor = 1.0 if re.search(r"\bmm\b|millim", text, re.I) else 10.0
    length, width, height = numbers[:3]
    return {
        "length_mm": length * factor,
        "width_mm": width * factor,
        "height_mm": height * factor,
    }


def _field_to_kwargs(field: str, message: str) -> dict:
    """Turn the user's answer into save_draft arguments.

    An address answer is also mined for a PIN code, so someone who writes their
    whole address is not then asked for the PIN separately.
    """
    if field == "package.dimensions":
        return _dimensions_mm(message)
    if field == "package.weight_g":
        grams = _to_grams(message)
        return {"weight_g": grams} if grams else {}
    if field == "declared_value":
        match = NUMBER.search(message.replace(",", ""))
        return {"declared_value": float(match.group())} if match else {}
    if field == "service_type":
        return {
            "service_type": "Express" if "express" in message.lower() else "Standard"
        }
    if field == "contents":
        return {"contents": message.strip()}

    if field.startswith(("sender.", "recipient.")):
        role, name = field.split(".", 1)
        values = {f"{role}_{name}": message.strip()}
        if name == "address":
            pin = PIN_IN_TEXT.search(message)
            if pin:
                values[f"{role}_pin"] = pin.group(1)
        return values
    return {}


def _fill_from_pin(session_id: str, role: str, calls: list) -> dict | None:
    """Fill city and state from the PIN lookup rather than asking for them.

    Returns a conflict description when the PIN's city disagrees with what the
    user already said.
    """
    summary = tools.get_summary(session_id)
    if not summary.get("ok"):
        return None
    person = (summary.get("draft") or {}).get(role) or {}
    pin = person.get("pin")
    if not pin:
        return None

    looked_up = _record(
        calls,
        "check_pin_serviceability",
        {"pin": pin, "city": person.get("city")},
        tools.check_pin_serviceability(pin, city=person.get("city")),
    )
    if looked_up.get("status") == "mismatch":
        return {
            "role": role,
            "pin": pin,
            "api_city": looked_up.get("api_city"),
            "given_city": looked_up.get("given_city"),
        }
    if looked_up.get("status") == "ok" and not person.get("city"):
        updates = {
            f"{role}_city": looked_up.get("city"),
            f"{role}_state": looked_up.get("state"),
        }
        _record(calls, "save_draft", updates, tools.save_draft(session_id, **updates))
    return None


def _needs_insurance_ack(summary: dict) -> bool:
    value = (summary.get("draft") or {}).get("declared_value")
    return (
        value is not None
        and float(value) > HIGH_VALUE_THRESHOLD_INR
        and not summary.get("insurance_acknowledged")
    )


def _new_warnings(session_id: str, warnings: list[str]) -> list[str]:
    """Mention a rule the first time it applies, not on every single turn."""
    seen = _seen_warnings.setdefault(session_id, set())
    fresh = [w for w in warnings if w not in seen]
    seen.update(warnings)
    return fresh


def reset_session(session_id: str) -> None:
    for store in (_last_asked, _seen_warnings, _pending_conflict, _pending_ack):
        store.pop(session_id, None)


# ---------------------------------------------------------------------------

def stub_turn(session_id: str, message: str) -> dict:
    calls: list[dict] = []
    text = message.strip()
    lowered = text.lower()

    # 1. An unresolved city/PIN conflict comes first. The user's answer must
    #    actually be acted on, or the conversation asks the same question
    #    forever no matter what they say.
    conflict = _pending_conflict.get(session_id)
    if conflict and text:
        keep = None
        api_city = (conflict.get("api_city") or "").lower()
        given_city = (conflict.get("given_city") or "").lower()
        if api_city and api_city in lowered:
            keep = "pin"
        elif given_city and given_city in lowered:
            keep = "city"
        elif "pin" in lowered:
            keep = "pin"

        if keep:
            _pending_conflict.pop(session_id, None)
            resolved = _record(
                calls,
                "resolve_address_conflict",
                {"role": conflict["role"], "keep": keep},
                tools.resolve_address_conflict(session_id, conflict["role"], keep),
            )
            prefix = (
                f"Thanks -- I've recorded the {conflict['role']} city as "
                f"{resolved.get('city')}. "
                if resolved.get("ok")
                else ""
            )
            return _ask_next(session_id, calls, prefix, allow_confirm=False)

        # They answered, but with neither option -- often a third city that is
        # near the PIN but not the one it is registered to. Say so, rather than
        # repeating the identical question and appearing to ignore them.
        conflict["attempts"] = conflict.get("attempts", 0) + 1
        return _respond(
            session_id,
            f"Sorry, I couldn't match that to either option. PIN "
            f"{conflict['pin']} is registered to {conflict['api_city']}, and the "
            f"address you gave says {conflict['given_city']}. Please pick one of "
            "those two -- or give me a different PIN code if neither is right.",
            [f"{conflict['api_city']} is correct",
             f"{conflict['given_city']} is correct"],
            "address_conflict", calls, None,
        )

    # 1b. An outstanding insurance acknowledgement. Their answer has to be
    #     recorded, not merely acknowledged in prose.
    if _pending_ack.get(session_id) and text:
        if re.search(r"\b(acknowledge|accept|agree|understood|yes|ok|okay)\b",
                     lowered):
            _pending_ack.pop(session_id, None)
            _record(calls, "acknowledge_insurance", {},
                    tools.acknowledge_insurance(session_id))
            return _ask_next(
                session_id, calls,
                "Thank you -- that's recorded. ", allow_confirm=False,
            )
        if re.search(r"\b(reduce|lower|change|no)\b", lowered):
            _pending_ack.pop(session_id, None)
            _last_asked[session_id] = "declared_value"
            return _respond(
                session_id,
                "No problem. What value should I record for the contents "
                "instead?",
                [], None, calls, None,
            )

    # 2a. Asking about an order without naming one. Look at what they actually
    #     have rather than asking for a reference they may not possess.
    if not TRACKING_REFERENCE.search(text) and TRACKING_INTENT.search(lowered):
        mine = _record(calls, "list_my_shipments", {},
                       tools.list_my_shipments(session_id))
        shipments = mine.get("shipments") or []

        if not shipments:
            _last_asked.pop(session_id, None)
            return _respond(
                session_id,
                "I've checked, and there aren't any orders on your account yet "
                "-- this would be your first. I'd be glad to get one booked for "
                "you: just tell me what you'd like to send.",
                [], None, calls, None,
            )

        if len(shipments) == 1:
            only = shipments[0]
            tracked = _record(
                calls, "get_tracking", {"reference": only["reference"]},
                tools.get_tracking(only["reference"]),
            )
            _last_asked.pop(session_id, None)
            return _respond(
                session_id,
                tracked.get("explanation", "")
                + "\n\nIs there anything else I can help with?",
                ["Send a parcel"], None, calls, None,
            )

        return _respond(
            session_id,
            "You have a few shipments with us. Which one would you like to "
            "check?",
            [f"{s['reference']} ({s['status']})" for s in shipments[:3]],
            "shipment", calls, None,
        )

    # 2. Tracking, whenever a reference is mentioned.
    reference = TRACKING_REFERENCE.search(text)
    if reference:
        result = _record(
            calls,
            "get_tracking",
            {"reference": reference.group(1)},
            tools.get_tracking(reference.group(1)),
        )
        if not result.get("ok"):
            return _respond(
                session_id,
                result.get("error", "I couldn't find that reference."),
                [], None, calls, None,
            )
        _last_asked.pop(session_id, None)
        return _respond(
            session_id,
            result["explanation"] + "\n\nIs there anything else I can help with?",
            ["Send a parcel", "Track another shipment"],
            None, calls, None,
        )

    # 3. File whatever was asked for last turn.
    asked = _last_asked.get(session_id)
    if asked and text:
        kwargs = _field_to_kwargs(asked, text)
        if kwargs:
            _record(calls, "save_draft", kwargs, tools.save_draft(session_id, **kwargs))
            if asked == "contents":
                _record(
                    calls, "check_contents", {"description": text},
                    tools.check_contents(text),
                )
            if asked.endswith(".address"):
                found = _fill_from_pin(session_id, asked.split(".")[0], calls)
                if found:
                    _pending_conflict[session_id] = found
                    _last_asked.pop(session_id, None)
                    return _respond(
                        session_id,
                        f"One thing to check: PIN {found['pin']} is registered to "
                        f"{found['api_city']}, but the address says "
                        f"{found['given_city']}. Which is right?",
                        [f"{found['api_city']} is correct",
                         f"{found['given_city']} is correct"],
                        "address_conflict", calls, None,
                    )

    return _ask_next(session_id, calls, "", allow_confirm=CONFIRMING.search(lowered))


def _ask_next(session_id: str, calls: list, prefix: str, allow_confirm) -> dict:
    """Work out where the draft stands, then ask for the next missing thing."""
    summary = _record(calls, "get_summary", {}, tools.get_summary(session_id))
    if not summary.get("ok"):
        _last_asked[session_id] = "contents"
        listed = _record(
            calls, "list_options", {"field": "contents_category"},
            tools.list_options("contents_category"),
        )
        return _respond(
            session_id,
            prefix + "Happy to help. What are you sending?",
            listed.get("options", []), "contents_category", calls, None,
        )

    validation = _record(
        calls, "validate_shipment", {}, tools.validate_shipment(session_id)
    )
    # Re-read: validation may have just made the draft bookable.
    summary = tools.get_summary(session_id)

    if summary.get("ready_to_book") and allow_confirm:
        result = _record(
            calls, "confirm_booking", {}, tools.confirm_booking(session_id)
        )
        if result.get("ok"):
            reset_session(session_id)
            return _respond(
                session_id,
                "All booked. Your IndiaShipments tracking reference is "
                f"{result['reference']}.",
                [], None, calls, result["reference"],
            )
        return _respond(
            session_id, f"I can't book this yet. {result.get('error', '')}",
            [], None, calls, None,
        )

    # A city/PIN disagreement is not just an error to recite: it needs a
    # question with an answer that gets acted on. Reported however it arose --
    # from an address, or from a city and PIN given separately.
    for role, check in (validation.get("serviceability") or {}).items():
        if check.get("status") != "mismatch":
            continue
        person = (summary.get("draft") or {}).get(role) or {}
        detail = _record(
            calls, "check_pin_serviceability",
            {"pin": person.get("pin"), "city": person.get("city")},
            tools.check_pin_serviceability(person.get("pin"), city=person.get("city")),
        )
        _pending_conflict[session_id] = {
            "role": role,
            "pin": detail.get("pin"),
            "api_city": detail.get("api_city"),
            "given_city": detail.get("given_city"),
        }
        _last_asked.pop(session_id, None)
        return _respond(
            session_id,
            prefix + (
                f"PIN {detail.get('pin')} is registered to "
                f"{detail.get('api_city')}, but the {role} address says "
                f"{detail.get('given_city')}. Which should I use?"
            ),
            [f"{detail.get('api_city')} is correct",
             f"{detail.get('given_city')} is correct"],
            "address_conflict", calls, None,
        )

    if validation.get("errors"):
        _last_asked.pop(session_id, None)
        return _respond(
            session_id, prefix + " ".join(validation["errors"]), [], None, calls, None
        )

    ask_next = summary.get("ask_next")
    if ask_next is None:
        _last_asked.pop(session_id, None)

        # Nothing is missing, but the draft may still not be bookable. Say what
        # is actually holding it up rather than announcing a summary and
        # inviting a confirmation that the guardrails would refuse.
        if not summary.get("ready_to_book"):
            documents = summary.get("pending_documents") or []
            if documents:
                return _respond(
                    session_id,
                    prefix + "Before I can book this I still need the "
                    f"{', '.join(documents)}. You can attach it below.",
                    [], None, calls, None,
                )

            if _needs_insurance_ack(summary):
                _pending_ack[session_id] = True
                return _respond(
                    session_id,
                    prefix + (
                        f"The contents are declared at Rs "
                        f"{float(summary['draft']['declared_value']):,.0f}, which "
                        f"is above Rs {HIGH_VALUE_THRESHOLD_INR:,}. Our liability "
                        "is limited unless the parcel is insured, so I need you "
                        "to acknowledge that before I can book it."
                    ),
                    ["I acknowledge this", "Reduce the declared value"],
                    "insurance", calls, None,
                )

            return _respond(
                session_id,
                prefix + "This shipment cannot be booked yet. "
                + " ".join(validation.get("errors") or []),
                [], None, calls, None,
            )

        draft = summary["draft"]
        return _respond(
            session_id,
            prefix + (
                f"Here's the shipment: {draft['sender'].get('name')} in "
                f"{draft['sender'].get('city')} sending {draft.get('contents')} to "
                f"{draft['recipient'].get('name')} in "
                f"{draft['recipient'].get('city')}, by "
                f"{draft.get('service_type')}. Use Confirm booking when you're "
                "happy with it, or tell me what to change."
            ),
            [], None, calls, None,
        )

    field = validation.get("ask_next_field") or ""
    _last_asked[session_id] = field

    options, expects = [], None
    option_list = summary.get("ask_next_options")
    if option_list:
        listed = _record(
            calls, "list_options", {"field": option_list},
            tools.list_options(option_list),
        )
        options, expects = listed.get("options", []), option_list

    fresh = _new_warnings(session_id, validation.get("warnings", []))
    lead = (" ".join(fresh) + " ") if fresh else ""
    question = _phrase(field, f"What is the {ask_next}?")
    return _respond(session_id, prefix + lead + question, options, expects, calls, None)


def _respond(session_id, reply, options, expects, calls, reference) -> dict:
    from app.agent.loop import _build_state

    state = _build_state(session_id, reference)
    state["stub_mode"] = True
    return {
        "reply": reply,
        "options": options,
        "expects": expects,
        "allow_free_text": True,
        "state": state,
        "tool_calls": calls,
        "degraded": False,
    }
