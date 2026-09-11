"""The tools the agent can call.

Every function here is plain, deterministic Python and is callable without an
LLM. The model decides *when* to call a tool; this module decides what happens
inside one, and refuses when a precondition is not met.

Each tool returns a JSON-serialisable dict containing `ok`. A refusal is a
normal return value with `ok: False` and a readable `error`, never an exception
-- the model reads the error and self-corrects on the next turn.

Arguments are flat scalars rather than nested objects because that is what
model function-calling schemas handle reliably.
"""

import re
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select

from app.constants import (
    DOC_ACCEPTED,
    DOC_PENDING,
    REFERENCE_PREFIX,
    SERVICE_TYPES,
    STATUS_BOOKED,
    STATUS_DRAFT,
)
from app.db import SessionLocal
from app.models import (
    ConversationState,
    Customer,
    Document,
    Shipment,
    TrackingEvent,
)
from app.rules import (
    CONTENTS_CATEGORIES,
    HIGH_VALUE_THRESHOLD_INR,
    MAX_DECLARED_VALUE_INR,
    MAX_TEXT_LENGTH,
    classify_contents,
    normalise_service,
    validate_draft,
)
from app.services import geocode, pin_api

# --- Pricing model. This is the application's own estimate, not a carrier
# quote, and it is always labelled as such to the user. ---
PRICE_TABLE = {
    "Standard": {"base_inr": 40.0, "per_kg_inr": 30.0, "per_km_inr": 0.35},
    "Express": {"base_inr": 80.0, "per_kg_inr": 55.0, "per_km_inr": 0.60},
}

# Document types the rule engine raises on its own, and may therefore withdraw
# on its own. A document requested explicitly is never withdrawn automatically.
RULE_DRIVEN_DOC_TYPES = ["prescription"]

# How people actually write a measurement, and what it means in the unit the
# field is stored in. Interpreting this here rather than leaving it to the model
# is the same principle as everything else in this layer: the model is good at
# noticing that the customer said "two and a half kilos", and must not be the
# thing that decides what that is in grams.
_UNIT_FACTORS = {
    "weight_g": {
        "kg": 1000.0, "kgs": 1000.0, "kilo": 1000.0, "kilos": 1000.0,
        "kilogram": 1000.0, "kilograms": 1000.0,
        "g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0,
    },
    "_dimension": {
        "mm": 1.0, "millimetre": 1.0, "millimetres": 1.0, "millimeter": 1.0,
        "millimeters": 1.0,
        "cm": 10.0, "centimetre": 10.0, "centimetres": 10.0,
        "centimeter": 10.0, "centimeters": 10.0,
        "m": 1000.0, "metre": 1000.0, "metres": 1000.0, "meter": 1000.0,
        "meters": 1000.0,
        "in": 25.4, "inch": 25.4, "inches": 25.4,
    },
    "declared_value": {
        "rs": 1.0, "rs.": 1.0, "inr": 1.0, "rupee": 1.0, "rupees": 1.0,
        "₹": 1.0,
    },
}

