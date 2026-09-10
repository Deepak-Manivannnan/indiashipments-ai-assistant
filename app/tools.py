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
    classify_contents,
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
    with session_scope() as db:
        state = _get_state(db, session_id)
        shipment = _get_draft(db, session_id)
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

        # Screening the contents here as well as in validate_shipment means the
        # verdict on a prohibited or restricted item always reaches the model as
        # a tool result. Otherwise a model that answers straight after saving
        # would be stating a rule from its own knowledge rather than from ours.
        if shipment.contents:
            decision = classify_contents(shipment.contents)
            payload["contents_check"] = {
                "decision": decision.decision,
                "reasons": decision.reasons,
                "requires_document": decision.requires_document,
                "note": (
                    "Use these reasons verbatim when explaining what can or "
                    "cannot be sent. Do not add rules of your own."
                ),
            }

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

    saved = save_draft(
        session_id=session_id,
        **{f"sender_{key}": value for key, value in sender.items() if value},
    )
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


def check_contents(description: str) -> dict:
    """Screen a description of the contents against the acceptance rules.

    Read-only and cheap, so the agent can consult it before saying anything
    about whether an item may be sent. Without this the model answers from its
    own knowledge of postal regulations, which is exactly what the brief
    forbids.
    """
    decision = classify_contents(description)
    return {
        "ok": True,
        "description": description,
        "decision": decision.decision,
        "reasons": decision.reasons,
        "requires_document": decision.requires_document,
        "note": (
            "These reasons are the only grounds you may give. Do not cite "
            "regulations, authorities or classifications that are not stated "
            "here."
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

        return {
            "ok": True,
            "draft": draft,
            "validated": shipment.validated,
            "insurance_acknowledged": shipment.insurance_ack,
            "pending_documents": [d.doc_type for d in pending],
            "still_missing": result.as_dict()["missing_readable"],
            "ask_next": result.as_dict()["ask_next"],
            "ask_next_options": result.as_dict()["ask_next_options"],
            "ask_next_instruction": result.as_dict()["ask_next_instruction"],
            "ready_to_book": (
                shipment.validated
                and not pending
                and (shipment.insurance_ack or not result.requires_insurance_ack)
            ),
        }


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
        draft["pin_checks"] = pin_checks

        result = validate_draft(draft)
        shipment.validated = result.ok

        # Requirements this rule engine raises by itself are withdrawn again if
        # the rule stops applying -- a user who says "medicines" and then
        # corrects it to "documents" must not stay blocked on a prescription
        # forever. Only un-uploaded, rule-driven requests are cleared; anything
        # requested explicitly or already supplied is left alone.
        stale = db.scalars(
            select(Document).where(
                Document.shipment_id == shipment.id,
                Document.status == DOC_PENDING,
                Document.uploaded_at.is_(None),
                Document.doc_type.in_(RULE_DRIVEN_DOC_TYPES),
                Document.doc_type != (result.requires_document or ""),
            )
        ).all()
        for document in stale:
            db.delete(document)

        # A required document is recorded now so that confirm_booking can block
        # on it even if the model forgets to ask.
        if result.requires_document:
            already = db.scalars(
                select(Document).where(
                    Document.shipment_id == shipment.id,
                    Document.doc_type == result.requires_document,
                    Document.status.in_([DOC_PENDING, DOC_ACCEPTED]),
                )
            ).first()
            if already is None:
                db.add(
                    Document(
                        shipment_id=shipment.id,
                        doc_type=result.requires_document,
                        status=DOC_PENDING,
                    )
                )

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
        return payload


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

        state.draft_shipment_id = None  # the draft is now a real shipment

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
    }
    if field not in catalogue:
        return _fail(
            f"'{field}' has no fixed option list. Known lists: "
            + ", ".join(catalogue)
        )
    return {"ok": True, "field": field, "options": catalogue[field]}
