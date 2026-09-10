"""A scripted stand-in for the language model, for testing without quota.

Enabled only by AGENT_MODE=stub. It exists so the user interface can be built
and exercised without spending free-tier model calls on layout work.

What it replaces: the language model, and nothing else. Every tool call below
is the real tool, hitting the real database, enforcing the real rules. A
booking made in stub mode is a genuine booking; validation, blocked contents,
document requirements and the insurance acknowledgement all behave exactly as
they do in real mode. Only the phrasing is canned.

It is not an agent. It follows whatever `ask_next` says to ask for and files
the answer against that field, which is enough to drive every screen state.
"""

import re

from app import tools

# Which field the stub asked about last, per session. Process-local on purpose:
# this is a testing aid and does not need to survive a restart.
_last_asked: dict[str, str] = {}

TRACKING_REFERENCE = re.compile(r"\b(IS-\d+)\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")

AFFIRMATIVE = {"yes", "yes please", "book it", "confirm", "go ahead", "ok", "okay",
               "yes, please book it", "please book it", "sure"}


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
    """Read '30 by 20 by 15 cm' or '300x200x150 mm' into millimetres."""
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
    """Turn the user's answer into save_draft arguments for the field asked."""
    if field == "package.dimensions":
        return _dimensions_mm(message)
    if field == "package.weight_g":
        grams = _to_grams(message)
        return {"weight_g": grams} if grams else {}
    if field == "declared_value":
        match = NUMBER.search(message.replace(",", ""))
        return {"declared_value": float(match.group())} if match else {}
    if field == "service_type":
        chosen = "Express" if "express" in message.lower() else "Standard"
        return {"service_type": chosen}
    if field == "contents":
        return {"contents": message.strip()}
    if field.startswith(("sender.", "recipient.")):
        role, name = field.split(".", 1)
        return {f"{role}_{name}": message.strip()}
    return {}


def stub_turn(session_id: str, message: str) -> dict:
    """One scripted turn. Real tools, canned wording."""
    calls: list[dict] = []
    booked_reference = None
    options: list[str] = []
    expects = None
    text = message.strip()
    lowered = text.lower()

    # 1. Tracking, whenever a reference is mentioned.
    reference = TRACKING_REFERENCE.search(text)
    if reference:
        result = _record(
            calls, "get_tracking", {"reference": reference.group(1)},
            tools.get_tracking(reference.group(1)),
        )
        reply = (
            result["explanation"]
            if result.get("ok")
            else result.get("error", "That reference could not be found.")
        )
        return _respond(session_id, reply, options, expects, calls, None)

    # 2. File whatever was asked for last turn.
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

    # 3. An explicit yes books it, if the guardrails allow.
    if lowered in AFFIRMATIVE or "book it" in lowered:
        _record(calls, "validate_shipment", {}, tools.validate_shipment(session_id))
        result = _record(
            calls, "confirm_booking", {}, tools.confirm_booking(session_id)
        )
        if result.get("ok"):
            booked_reference = result["reference"]
            reply = (
                f"Booked. Your IndiaShipments tracking reference is "
                f"{booked_reference}."
            )
            _last_asked.pop(session_id, None)
            return _respond(session_id, reply, [], None, calls, booked_reference)
        reply = f"I can't book this yet. {result.get('error', '')}"
        return _respond(session_id, reply, [], None, calls, None)

    # 4. Otherwise ask for the next outstanding thing.
    summary = _record(calls, "get_summary", {}, tools.get_summary(session_id))
    if not summary.get("ok"):
        _last_asked[session_id] = "contents"
        listed = _record(
            calls, "list_options", {"field": "contents_category"},
            tools.list_options("contents_category"),
        )
        return _respond(
            session_id,
            "Happy to help you send a parcel. What does it contain?",
            listed.get("options", []), "contents_category", calls, None,
        )

    validation = _record(
        calls, "validate_shipment", {}, tools.validate_shipment(session_id)
    )
    if validation.get("errors"):
        _last_asked.pop(session_id, None)
        return _respond(
            session_id, " ".join(validation["errors"]), [], None, calls, None
        )

    ask_next = summary.get("ask_next")
    if ask_next is None:
        if summary.get("ready_to_book"):
            draft = summary["draft"]
            reply = (
                f"Here is the shipment: {draft['sender'].get('name')} in "
                f"{draft['sender'].get('city')} to {draft['recipient'].get('name')} "
                f"in {draft['recipient'].get('city')}, {draft.get('contents')}, "
                f"{draft.get('service_type')}. Shall I book it?"
            )
        else:
            blockers = summary.get("pending_documents") or []
            reply = (
                f"Before booking I still need: {', '.join(blockers)}."
                if blockers
                else "Everything is recorded. Shall I book it?"
            )
        _last_asked.pop(session_id, None)
        return _respond(session_id, reply, [], None, calls, None)

    field = validation.get("ask_next_field") or ""
    _last_asked[session_id] = field

    option_list = summary.get("ask_next_options")
    if option_list:
        listed = _record(
            calls, "list_options", {"field": option_list},
            tools.list_options(option_list),
        )
        options, expects = listed.get("options", []), option_list

    warnings = " ".join(validation.get("warnings", []))
    prefix = f"{warnings} " if warnings else ""
    return _respond(
        session_id, f"{prefix}What is the {ask_next}?", options, expects, calls, None
    )


def _respond(session_id, reply, options, expects, calls, reference) -> dict:
    from app.agent.loop import _build_state

    state = _build_state(session_id, reference)
    # The UI reads this to show its "no model is being called" banner.
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