_NUMBER_IN_TEXT = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_measurement(field: str, value) -> tuple[float | None, str | None]:
    """Read a number a person may have written with a unit or a currency symbol.

    "2 kg" in a field counted in grams is 2000, not 2 -- and not a value to be
    quietly discarded either, which is what happens if this only accepts bare
    numbers. A unit that is not recognised is refused rather than guessed at.

    Returns (number, refusal); exactly one is None.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return (None, f"'{value}' is not a usable measurement.") if (
            number != number or number in (float("inf"), float("-inf"))
        ) else (number, None)

    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None, None

    found = _NUMBER_IN_TEXT.search(text)
    if found is None:
        return None, f"'{value}' is not a number, so it was not recorded."

    number = float(found.group())
    remainder = (text[: found.start()] + text[found.end():]).strip()
    remainder = remainder.replace("₹", " ₹ ").strip()
    if not remainder:
        return number, None

    table = _UNIT_FACTORS["_dimension" if field.endswith("_mm") else field]
    words = [word for word in re.split(r"[\s./]+", remainder) if word]
    factor = 1.0
    for word in words:
        if word in table:
            factor = table[word]
        elif word not in ("of", "about", "approx", "approximately", "around"):
            unit = " ".join(words)
            return None, (
                f"'{value}' could not be read as a measurement -- '{unit}' is "
                "not a unit this service recognises, so it was not recorded. "
                "Ask the customer for the number on its own."
            )
    # Rounded because a unit conversion in binary floating point produces
    # 304.79999999999995 for twelve inches, and that number goes on screen.
    return round(number * factor, 2), None


def _screen_value(field: str, value):
    """Decide whether a supplied field is storable, before it reaches the row.

    The rule engine is the place where business rules live, but it only runs
    after the draft has been written, and a number wider than its column or a
    string longer than its column fails at the write with a database error the
    customer can make nothing of. So the few limits that are about storage are
    checked here, in front of the row, and returned as a sentence.

    Returns (value_to_store, refusal). Exactly one of the two is None.
    """
    if value is None:
        return None, None

    if field in ("weight_g", "length_mm", "width_mm", "height_mm",
                 "declared_value"):
        number, refusal = _parse_measurement(field, value)
        if refusal or number is None:
            return None, refusal
        if field == "declared_value" and number > MAX_DECLARED_VALUE_INR:
            return None, (
                f"A declared value of Rs {number:,.0f} is above the "
                f"Rs {MAX_DECLARED_VALUE_INR:,} ceiling for a parcel, so it "
                "was not recorded. Ask the customer to confirm the value."
            )
        # Dimensions and weight are held as JSON, so only the rule engine
        # bounds them; it reports anything out of range as a proper error.
        return number, None

    if field == "service_type":
        # Stored as this application spells it, so the draft panel and the
        # booking agree with the option the customer was offered.
        return normalise_service(value) or str(value).strip() or None, None

    text = str(value).strip()
    if len(text) > MAX_TEXT_LENGTH:
        return None, (
            f"That {field.replace('_', ' ')} is {len(text)} characters long, "
            f"longer than the {MAX_TEXT_LENGTH} this service stores, so it was "
            "not recorded. Ask for a shorter version."
        )
    # An empty string is not an answer; leave whatever was already there.
    return (text or None), None


# Words that describe the role rather than name the person. Saving one of
# these puts something on screen that looks like real data and is not, so they
# are refused and the field stays visibly empty.
PLACEHOLDER_NAMES = {
    "sender", "recipient", "receiver", "me", "myself", "self", "i", "you",
    "customer", "user", "my house", "my home", "my address", "same",
    "n/a", "na", "none", "unknown", "not given", "-",
}


def _is_placeholder(value: str | None) -> bool:
    return bool(value) and value.strip().lower().strip(".") in PLACEHOLDER_NAMES


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Draft plumbing
# ---------------------------------------------------------------------------

def _get_state(db, session_id: str) -> ConversationState:
    state = db.get(ConversationState, session_id)
    if state is None:
        state = ConversationState(session_id=session_id)
        db.add(state)
        db.flush()
    return state


def _get_draft(db, session_id: str) -> Shipment | None:
    state = _get_state(db, session_id)
    if state.draft_shipment_id is None:
        return None
    shipment = db.get(Shipment, state.draft_shipment_id)
    if shipment is None or shipment.status != STATUS_DRAFT:
        return None
    return shipment


def _draft_as_dict(shipment: Shipment) -> dict:
    return {
        "sender": shipment.sender_json or {},
        "recipient": shipment.recipient_json or {},
        "package": shipment.package_json or {},
        "service_type": shipment.service_type,
        "contents": shipment.contents,
        "declared_value": float(shipment.declared_value)
        if shipment.declared_value is not None
        else None,
    }


def _gate_on_document(db, shipment: Shipment, payload: dict) -> dict:
    """Make an outstanding document the only thing being asked for.

    A required document blocks the booking outright, so asking for it and for
    the next detail in the same breath tells the user to do something and then
    talks over the answer. Enforced here rather than in the prompt, because
    guidance in the prompt does not hold.
    """
    pending = _pending_documents(db, shipment.id)
    if not pending:
        return payload

    doc_type = pending[0].doc_type
    payload["awaiting_document"] = doc_type
    payload["ask_next"] = f"the {doc_type}"
    payload["ask_next_options"] = None
    payload["ask_next_instruction"] = (
        f"A {doc_type} is required before this shipment can go any further. "
        "Ask the user to attach it, and ask for NOTHING else -- no weight, no "
        "size, no addresses. There is an upload control on screen for them to "
        "use. Wait until it has been supplied before collecting any other "
        "detail."
    )
    return payload


def _require_document(db, shipment_id: int, doc_type: str) -> None:
    """Record that a document is needed, unless it already is."""
    existing = db.scalars(
        select(Document).where(
            Document.shipment_id == shipment_id,
            Document.doc_type == doc_type,
            Document.status.in_([DOC_PENDING, DOC_ACCEPTED]),
        )
    ).first()
    if existing is None:
        db.add(
            Document(shipment_id=shipment_id, doc_type=doc_type,
                     status=DOC_PENDING)
        )
        db.flush()


def _sync_rule_documents(db, shipment_id: int, required: str | None) -> None:
    """Bring the pending document requests in line with the rules as they stand.

    Requirements this application raises by itself are withdrawn again when the
    rule stops applying -- a user who says "medicines" and then corrects it to
    "books" must not stay blocked on a prescription forever. Only un-uploaded,
    rule-driven requests are cleared; anything requested explicitly, or already
    supplied, is left alone.

    Both save_draft and validate_shipment call this, because the requirement
    follows the contents and the contents can change in either.
    """
    stale = db.scalars(
        select(Document).where(
            Document.shipment_id == shipment_id,
            Document.status == DOC_PENDING,
            Document.uploaded_at.is_(None),
            Document.doc_type.in_(RULE_DRIVEN_DOC_TYPES),
            Document.doc_type != (required or ""),
        )
    ).all()
    for document in stale:
        db.delete(document)
    if stale:
        db.flush()

    if required:
        _require_document(db, shipment_id, required)


SENDER_OFFER_DECLINED = "sender_offer_declined"


def decline_sender_offer(session_id: str) -> dict:
    """Record that the customer is not the sender on this shipment.

    Without this the offer is made again on the next turn, because nothing in
    the draft has changed yet -- they have said no, but not yet said who.
    """
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")
        sender = dict(shipment.sender_json or {})
        sender[SENDER_OFFER_DECLINED] = True
        shipment.sender_json = sender
        return {"ok": True, "note": "Collect the sender's details from the user."}


def _last_sender_used(db, customer_id: int, exclude_id: int) -> dict | None:
    """The sender block from this customer's most recent booking."""
    previous = db.scalars(
        select(Shipment)
        .where(
            Shipment.customer_id == customer_id,
            Shipment.reference.is_not(None),
            Shipment.id != exclude_id,
        )
        .order_by(Shipment.id.desc())
    ).first()
    if previous is None:
        return None
    sender = previous.sender_json or {}
    return sender if sender.get("name") and sender.get("address") else None


