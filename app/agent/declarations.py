"""Gemini function declarations for the tool layer.

`session_id` is deliberately absent from every schema. The loop injects it, so
the model cannot address a conversation other than the one it is in.
"""

from google.genai import types

from app import tools

STR = types.Type.STRING
NUM = types.Type.NUMBER


def _schema(properties: dict[str, tuple], required: list[str] | None = None):
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            name: types.Schema(type=kind, description=description)
            for name, (kind, description) in properties.items()
        },
        required=required or [],
    )


_PERSON_FIELDS = {
    "{role}_name": (STR, "{Role}'s full name."),
    "{role}_phone": (STR, "{Role}'s phone number."),
    "{role}_address": (STR, "{Role}'s street address, without city or PIN."),
    "{role}_city": (STR, "{Role}'s city."),
    "{role}_state": (STR, "{Role}'s state."),
    "{role}_pin": (STR, "{Role}'s six-digit Indian PIN code."),
}


def _person_properties(role: str) -> dict:
    return {
        key.format(role=role): (kind, text.format(Role=role.capitalize()))
        for key, (kind, text) in _PERSON_FIELDS.items()
    }


SAVE_DRAFT_PROPERTIES = {
    **_person_properties("sender"),
    **_person_properties("recipient"),
    "weight_g": (NUM, "Package weight in GRAMS. Convert from kg (2 kg = 2000)."),
    "length_mm": (NUM, "Package length in MILLIMETRES. Convert from cm (30 cm = 300)."),
    "width_mm": (NUM, "Package width in MILLIMETRES."),
    "height_mm": (NUM, "Package height in MILLIMETRES."),
    "service_type": (STR, "Either 'Standard' or 'Express'."),
    "contents": (STR, "What the parcel contains, in the user's own words."),
    "declared_value": (NUM, "What the contents are worth in rupees -- used for the insurance rule and to set the compensation limit if the parcel is lost or damaged. It is not the shipping charge."),
}


DECLARATIONS = [
    types.FunctionDeclaration(
        name="save_draft",
        description=(
            "Record shipment details the user has given. Merges with what is "
            "already known, so call it with just the new fields as soon as the "
            "user mentions them. Any change requires re-validation."
        ),
        parameters=_schema(SAVE_DRAFT_PROPERTIES),
    ),
    types.FunctionDeclaration(
        name="start_new_shipment",
        description=(
            "Clear the current draft and begin a fresh shipment. Call this the "
            "moment the user wants to send something else -- 'a new shipment', "
            "'another parcel', 'start over' -- so that nothing from the "
            "previous parcel carries over. A shipment already booked is never "
            "altered by this."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="check_contents",
        description=(
            "Screen what the user wants to send against the acceptance rules. "
            "Call this BEFORE saying anything about whether an item can or "
            "cannot be sent. Returns allowed, conditional or blocked, with the "
            "exact reasons you must use."
        ),
        parameters=_schema(
            {"description": (STR, "What the user said they want to send.")},
            required=["description"],
        ),
    ),
    types.FunctionDeclaration(
        name="prefill_sender_from_profile",
        description=(
            "Fill the sender details from the signed-in customer's saved "
            "profile. ONLY call this once the user has agreed to it. Ask them "
            "first whether the parcel is going from their own saved address or "
            "from somewhere else -- they may well be sending on behalf of "
            "someone, or from a different place today."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="prefill_sender_from_last_shipment",
        description=(
            "Reuse the sender details from this customer's previous booking, "
            "for customers who registered without an address. Only call it "
            "once they have agreed. It fills blanks only."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="list_my_shipments",
        description=(
            "List the signed-in customer's existing shipments and their "
            "statuses. Use it when they ask about their orders, or to offer "
            "them something to track."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="get_summary",
        description=(
            "Read back the current draft, what is still missing, any blockers, "
            "and whether it is ready to book."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="list_options",
        description=(
            "Get the selectable choices for a fixed-choice question so they can "
            "be offered to the user. Call this before asking about service type, "
            "contents category, or insurance."
        ),
        parameters=_schema(
            {"field": (STR, "One of: service_type, contents_category, insurance.")},
            required=["field"],
        ),
    ),
    types.FunctionDeclaration(
        name="check_pin_serviceability",
        description=(
            "Check an Indian PIN code with India Post: whether it exists, and "
            "its real city and state. Optionally compare it with the city the "
            "user stated, which surfaces a mismatch."
        ),
        parameters=_schema(
            {
                "pin": (STR, "Six-digit Indian PIN code."),
                "city": (STR, "The city the user said, to check it agrees."),
            },
            required=["pin"],
        ),
    ),
    types.FunctionDeclaration(
        name="resolve_address_conflict",
        description=(
            "Settle a disagreement between a stated city and its PIN code, "
            "once the user has said which is right. keep='pin' rewrites the "
            "city to the one India Post holds; keep='city' records that the "
            "address is correct as written. Call this as soon as they answer -- "
            "asking again without calling it leaves the booking stuck."
        ),
        parameters=_schema(
            {
                "role": (STR, "'sender' or 'recipient'."),
                "keep": (STR, "'pin' to use the PIN's city, 'city' to keep the "
                              "address as the user wrote it."),
            },
            required=["role", "keep"],
        ),
    ),
    types.FunctionDeclaration(
        name="validate_shipment",
        description=(
            "Run every business rule against the draft: completeness, PIN codes, "
            "dimensions, weight, declared value, prohibited and restricted "
            "contents, and serviceability. Returns what is missing, what is "
            "wrong, and any warnings. Call before showing a summary."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="request_document",
        description="Record that a supporting document is required before booking.",
        parameters=_schema(
            {"doc_type": (STR, "For example 'prescription' or 'invoice'.")},
            required=["doc_type"],
        ),
    ),
    types.FunctionDeclaration(
        name="acknowledge_insurance",
        description=(
            "Record that the user has explicitly accepted the high-value "
            "insurance warning. Only call this once they have actually agreed."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="calculate_distance",
        description=(
            "Straight-line distance between two PIN codes. Returns 'unknown' if "
            "either cannot be located; it is never estimated."
        ),
        parameters=_schema(
            {
                "pin_a": (STR, "Origin PIN code."),
                "pin_b": (STR, "Destination PIN code."),
            },
            required=["pin_a", "pin_b"],
        ),
    ),
    types.FunctionDeclaration(
        name="estimate_price",
        description=(
            "Estimate the shipping charge. This is a Rapid India estimate "
            "from this application, not a carrier quote, and must be described "
            "that way to the user."
        ),
        parameters=_schema(
            {
                "weight_g": (NUM, "Package weight in grams."),
                "service_type": (STR, "'Standard' or 'Express'."),
                "distance_km": (NUM, "Distance from calculate_distance."),
            },
            required=["weight_g", "service_type"],
        ),
    ),
    types.FunctionDeclaration(
        name="confirm_booking",
        description=(
            "Create the shipment and return its tracking reference. Only call "
            "this after the user has seen the full summary and explicitly agreed "
            "to book. It refuses if validation has not passed, a document is "
            "outstanding, or the insurance warning is unacknowledged."
        ),
        parameters=_schema({}),
    ),
    types.FunctionDeclaration(
        name="get_tracking",
        description=(
            "Look up a shipment by its reference and return its real stored "
            "tracking events. Describe only what this returns."
        ),
        parameters=_schema(
            {"reference": (STR, "Shipment reference, for example RI-1042.")},
            required=["reference"],
        ),
    ),
]

# Tools that operate on the current conversation. The loop supplies session_id
# for these; the model never sees or sets it.
SESSION_SCOPED = {
    "save_draft",
    "check_contents",
    "start_new_shipment",
    "get_summary",
    "validate_shipment",
    "request_document",
    "acknowledge_insurance",
    "confirm_booking",
    "prefill_sender_from_profile",
    "prefill_sender_from_last_shipment",
    "list_my_shipments",
    "resolve_address_conflict",
}

# submit_document is intentionally absent: files arrive through the upload
# endpoint, not through the model.
CALLABLE_TOOLS = {
    "save_draft": tools.save_draft,
    "get_summary": tools.get_summary,
    "list_options": tools.list_options,
    "check_contents": tools.check_contents,
    "start_new_shipment": tools.start_new_shipment,
    "resolve_address_conflict": tools.resolve_address_conflict,
    "prefill_sender_from_profile": tools.prefill_sender_from_profile,
    "prefill_sender_from_last_shipment": tools.prefill_sender_from_last_shipment,
    "list_my_shipments": tools.list_my_shipments,
    "check_pin_serviceability": tools.check_pin_serviceability,
    "validate_shipment": tools.validate_shipment,
    "request_document": tools.request_document,
    "acknowledge_insurance": tools.acknowledge_insurance,
    "calculate_distance": tools.calculate_distance,
    "estimate_price": tools.estimate_price,
    "confirm_booking": tools.confirm_booking,
    "get_tracking": tools.get_tracking,
}