def _gate_on_sender_choice(db, session_id: str, shipment: Shipment,
                           payload: dict) -> dict:
    """Ask whose address the parcel is going from, before assuming.

    A customer with an address saved may still be sending on someone else's
    behalf, or from somewhere else today. Enforced here rather than left to
    the prompt, because filling it in silently decides something for them.
    Customers with no saved address are never asked -- there is nothing to
    offer, so their details are collected as normal.
    """
    if payload.get("awaiting_document") or payload.get("awaiting_acknowledgement"):
        return payload

    sender = shipment.sender_json or {}
    if sender.get("name") and sender.get("phone"):
        return payload  # we know who is sending and how to reach them

    if sender.get(SENDER_OFFER_DECLINED):
        # They have said it is somebody else, so the sender's details are what
        # comes next -- not whatever the usual order would have picked. The
        # question on screen and the buttons under it have to be about the same
        # thing, or the customer is asked for an address and offered a list of
        # parcel contents.
        payload["awaiting_sender_details"] = True
        payload["ask_next"] = "the sender's details"
        payload["ask_next_options"] = None
        payload["ask_next_instruction"] = (
            "The customer has said someone else is sending this parcel. Ask for "
            "that person's name, phone number and full address with the PIN "
            "code. They may give it all in one message or a piece at a time -- "
            "take whatever they give and ask only for what is still missing."
        )
        return payload

    state = _get_state(db, session_id)
    if state.customer_id is None:
        return payload
    customer = db.get(Customer, state.customer_id)
    if customer is None:
        return payload

    # Prefer what they saved when registering. Failing that, what they used
    # last time -- a customer who skipped the address at sign-up has still
    # told us one on a previous booking, and retyping it is the annoyance
    # accounts were meant to remove.
    if customer.address and customer.pin:
        known = ", ".join(
            filter(None, [customer.name, customer.phone, customer.address,
                          customer.city, customer.pin])
        )
        source = "saved on their account"
        tool = "prefill_sender_from_profile"
        choices = "sender_address"
    else:
        previous = _last_sender_used(db, customer.id, shipment.id)
        if previous is None:
            return payload  # nothing to offer, so collect it normally
        known = ", ".join(
            filter(None, [previous.get("name"), previous.get("phone"),
                          previous.get("address"), previous.get("city"),
                          previous.get("pin")])
        )
        source = "used on their last shipment"
        tool = "prefill_sender_from_last_shipment"
        choices = "sender_previous"

    payload["awaiting_sender_choice"] = True
    payload["ask_next"] = "whether the sender is this customer"
    payload["ask_next_options"] = choices
    payload["ask_next_instruction"] = (
        f"These sender details are {source}: {known}. Ask whether to use them "
        "for this shipment, or whether the parcel is going from somewhere else "
        "or being sent by somebody else. Ask NOTHING else until they answer. "
        f"If they say yes, call {tool} -- it fills only the blanks, so "
        "anything they have already told you is kept. Otherwise collect the "
        "sender's details from them."
    )
    return payload


def _gate_on_acknowledgement(shipment: Shipment, payload: dict) -> dict:
    """Make the high-value warning the only thing being asked for.

    Same reasoning as the document gate: told to ask for the acknowledgement,
    the model mentioned it and carried on to the next question. It cannot book
    without it, so nothing else should be collected until it is given.
    """
    value = shipment.declared_value
    if value is None or float(value) <= HIGH_VALUE_THRESHOLD_INR:
        return payload
    if shipment.insurance_ack:
        return payload
    if payload.get("awaiting_document"):
        return payload  # the document comes first

    payload["awaiting_acknowledgement"] = True
    payload["ask_next"] = "the insurance acknowledgement"
    payload["ask_next_options"] = "insurance"
    payload["ask_next_instruction"] = (
        f"The contents are declared at Rs {float(value):,.0f}, above the Rs "
        f"{HIGH_VALUE_THRESHOLD_INR:,} threshold. Tell the user our liability "
        "is limited unless the parcel is insured and ask them to acknowledge "
        "it. Ask for NOTHING else until they answer, and call "
        "acknowledge_insurance the moment they agree. If they would rather "
        "lower the declared value, take the new figure instead."
    )
    return payload


def _supplied_documents(db, shipment_id: int) -> list[str]:
    """The document types already received for this shipment."""
    return [
        d.doc_type
        for d in db.scalars(
            select(Document).where(
                Document.shipment_id == shipment_id,
                Document.status == DOC_ACCEPTED,
            )
        )
    ]


def _note_supplied_documents(db, shipment: Shipment, payload: dict) -> dict:
    """Say which documents are already in hand, and retire the rules they meet.

    Without this the only trace of an upload is the absence of a pending
    request. The rules keep reporting that medicines require a prescription --
    which is true of medicines in general and no longer true of this shipment --
    so the agent reads the requirement as outstanding and asks for a document
    the customer has already attached.
    """
    supplied = _supplied_documents(db, shipment.id)
    if not supplied:
        return payload

    payload["documents_received"] = supplied
    payload["documents_received_instruction"] = (
        f"Already received and on file for this shipment: {', '.join(supplied)}. "
        "Do NOT ask for any of these again. If one was just attached, say it "
        "has been received and recorded -- never that it was verified, checked "
        "or approved, and never describe what it contains -- then carry on with "
        "the next detail."
    )

    if payload.get("requires_document") in supplied:
        payload["requires_document"] = None

    # A condition that has been met is no longer a warning to repeat.
    if payload.get("warnings"):
        payload["warnings"] = [
            warning
            for warning in payload["warnings"]
            if not any(doc_type in warning.lower() for doc_type in supplied)
        ]

    return payload


def _pending_documents(db, shipment_id: int) -> list[Document]:
    return list(
        db.scalars(
            select(Document).where(
                Document.shipment_id == shipment_id,
                Document.status == DOC_PENDING,
            )
        )
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def save_draft(
    session_id: str,
    sender_name: str | None = None,
    sender_phone: str | None = None,
    sender_address: str | None = None,
    sender_city: str | None = None,
    sender_state: str | None = None,
    sender_pin: str | None = None,
    recipient_name: str | None = None,
    recipient_phone: str | None = None,
    recipient_address: str | None = None,
    recipient_city: str | None = None,
    recipient_state: str | None = None,
    recipient_pin: str | None = None,
    weight_g: float | None = None,
    length_mm: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    service_type: str | None = None,
    contents: str | None = None,
    declared_value: float | None = None,
) -> dict:
    """Create or update the draft shipment with whatever is known so far.

    Only the arguments actually supplied are written, so partial information
    from one conversational turn never erases what an earlier turn established.
    """
    supplied = {
        "sender_name": sender_name, "sender_phone": sender_phone,
        "sender_address": sender_address, "sender_city": sender_city,
        "sender_state": sender_state, "sender_pin": sender_pin,
        "recipient_name": recipient_name, "recipient_phone": recipient_phone,
        "recipient_address": recipient_address, "recipient_city": recipient_city,
        "recipient_state": recipient_state, "recipient_pin": recipient_pin,
        "weight_g": weight_g, "length_mm": length_mm, "width_mm": width_mm,
        "height_mm": height_mm, "service_type": service_type,
        "contents": contents, "declared_value": declared_value,
    }
    refusals: list[str] = []
    for field, value in list(supplied.items()):
        supplied[field], refusal = _screen_value(field, value)
        if refusal:
            refusals.append(refusal)

    sender_name = supplied["sender_name"]
    sender_phone = supplied["sender_phone"]
    sender_address = supplied["sender_address"]
    sender_city = supplied["sender_city"]
    sender_state = supplied["sender_state"]
    sender_pin = supplied["sender_pin"]
    recipient_name = supplied["recipient_name"]
    recipient_phone = supplied["recipient_phone"]
    recipient_address = supplied["recipient_address"]
    recipient_city = supplied["recipient_city"]
    recipient_state = supplied["recipient_state"]
    recipient_pin = supplied["recipient_pin"]
    weight_g = supplied["weight_g"]
    length_mm = supplied["length_mm"]
    width_mm = supplied["width_mm"]
    height_mm = supplied["height_mm"]
    service_type = supplied["service_type"]
    contents = supplied["contents"]
    declared_value = supplied["declared_value"]

    with session_scope() as db:
        state = _get_state(db, session_id)
        shipment = _get_draft(db, session_id)

        # A booked shipment is never edited. If this conversation has already
        # confirmed one, saving again opens a fresh draft and leaves the booked
        # row exactly as it was booked. Said out loud here, because otherwise a
        # customer who follows "booked!" with "actually make it 5 kg" is quietly
        # started on a second shipment and finds out much later.
        started_after_booking = None
        if shipment is None and state.draft_shipment_id is not None:
            previous = db.get(Shipment, state.draft_shipment_id)
            if previous is not None and previous.reference:
                started_after_booking = previous.reference

        if shipment is None:
            shipment = Shipment(status=STATUS_DRAFT)
            db.add(shipment)
            db.flush()
            state.draft_shipment_id = shipment.id

        sender = dict(shipment.sender_json or {})
        recipient = dict(shipment.recipient_json or {})
        package = dict(shipment.package_json or {})

        def apply(block: dict, updates: list[tuple[str, object]]) -> dict:
            for key, value in updates:
                if value is None:
                    continue
                # A role word in a name field is not information.
                if key == "name" and _is_placeholder(str(value)):
                    continue
                # A city or PIN edit invalidates any earlier acceptance of a
                # mismatch: the user agreed to the old pairing, not this one.
                if key in ("city", "pin") and block.get(key) != value:
                    block.pop("city_pin_accepted", None)
                block[key] = value
            return block

        sender = apply(sender, [
            ("name", sender_name), ("phone", sender_phone),
            ("address", sender_address), ("city", sender_city),
            ("state", sender_state), ("pin", sender_pin),
        ])
        recipient = apply(recipient, [
            ("name", recipient_name), ("phone", recipient_phone),
            ("address", recipient_address), ("city", recipient_city),
            ("state", recipient_state), ("pin", recipient_pin),
        ])

        for key, value in [
            ("weight_g", weight_g), ("length_mm", length_mm),
            ("width_mm", width_mm), ("height_mm", height_mm),
        ]:
            if value is not None:
                package[key] = value

        shipment.sender_json = sender
        shipment.recipient_json = recipient
        shipment.package_json = package

        if service_type is not None:
            shipment.service_type = service_type
        if contents is not None:
            shipment.contents = contents
        if declared_value is not None:
            previous_value = shipment.declared_value
            shipment.declared_value = declared_value
            # An acknowledgement covers the amount that was shown to the user.
            # Change the amount and it must be acknowledged again, otherwise a
            # user who accepted the warning at Rs 60,000 would be treated as
            # having accepted it at Rs 5,00,000.
            if previous_value is None or float(previous_value) != float(declared_value):
                shipment.insurance_ack = False

        # Any change invalidates a previous validation pass, so the agent cannot
        # validate a draft, quietly alter it, and then book the altered version.
        shipment.validated = False

        draft = _draft_as_dict(shipment)
        result = validate_draft(draft)

        as_dict = result.as_dict()
        payload = {
            "ok": True,
            "draft": draft,
            "still_missing_count": len(as_dict["missing_readable"]),
            "ask_next": as_dict["ask_next"],
            "ask_next_options": as_dict["ask_next_options"],
            "ask_next_instruction": as_dict["ask_next_instruction"],
            "note": "Draft saved. It must be validated again before booking.",
        }
        if started_after_booking:
            payload["started_new_shipment"] = started_after_booking
            payload["started_new_shipment_instruction"] = (
                f"Shipment {started_after_booking} is already booked and cannot "
                "be changed here -- these details have started a new shipment "
                "instead. Say so before anything else, and ask the customer "
                "whether that is what they intended. If they wanted to change "
                f"{started_after_booking}, tell them that booking has to be "
                "amended by support, which this assistant cannot do."
            )

        if refusals:
            payload["not_recorded"] = refusals
            payload["not_recorded_instruction"] = (
                "Some of what was supplied could not be stored, for the reasons "
                "listed. Tell the customer plainly which detail was not saved "
                "and why, using those reasons, before asking anything else."
            )
        # Screening the contents here as well as in validate_shipment means the
        # verdict on a prohibited or restricted item always reaches the model as
        # a tool result. Otherwise a model that answers straight after saving
        # would be stating a rule from its own knowledge rather than from ours.
        #
        # This runs before the gates below, not after: the gates read the
        # pending documents, so screening first is what lets a correction --
        # "medicines" changed to "books" -- lift the prescription request in the
        # same turn it was made, instead of one turn later.
        if shipment.contents:
            decision = classify_contents(shipment.contents)
            _sync_rule_documents(db, shipment.id, decision.requires_document)
            payload["contents_check"] = {
                "decision": decision.decision,
                "reasons": decision.reasons,
                "requires_document": decision.requires_document,
                "note": (
                    "Use these reasons verbatim when explaining what can or "
                    "cannot be sent. Do not add rules of your own."
                ),
            }

        payload = _gate_on_sender_choice(
            db, session_id, shipment,
            _gate_on_acknowledgement(
                shipment,
                _gate_on_document(
                    db, shipment, _note_supplied_documents(db, shipment, payload)
                ),
            ),
        )

        return payload


def bind_session(session_id: str, customer_id: int) -> dict:
    """Attach a signed-in customer to this conversation.

    Called by the application at sign-in, not by the model -- the model must
    never be able to decide who it is acting for.
    """
    with session_scope() as db:
        customer = db.get(Customer, customer_id)
        if customer is None:
            return _fail(f"No customer {customer_id} exists.")
        state = _get_state(db, session_id)
        state.customer_id = customer_id
        return {"ok": True, "session_id": session_id, "customer": customer.name}


def session_customer_id(session_id: str) -> int | None:
    """Who, if anyone, is signed in on this conversation.

    Lets the application restore a sign-in after a browser refresh, since the
    conversation and its owner are held in the database rather than in the
    page.
    """
    db = SessionLocal()
    try:
        state = db.get(ConversationState, session_id)
        return state.customer_id if state else None
    finally:
        db.close()


def prefill_sender_from_profile(session_id: str) -> dict:
    """Fill the sender block from the signed-in customer's saved details.

    This is why accounts exist: the user should not retype an address the
    application already holds.
    """
    with session_scope() as db:
        state = _get_state(db, session_id)
        if state.customer_id is None:
            return _fail("Nobody is signed in on this conversation.")
        customer = db.get(Customer, state.customer_id)
        if customer is None:
            return _fail("The signed-in customer no longer exists.")

        sender = customer.as_sender()
        missing = [key for key, value in sender.items() if not value]
        # Only blanks are filled. A customer who has already given a pickup
        # address must not have it replaced by the one on their profile.
        draft = _get_draft(db, session_id)
        already = (draft.sender_json if draft else None) or {}
        to_apply = {
            f"sender_{key}": value
            for key, value in sender.items()
            if value and not already.get(key)
        }

    saved = save_draft(session_id=session_id, **to_apply) if to_apply else {}
    return {
        "ok": True,
        "sender": sender,
        "still_missing_from_profile": missing,
        "ask_next": saved.get("ask_next"),
        "note": (
            "The sender details now come from the user's saved profile. Tell "
            "them which details you have used rather than asking for them "
            "again, and ask only for anything the profile did not cover."
        ),
    }


def prefill_sender_from_last_shipment(session_id: str) -> dict:
    """Reuse the sender details from this customer's previous booking.

    For customers who registered without an address. Fills blanks only, so
    anything already given for this shipment stands.
    """
    with session_scope() as db:
        state = _get_state(db, session_id)
        if state.customer_id is None:
            return _fail("Nobody is signed in on this conversation.")
        draft = _get_draft(db, session_id)
        if draft is None:
            return _fail("There is no shipment draft for this conversation yet.")

        previous = _last_sender_used(db, state.customer_id, draft.id)
        if previous is None:
            return _fail(
                "This customer has no previous shipment to copy the sender "
                "details from. Ask them for the details instead."
            )

        already = draft.sender_json or {}
        to_apply = {
            f"sender_{key}": value
            for key, value in previous.items()
            if key not in ("city_pin_accepted", SENDER_OFFER_DECLINED)
            and value and not already.get(key)
        }

    saved = save_draft(session_id=session_id, **to_apply) if to_apply else {}
    return {
        "ok": True,
        "sender": previous,
        "ask_next": saved.get("ask_next"),
        "note": (
            "The sender details are reused from the customer's last shipment. "
            "Say which details you have used rather than asking for them again."
        ),
    }


def list_my_shipments(session_id: str) -> dict:
    """The signed-in customer's shipments, newest first."""
    with session_scope() as db:
        state = _get_state(db, session_id)
        if state.customer_id is None:
            return _fail("Nobody is signed in on this conversation.")

        shipments = db.scalars(
            select(Shipment)
            .where(
                Shipment.customer_id == state.customer_id,
                Shipment.reference.is_not(None),
            )
            .order_by(Shipment.id.desc())
        ).all()

        return {
            "ok": True,
            "count": len(shipments),
            "shipments": [
                {
                    "reference": s.reference,
                    "status": s.status,
                    "to_city": (s.recipient_json or {}).get("city"),
                    "contents": s.contents,
                }
                for s in shipments
            ],
        }


def resolve_address_conflict(session_id: str, role: str, keep: str) -> dict:
    """Settle a disagreement between a stated city and its PIN code.

    `keep="pin"` rewrites the city and state to the values India Post holds.
    `keep="city"` records that the user stands by the address as written.

    Either way the conflict is settled and stops blocking the booking. Without
    this there is no way out of the disagreement, and the conversation loops on
    the same question no matter what the user answers.
    """
    role = (role or "").strip().lower()
    keep = (keep or "").strip().lower()
    if role not in ("sender", "recipient"):
        return _fail("Role must be 'sender' or 'recipient'.")
    if keep not in ("pin", "city"):
        return _fail(
            "Say whether to keep the address as written ('city') or to use the "
            "location the PIN code belongs to ('pin')."
        )

    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")

        person = dict(
            (shipment.sender_json if role == "sender" else shipment.recipient_json)
            or {}
        )
        pin = person.get("pin")
        if not pin:
            return _fail(f"No PIN code has been given for the {role} yet.")

        if keep == "pin":
            looked_up = pin_api.lookup_pin(pin)
            if looked_up.get("status") != pin_api.STATUS_OK:
                return _fail(
                    f"PIN {pin} could not be looked up just now, so its city "
                    "cannot be applied. Ask the user to confirm the address "
                    "instead."
                )
            person["city"] = looked_up.get("city")
            person["state"] = looked_up.get("state")
            person.pop("city_pin_accepted", None)
            outcome = (
                f"The {role} address now reads {person['city']}, "
                f"{person['state']}, matching PIN {pin}."
            )
        else:
            person["city_pin_accepted"] = True
            outcome = (
                f"Recorded that the {role} address is correct as written "
                f"({person.get('city')}), even though PIN {pin} is registered "
                "elsewhere."
            )

        if role == "sender":
            shipment.sender_json = person
        else:
            shipment.recipient_json = person
        shipment.validated = False  # the draft changed; it must be re-checked

        return {
            "ok": True,
            "role": role,
            "kept": keep,
            "city": person.get("city"),
            "state": person.get("state"),
            "note": outcome + " Re-run validation and carry on with the booking.",
        }


def _record_conditional_contents(session_id: str, description: str,
                                 doc_type: str) -> bool:
    """Put a screened item on the draft so its document requirement is real.

    The upload control on screen is drawn from the pending documents in the
    database. Screening alone used to record nothing, so the agent would say
    "please attach the prescription" and the customer had nothing to attach it
    with -- the requirement existed only in the sentence.

    Only fills a draft with no contents yet. Someone asking "what about
    medicines?" while already sending books is asking a question, not changing
    their parcel, and their draft is left alone.
    """
    with session_scope() as db:
        state = _get_state(db, session_id)
        shipment = _get_draft(db, session_id)
        if shipment is None:
            if state.draft_shipment_id is not None:
                previous = db.get(Shipment, state.draft_shipment_id)
                if previous is not None and previous.reference:
                    return False  # the last shipment is booked; do not reopen it
            shipment = Shipment(status=STATUS_DRAFT)
            db.add(shipment)
            db.flush()
            state.draft_shipment_id = shipment.id
        elif (shipment.contents or "").strip().lower() not in (
            "", description.strip().lower()
        ):
            return False

        shipment.contents = description.strip()[:MAX_TEXT_LENGTH]
        shipment.validated = False
        _sync_rule_documents(db, shipment.id, doc_type)
        return True


def start_new_shipment(session_id: str) -> dict:
    """Put the current draft aside and begin a fresh one.

    "Create a new shipment" was only ever a sentence sent to the model, so the
    draft already attached to the conversation stayed attached: a customer who
    had just finished a parcel of medicines was told their prescription was
    still on file and found the contents already filled in.

    A booked shipment is never touched -- it is a real booking, and the pointer
    to it is simply released. Only an unfinished draft is discarded, along with
    the document requests that belonged to it.
    """
    with session_scope() as db:
        state = _get_state(db, session_id)
        discarded = None

        if state.draft_shipment_id is not None:
            shipment = db.get(Shipment, state.draft_shipment_id)
            if shipment is not None and not shipment.reference:
                discarded = _draft_as_dict(shipment)
                for document in db.scalars(
                    select(Document).where(Document.shipment_id == shipment.id)
                ):
                    db.delete(document)
                db.delete(shipment)
            state.draft_shipment_id = None

        # The "somebody else is sending" flag lives inside the draft's own
        # sender block, so it goes with the row.
        return {
            "ok": True,
            "started_fresh": True,
            "discarded_draft": discarded,
            "note": (
                "The previous draft has been cleared. Nothing is recorded for "
                "this shipment yet -- no contents, no addresses, and no "
                "documents. Do not carry anything over from the shipment "
                "before it; ask for each detail again."
            ),
        }


def check_contents(description: str, session_id: str | None = None) -> dict:
    """Screen a description of the contents against the acceptance rules.

    Cheap, so the agent can consult it before saying anything about whether an
    item may be sent. Without this the model answers from its own knowledge of
    postal regulations, which is exactly what the brief forbids.

    Screening an item that needs a document also records it on the draft, so
    that the requirement the agent is about to describe is one the application
    is actually holding -- and so the upload control appears.
    """
    decision = classify_contents(description)

    recorded = bool(
        session_id
        and decision.requires_document
        and _record_conditional_contents(
            session_id, description, decision.requires_document
        )
    )

    if not decision.requires_document:
        note = ""
    elif recorded:
        note = (
            f" A {decision.requires_document} is required before this shipment "
            "can go any further: say so and ask the user to attach it. There is "
            "an upload control on screen for them to use. Ask for NOTHING else "
            "in that message -- no weight, no size, no addresses. Collect the "
            "rest only after it has been supplied."
        )
    else:
        # Answering a question about something they are not sending. Saying
        # "attach it" here would point at an upload control that is not there.
        note = (
            f" Sending that would require a {decision.requires_document}. This "
            "is an answer to a question, not a change to their shipment, so "
            "explain the rule but do NOT ask them to attach anything yet. If "
            "they decide to send it, record it with save_draft first."
        )

    return {
        "ok": True,
        "description": description,
        "decision": decision.decision,
        "reasons": decision.reasons,
        "requires_document": decision.requires_document,
        "recorded_on_draft": recorded,
        "note": (
            "These reasons are the only grounds you may give. Do not cite "
            "regulations, authorities or classifications that are not stated "
            "here." + note
        ),
    }


def get_summary(session_id: str) -> dict:
    """Return the full draft for the agent to present to the user for review."""
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")

        draft = _draft_as_dict(shipment)
        result = validate_draft(draft)
        pending = _pending_documents(db, shipment.id)

        as_dict = result.as_dict()
        summary = {
            "ok": True,
            "draft": draft,
            "validated": shipment.validated,
            "insurance_acknowledged": shipment.insurance_ack,
            "pending_documents": [d.doc_type for d in pending],
            "still_missing": as_dict["missing_readable"],
            "ask_next": as_dict["ask_next"],
            "ask_next_options": as_dict["ask_next_options"],
            "ask_next_instruction": as_dict["ask_next_instruction"],
            "ready_to_book": (
                shipment.validated
                and not pending
                and (shipment.insurance_ack or not result.requires_insurance_ack)
            ),
        }
        return _gate_on_sender_choice(
            db, session_id, shipment,
            _gate_on_acknowledgement(
                shipment,
                _gate_on_document(
                    db, shipment, _note_supplied_documents(db, shipment, summary)
                ),
            ),
        )


def check_pin_serviceability(pin: str, city: str | None = None) -> dict:
    """Look up an Indian PIN code with India Post and compare it to a stated city.

    An unreachable service returns `unavailable` rather than failing the
    conversation, so the user's draft is preserved.
    """
    result = pin_api.lookup_pin(pin)
    if city:
        result = pin_api.compare_city(result, city)

    serviceable = result.get("status") == pin_api.STATUS_OK
    return {
        "ok": True,
        "pin": pin,
        "status": result.get("status"),
        "serviceable": serviceable,
        "city": result.get("city"),
        "state": result.get("state"),
        "post_offices": result.get("post_offices", []),
        "message": result.get("message"),
        "given_city": result.get("given_city"),
        "api_city": result.get("api_city"),
    }


def validate_shipment(session_id: str) -> dict:
    """Run every business rule against the draft.

    Sets `validated` only on a genuine pass. Serviceability is re-checked here
    against the live PIN service rather than trusting an earlier turn.
    """
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft to validate yet.")

        draft = _draft_as_dict(shipment)

        pin_checks = {}
        for role in ("sender", "recipient"):
            person = draft.get(role) or {}
            pin = person.get("pin")
            if not pin:
                continue
            looked_up = pin_api.lookup_pin(pin)
            pin_checks[role] = pin_api.compare_city(looked_up, person.get("city"))

            # The lookup gives us the city and state for that PIN. Asking the
            # customer for them afterwards is asking for something we hold.
            # Only blanks are filled: whatever they told us stands.
            if looked_up.get("status") == pin_api.STATUS_OK:
                filled = dict(person)
                for field, value in (("city", looked_up.get("city")),
                                     ("state", looked_up.get("state"))):
                    if value and not filled.get(field):
                        filled[field] = value
                if filled != person:
                    draft[role] = filled
                    if role == "sender":
                        shipment.sender_json = filled
                    else:
                        shipment.recipient_json = filled
        draft["pin_checks"] = pin_checks

        result = validate_draft(draft)
        shipment.validated = result.ok

        # Withdraw a requirement the rules no longer raise, and record one they
        # do, so that confirm_booking can block on it even if the model forgets
        # to ask.
        _sync_rule_documents(db, shipment.id, result.requires_document)

        payload = result.as_dict()
        payload.update(
            {
                "ok": True,
                "validated": shipment.validated,
                "serviceability": {
                    role: {
                        "status": check.get("status"),
                        "city": check.get("city"),
                        "state": check.get("state"),
                    }
                    for role, check in pin_checks.items()
                },
            }
        )
        return _gate_on_sender_choice(
            db, session_id, shipment,
            _gate_on_acknowledgement(
                shipment,
                _gate_on_document(
                    db, shipment, _note_supplied_documents(db, shipment, payload)
                ),
            ),
        )


def request_document(session_id: str, doc_type: str) -> dict:
    """Record that the booking is waiting on a supporting document."""
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")

        existing = db.scalars(
            select(Document).where(
                Document.shipment_id == shipment.id,
                Document.doc_type == doc_type,
                Document.status.in_([DOC_PENDING, DOC_ACCEPTED]),
            )
        ).first()
        if existing is not None:
            return {
                "ok": True,
                "document_id": existing.id,
                "doc_type": doc_type,
                "status": existing.status,
                "note": "This document was already requested.",
            }

        document = Document(
            shipment_id=shipment.id, doc_type=doc_type, status=DOC_PENDING
        )
        db.add(document)
        db.flush()
        return {
            "ok": True,
            "document_id": document.id,
            "doc_type": doc_type,
            "status": DOC_PENDING,
            "note": "Booking is paused until this document is provided.",
        }


def submit_document(
    session_id: str,
    doc_type: str,
    filename: str,
    stored_path: str | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
) -> dict:
    """Record a supporting document supplied by the user.

    Review is simulated in this build, as the brief permits: the file is stored
    and marked accepted on receipt, and its contents are never read. Because
    nothing is extracted, nothing can be wrongly asserted about the document --
    the agent must describe it as received, not as verified.
    """
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")

        document = db.scalars(
            select(Document).where(
                Document.shipment_id == shipment.id,
                Document.doc_type == doc_type,
                Document.status == DOC_PENDING,
            )
        ).first()
        if document is None:
            document = Document(
                shipment_id=shipment.id, doc_type=doc_type, status=DOC_PENDING
            )
            db.add(document)
            db.flush()

        document.filename = filename
        document.stored_path = stored_path
        document.file_metadata_json = {
            "size_bytes": size_bytes,
            "content_type": content_type,
            "contents_inspected": False,
        }
        document.uploaded_at = _utcnow()
        document.status = DOC_ACCEPTED

        return {
            "ok": True,
            "document_id": document.id,
            "doc_type": doc_type,
            "status": DOC_ACCEPTED,
            "filename": filename,
            "note": (
                f"'{filename}' received and recorded against the {doc_type} "
                "requirement, and the booking is no longer blocked by it. "
                "Document review is simulated in this build -- the file contents "
                "were not inspected, so do not tell the user the document has "
                "been verified or describe what it contains."
            ),
        }


def acknowledge_insurance(session_id: str) -> dict:
    """Record the user's explicit acknowledgement of the high-value warning."""
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft for this conversation yet.")

        value = float(shipment.declared_value or 0)
        if value <= HIGH_VALUE_THRESHOLD_INR:
            return {
                "ok": True,
                "insurance_acknowledged": shipment.insurance_ack,
                "note": (
                    f"The declared value is not above Rs {HIGH_VALUE_THRESHOLD_INR:,}, "
                    "so no acknowledgement is required."
                ),
            }

        shipment.insurance_ack = True
        return {
            "ok": True,
            "insurance_acknowledged": True,
            "declared_value": value,
        }


def calculate_distance(pin_a: str, pin_b: str) -> dict:
    """Straight-line distance between two PIN codes.

    Each PIN is geocoded via OpenStreetMap, then the distance is computed
    locally. If either PIN cannot be geocoded the answer is `unknown` -- it is
    never estimated, and it never blocks a booking.
    """
    places = {}
    for label, pin in (("origin", pin_a), ("destination", pin_b)):
        # The India Post lookup gives a district/state to fall back on when the
        # PIN itself is not tagged in OpenStreetMap.
        postal = pin_api.lookup_pin(pin)
        fallback = None
        if postal.get("status") == pin_api.STATUS_OK:
            fallback = f"{postal.get('district')}, {postal.get('state')}"
        places[label] = geocode.geocode_pin(pin, fallback_place=fallback)

    unresolved = [
        label for label, place in places.items() if place.get("status") != "ok"
    ]
    if unresolved:
        return {
            "ok": True,
            "status": "unknown",
            "distance_km": None,
            "message": (
                "The distance could not be determined because coordinates for the "
                + " and ".join(unresolved)
                + " PIN code are unavailable."
            ),
            "detail": {label: places[label].get("status") for label in places},
        }

    distance = geocode.haversine_km(
        places["origin"]["lat"], places["origin"]["lon"],
        places["destination"]["lat"], places["destination"]["lon"],
    )
    return {
        "ok": True,
        "status": "ok",
        "distance_km": round(distance, 1),
        "method": "straight-line (haversine) between geocoded PIN centres",
        "origin": places["origin"].get("display_name"),
        "destination": places["destination"].get("display_name"),
    }


def estimate_price(
    weight_g: float, service_type: str, distance_km: float | None = None
) -> dict:
    """Estimate the shipping charge using this application's own formula.

    Always presented as an estimate produced by IndiaShipments, never as a
    carrier quote.
    """
    if service_type not in PRICE_TABLE:
        return _fail(
            f"'{service_type}' is not a service we offer. Choose "
            f"{' or '.join(SERVICE_TYPES)}."
        )
    try:
        weight_kg = float(weight_g) / 1000.0
    except (TypeError, ValueError):
        return _fail("Package weight is needed before a price can be estimated.")
    if weight_kg <= 0:
        return _fail("Package weight must be greater than zero.")

    rates = PRICE_TABLE[service_type]
    if distance_km is None:
        return {
            "ok": True,
            "status": "unavailable",
            "message": (
                "A price estimate needs the distance between the two PIN codes, "
                "which is not available right now."
            ),
        }

    weight_component = round(weight_kg * rates["per_kg_inr"], 2)
    distance_component = round(float(distance_km) * rates["per_km_inr"], 2)
    total = round(rates["base_inr"] + weight_component + distance_component, 2)

    return {
        "ok": True,
        "status": "ok",
        "estimated_price_inr": total,
        "service_type": service_type,
        "breakdown": {
            "base_inr": rates["base_inr"],
            "weight_inr": weight_component,
            "distance_inr": distance_component,
        },
        "disclaimer": (
            "This is an IndiaShipments estimate calculated by this application, "
            "not a carrier quote."
        ),
    }


def _next_reference(db, shipment_id: int) -> str:
    """Human-friendly reference. Not a carrier AWB and never presented as one."""
    number = 1000 + shipment_id
    while db.scalars(
        select(Shipment).where(Shipment.reference == f"{REFERENCE_PREFIX}{number}")
    ).first():
        number += 1
    return f"{REFERENCE_PREFIX}{number}"


def confirm_booking(session_id: str) -> dict:
    """Persist the draft as a booked shipment.

    This is the guardrail that matters. It refuses unless the draft has passed
    validation, every requested document has been accepted, and the high-value
    insurance warning has been acknowledged where it applies.
    """
    with session_scope() as db:
        shipment = _get_draft(db, session_id)
        if shipment is None:
            return _fail("There is no shipment draft to book for this conversation.")

        state = _get_state(db, session_id)
        if state.customer_id is None:
            return _fail(
                "A shipment can only be booked by a signed-in customer, and "
                "nobody is signed in on this conversation. Ask the user to sign "
                "in; their draft is saved and will be waiting.",
                blocked_by="not_signed_in",
            )

        if not shipment.validated:
            return _fail(
                "This shipment has not passed validation yet, so it cannot be "
                "booked. Run the validation check first and resolve anything it "
                "reports.",
                blocked_by="not_validated",
            )

        pending = _pending_documents(db, shipment.id)
        if pending:
            return _fail(
                "Booking is paused because a required document is still "
                "outstanding: " + ", ".join(d.doc_type for d in pending) + ".",
                blocked_by="pending_document",
                pending_documents=[d.doc_type for d in pending],
            )

        value = float(shipment.declared_value or 0)
        if value > HIGH_VALUE_THRESHOLD_INR and not shipment.insurance_ack:
            return _fail(
                f"The declared value is above Rs {HIGH_VALUE_THRESHOLD_INR:,}, so "
                "the user must explicitly acknowledge the insurance warning before "
                "this can be booked.",
                blocked_by="insurance_not_acknowledged",
            )

        shipment.reference = _next_reference(db, shipment.id)
        shipment.status = STATUS_BOOKED
        shipment.customer_id = state.customer_id
        db.add(
            TrackingEvent(
                shipment_id=shipment.id,
                status=STATUS_BOOKED,
                event_time=_utcnow(),
                location=(shipment.sender_json or {}).get("city"),
                note="Booking confirmed by the sender.",
            )
        )

        # The pointer stays on the shipment that was just booked rather than
        # being cleared. _get_draft only ever returns a row still in Draft
        # status, so this conversation has no draft either way -- but keeping
        # the link is what lets save_draft notice that the next detail typed
        # arrived after a booking, and say so.

        return {
            "ok": True,
            "reference": shipment.reference,
            "status": shipment.status,
            "note": (
                "Shipment booked and stored. This is an IndiaShipments tracking "
                "reference, not a carrier air waybill."
            ),
        }


def build_tracking_response(shipment: Shipment) -> dict:
    """Plain-language tracking view built only from stored events."""
    events = list(shipment.tracking_events)
    latest = events[-1] if events else None

    if latest is None:
        explanation = (
            f"{shipment.reference} is currently '{shipment.status}' and has no "
            "tracking events recorded yet."
        )
    else:
        where = f" at {latest.location}" if latest.location else ""
        explanation = (
            f"{shipment.reference} is currently '{shipment.status}'. The most "
            f"recent update was on {latest.event_time:%d %b %Y at %H:%M} UTC"
            f"{where}: {latest.note}"
        )

    return {
        "reference": shipment.reference,
        "status": shipment.status,
        "last_event_time": latest.event_time.isoformat() if latest else None,
        "last_location": latest.location if latest else None,
        "explanation": explanation,
        "events": [
            {
                "status": e.status,
                "event_time": e.event_time.isoformat(),
                "location": e.location,
                "note": e.note,
            }
            for e in events
        ],
    }


def get_tracking(reference: str) -> dict:
    """Return the stored tracking history for a shipment reference."""
    with session_scope() as db:
        shipment = db.scalars(
            select(Shipment).where(Shipment.reference == reference.strip().upper())
        ).one_or_none()
        if shipment is None:
            return _fail(
                f"No shipment with reference {reference} exists. Ask the user to "
                "check the reference.",
                reference=reference,
            )
        return {"ok": True, **build_tracking_response(shipment)}


def list_options(field: str) -> dict:
    """Selectable choices for a fixed-choice question, drawn from app data."""
    catalogue = {
        "service_type": SERVICE_TYPES,
        "contents_category": CONTENTS_CATEGORIES,
        "insurance": ["Yes, I acknowledge", "No, reduce the declared value"],
        "sender_address": ["Use my saved details", "A different sender"],
        "sender_previous": [
            "Use my last shipment's details", "A different sender",
        ],
    }
    if field not in catalogue:
        return _fail(
            f"'{field}' has no fixed option list. Known lists: "
            + ", ".join(catalogue)
        )
    return {"ok": True, "field": field, "options": catalogue[field]}
