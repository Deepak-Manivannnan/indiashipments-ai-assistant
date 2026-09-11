"""Deterministic business-rule engine.

Nothing here calls an LLM. The agent decides *when* to run these checks; this
module decides *what the answer is*. Every rule below is traceable to the
challenge brief -- no invented thresholds.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.constants import SERVICE_TYPES

# --- Limits, all stated in the brief ---
PIN_LENGTH = 6
MIN_DIMENSIONS_MM = (140, 90, 10)  # applied to the sorted L>=W>=H triple
MAX_TOTAL_DIMENSIONS_MM = 3000  # sum of the three sides
HIGH_VALUE_THRESHOLD_INR = 50_000

# --- Outer bounds, assumed rather than stated ---
# The brief fixes the rules above and requires weight, dimensions and declared
# value to be positive, but says nothing about an upper end. These bounds exist
# because "positive" alone accepts a 100-tonne parcel worth a trillion rupees,
# and because a number that absurd is almost always a slip -- 2 kg typed into a
# field that counts grams, or a value entered in paise. Catching it here turns a
# storage error into a sentence the customer can act on.
MIN_WEIGHT_G = 1  # below a gram is a unit mix-up, not a parcel
MAX_WEIGHT_G = 50_000  # 50 kg, the heaviest single parcel accepted
MAX_DECLARED_VALUE_INR = 10_000_000  # Rs 1 crore
PHONE_DIGITS = 10  # after stripping punctuation and any +91 or leading 0
MAX_TEXT_LENGTH = 200  # names, addresses and the contents description

# Offered to the user as selectable options; free text is still accepted.
CONTENTS_CATEGORIES = [
    "Documents",
    "Clothing",
    "Books",
    "Electronics",
    "Fragile goods",
    "Liquids",
    "Perishables / food",
    "Medicines",
    "Other",
]

# --- Prohibited contents (brief: "must be blocked") ---
BLOCKED_CONTENTS = [
    (
        ["currency", "cash", "coins", "banknote", "bank note", "rupee note"],
        "Currency, cash and coins cannot be sent through IndiaShipments.",
    ),
    (
        # "cracker" alone is deliberately absent: in Indian English it is as
        # likely to mean a snack as a firework.
        ["explosive", "firework", "firecracker", "fire cracker", "diwali cracker",
         "flammable", "petrol", "diesel", "kerosene", "compressed gas",
         "gas cylinder", "lighter fluid", "aerosol"],
        "Explosives, fireworks, flammable liquids and compressed gases cannot be carried.",
    ),
    (
        ["firearm", "gun", "pistol", "rifle", "ammunition", "bullet", "weapon"],
        "Firearms, ammunition and weapons cannot be carried.",
    ),
    (
        ["narcotic", "cocaine", "heroin", "cannabis", "ganja", "opium",
         "poison", "radioactive", "uranium"],
        "Narcotics, poisons and radioactive material cannot be carried.",
    ),
    (
        ["live animal", "live bird", "puppy", "kitten", "livestock"],
        "Live animals cannot be carried.",
    ),
    (
        ["counterfeit", "fake branded", "replica watch", "pirated", "duplicate brand"],
        "Counterfeit and otherwise prohibited goods cannot be carried.",
    ),
]

# --- Conditional contents (brief: "require a condition or warning") ---
LOOSE_BATTERY_TERMS = ["spare", "loose", "separate", "standalone", "power bank", "powerbank"]
INSTALLED_BATTERY_TERMS = ["installed", "inside", "built-in", "built in", "fitted", "in the device"]

CONDITIONAL_CONTENTS = [
    (
        ["liquid", "oil", "syrup", "shampoo", "paint", "bottle"],
        "Liquids must be in leak-proof packaging.",
        "liquids",
    ),
    (
        ["fragile", "glass", "ceramic", "crockery", "kitchenware", "mirror"],
        "Fragile goods must have suitable cushioning inside the box.",
        "fragile",
    ),
    (
        ["perishable", "food", "fruit", "vegetable", "sweets", "dairy", "frozen"],
        "Perishables need suitable packaging and a fast enough service.",
        "perishables",
    ),
    (
        ["medicine", "medicines", "tablet", "tablets", "prescription drug",
         "pharma", "pharmaceutical", "capsule", "capsules", "insulin"],
        "Medicines may require a prescription or supporting document before booking.",
        "medicines",
    ),
]


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern:
    """Whole-word matcher for one term, tolerating a plural 's'/'es'.

    Plain substring matching is not safe here: 'oil' appears inside 'foil' and
    'boiled', 'gun' inside 'gunny bag'. Matching on word boundaries stops the
    rule engine from blocking parcels it has no rule against.
    """
    return re.compile(rf"\b{re.escape(term)}(?:e?s)?\b", re.IGNORECASE)


def _mentions(text: str, terms: list[str]) -> bool:
    return any(_term_pattern(term).search(text) for term in terms)


@dataclass
class ContentsDecision:
    """Outcome of screening the contents description."""

    decision: str  # "allowed" | "blocked" | "conditional"
    reasons: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    requires_document: str | None = None  # e.g. "prescription"

    @property
    def is_blocked(self) -> bool:
        return self.decision == "blocked"


def classify_contents(contents: str | None) -> ContentsDecision:
    """Screen a free-text contents description against the challenge rules."""
    if not contents or not contents.strip():
        return ContentsDecision(decision="allowed")

    text = contents.lower()

    for terms, reason in BLOCKED_CONTENTS:
        if _mentions(text, terms):
            return ContentsDecision(decision="blocked", reasons=[reason])

    # Lithium batteries are the one item whose verdict depends on how it is
    # packed, so it is screened separately rather than by a flat keyword list.
    # "Power bank" names the item without using the word battery, so it has to
    # bring us into this branch on its own or it slips through as allowed.
    if _mentions(text, ["lithium", "li-ion", "battery", "batteries",
                        "power bank", "powerbank"]):
        if _mentions(text, LOOSE_BATTERY_TERMS):
            return ContentsDecision(
                decision="blocked",
                reasons=[
                    "Loose or spare lithium batteries (including power banks) cannot "
                    "be carried. Batteries are allowed only when installed in the "
                    "equipment they power."
                ],
            )
        if _mentions(text, INSTALLED_BATTERY_TERMS):
            return ContentsDecision(
                decision="conditional",
                reasons=[
                    "Lithium batteries are accepted only while installed in the "
                    "equipment they power. Please keep the device switched off and "
                    "protected against accidental activation."
                ],
                tags=["lithium_installed"],
            )
        # Ambiguous: neither loose nor confirmed installed. Do not guess.
        return ContentsDecision(
            decision="conditional",
            reasons=[
                "Lithium batteries are allowed only when installed in the equipment "
                "they power; loose or spare batteries must be blocked. Please confirm "
                "whether the battery is installed inside the device."
            ],
            tags=["lithium_unclear"],
        )

    reasons: list[str] = []
    tags: list[str] = []
    requires_document = None
    for terms, reason, tag in CONDITIONAL_CONTENTS:
        if _mentions(text, terms):
            reasons.append(reason)
            tags.append(tag)
            if tag == "medicines":
                requires_document = "prescription"

    if reasons:
        return ContentsDecision(
            decision="conditional",
            reasons=reasons,
            tags=tags,
            requires_document=requires_document,
        )

    return ContentsDecision(decision="allowed")


# ---------------------------------------------------------------------------
# Draft validation
# ---------------------------------------------------------------------------

PERSON_REQUIRED_FIELDS = ["name", "phone", "address", "city", "state", "pin"]
PACKAGE_REQUIRED_FIELDS = ["weight_g", "length_mm", "width_mm", "height_mm"]

FIELD_LABELS = {
    "sender.name": "sender's name",
    "sender.phone": "sender's phone number",
    "sender.address": "sender's street address",
    "sender.city": "sender's city",
    "sender.state": "sender's state",
    "sender.pin": "sender's 6-digit PIN code",
    "recipient.name": "recipient's name",
    "recipient.phone": "recipient's phone number",
    "recipient.address": "recipient's street address",
    "recipient.city": "recipient's city",
    "recipient.state": "recipient's state",
    "recipient.pin": "recipient's 6-digit PIN code",
    "package.weight_g": "package weight",
    "package.length_mm": "package length",
    "package.width_mm": "package width",
    "package.height_mm": "package height",
    "package.dimensions": "package size -- length, width and height together",
    "service_type": "service type (Standard or Express)",
    "contents": "what the parcel contains",
    "declared_value": "value of the contents in rupees (what they are worth, not the postage) -- it sets the compensation limit if the parcel is lost or damaged",
}


# The order in which missing details are asked for. Contents comes first: it is
# the only field that can rule the whole shipment out, and asking eighteen
# questions before discovering the parcel cannot be sent wastes the user's time.
ASK_ORDER = [
    "contents",
    "package.weight_g",
    "package.length_mm",
    "package.width_mm",
    "package.height_mm",
    "declared_value",
    "service_type",
    "sender.name",
    "sender.phone",
    "sender.address",
    "sender.city",
    "sender.pin",
    "sender.state",
    "recipient.name",
    "recipient.phone",
    "recipient.address",
    "recipient.city",
    "recipient.pin",
    "recipient.state",
]

# Missing fields that have a fixed list of choices to offer.
FIELD_OPTION_LISTS = {
    "contents": "contents_category",
    "service_type": "service_type",
}


# Fields that are natural to give together. Asking for length, then width, then
# height as three separate turns is technically "one question at a time" and
# awful to sit through.
FIELD_GROUPS = {
    "package.length_mm": "package.dimensions",
    "package.width_mm": "package.dimensions",
    "package.height_mm": "package.dimensions",
}


def next_field_to_ask(missing: list[str]) -> str | None:
    """The single next thing to ask about, in a sensible order."""
    ranked = sorted(
        missing, key=lambda f: ASK_ORDER.index(f) if f in ASK_ORDER else len(ASK_ORDER)
    )
    if not ranked:
        return None
    return FIELD_GROUPS.get(ranked[0], ranked[0])


@dataclass
class ValidationResult:
    """Structured verdict the agent turns into plain language.

    `ok` means the data itself is valid. It deliberately does NOT mean the
    shipment may be booked -- an outstanding document or insurance
    acknowledgement is enforced separately by confirm_booking.
    """

    ok: bool = False
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_document: str | None = None
    requires_insurance_ack: bool = False
    contents_decision: str = "allowed"

    def as_dict(self) -> dict:
        next_field = next_field_to_ask(self.missing)
        return {
            "ok": self.ok,
            "missing": self.missing,
            "missing_readable": [FIELD_LABELS.get(m, m) for m in self.missing],
            # Deliberately singular. The agent is told what to ask for next, one
            # item at a time, rather than being handed the whole list to recite.
            "ask_next": FIELD_LABELS.get(next_field, next_field) if next_field else None,
            "ask_next_field": next_field,
            "ask_next_options": FIELD_OPTION_LISTS.get(next_field),
            "ask_next_instruction": (
                "Ask the user for this one item only. Do not ask for anything "
                "else in the same message, and do not list what is still "
                "outstanding."
                + (
                    " If they reply with a bare number and no unit, ask whether "
                    "they mean kilograms or grams before saving it -- do not "
                    "assume, a parcel of 3 is not the same as 3 kg."
                    if next_field == "package.weight_g"
                    else ""
                )
                + (
                    " Give the measurements in centimetres unless they say "
                    "otherwise, and ask which unit they meant if it is unclear."
                    if next_field == "package.dimensions"
                    else ""
                )
                + (
                    " This field has a fixed list of choices: call list_options "
                    f"with field='{FIELD_OPTION_LISTS[next_field]}' and offer them."
                    if next_field in FIELD_OPTION_LISTS
                    else ""
                )
                if next_field
                else None
            ),
            "errors": self.errors,
            "warnings": self.warnings,
            "requires_document": self.requires_document,
            "requires_insurance_ack": self.requires_insurance_ack,
            "contents_decision": self.contents_decision,
        }


def is_valid_pin(pin) -> bool:
    return bool(pin) and str(pin).isdigit() and len(str(pin)) == PIN_LENGTH


def normalise_service(service) -> str | None:
    """The service type as this application spells it, or None if unrecognised.

    A customer who types "express" has chosen Express. Matching the capital
    letter is the application's job, not theirs.
    """
    wanted = str(service or "").strip().lower()
    for known in SERVICE_TYPES:
        if known.lower() == wanted:
            return known
    return None


def _positive(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def phone_digits(phone) -> str:
    """The dialable digits in a phone number, without country code or trunk 0.

    Accepts the shapes people actually type: "+91 98470 12345", "098470-12345",
    "(0484) 234 5678".
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == PHONE_DIGITS + 2 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == PHONE_DIGITS + 1 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def is_valid_phone(phone) -> bool:
    """An Indian number has ten digits once the prefixes are removed.

    Deliberately not stricter than that. Requiring a 6-9 first digit would be
    right for mobiles and wrong for the landlines behind area codes like 0484,
    and a pickup address with a landline on it is a real shipment.
    """
    return len(phone_digits(phone)) == PHONE_DIGITS


def _check_person(person: dict | None, role: str, result: ValidationResult) -> None:
    person = person or {}
    for fname in PERSON_REQUIRED_FIELDS:
        if not str(person.get(fname) or "").strip():
            result.missing.append(f"{role}.{fname}")

    pin = person.get("pin")
    if pin and not is_valid_pin(pin):
        result.errors.append(
            f"The {role} PIN code '{pin}' is not valid -- an Indian PIN code has "
            "exactly six digits."
        )

    phone = person.get("phone")
    if str(phone or "").strip() and not is_valid_phone(phone):
        result.errors.append(
            f"The {role} phone number '{phone}' is not valid -- an Indian phone "
            f"number has {PHONE_DIGITS} digits, with or without a +91 prefix. "
            "The courier needs a working number to arrange the handover."
        )


def _check_package(package: dict | None, result: ValidationResult) -> None:
    package = package or {}
    for fname in PACKAGE_REQUIRED_FIELDS:
        if package.get(fname) in (None, ""):
            result.missing.append(f"package.{fname}")

    weight = package.get("weight_g")
    if weight is not None:
        if not _positive(weight):
            result.errors.append("Package weight must be greater than zero.")
        elif float(weight) < MIN_WEIGHT_G:
            # Almost always a unit slip: "0.5" meant kilograms.
            result.errors.append(
                f"A weight of {float(weight):g} g is below the minimum of "
                f"{MIN_WEIGHT_G} g. If that was meant in kilograms, "
                f"{float(weight):g} kg is {int(float(weight) * 1000)} g."
            )
        elif float(weight) > MAX_WEIGHT_G:
            result.errors.append(
                f"This parcel weighs {float(weight) / 1000:g} kg, above the "
                f"{MAX_WEIGHT_G // 1000} kg limit for a single shipment."
            )

    dims = [package.get(k) for k in ("length_mm", "width_mm", "height_mm")]
    if all(d is not None for d in dims):
        if not all(_positive(d) for d in dims):
            result.errors.append(
                "Length, width and height must each be greater than zero."
            )
        else:
            dims_f = sorted((float(d) for d in dims), reverse=True)
            # Assumption: the minimum applies to the sorted sides, so a box may be
            # oriented any way round provided its largest side is >= 140mm, its
            # middle side >= 90mm and its smallest >= 10mm.
            if any(d < m for d, m in zip(dims_f, MIN_DIMENSIONS_MM)):
                result.errors.append(
                    "The package is smaller than the minimum accepted size of "
                    f"{MIN_DIMENSIONS_MM[0]} x {MIN_DIMENSIONS_MM[1]} x "
                    f"{MIN_DIMENSIONS_MM[2]} mm."
                )
            if sum(dims_f) > MAX_TOTAL_DIMENSIONS_MM:
                result.errors.append(
                    "Length, width and height together must not exceed "
                    f"{MAX_TOTAL_DIMENSIONS_MM} mm (this parcel totals "
                    f"{int(sum(dims_f))} mm)."
                )


def validate_draft(draft: dict) -> ValidationResult:
    """Run every deterministic rule against a draft shipment.

    `draft` keys: sender, recipient, package, service_type, contents,
    declared_value, plus optional pin_checks from the serviceability lookup.
    """
    result = ValidationResult()

    _check_person(draft.get("sender"), "sender", result)
    _check_person(draft.get("recipient"), "recipient", result)
    _check_package(draft.get("package"), result)

    service = draft.get("service_type")
    if not service:
        result.missing.append("service_type")
    elif normalise_service(service) is not None:
        pass  # "express" is Express; the casing is not the customer's problem
    elif service not in SERVICE_TYPES:
        result.errors.append(
            f"'{service}' is not a service we offer. Choose "
            f"{' or '.join(SERVICE_TYPES)}."
        )

    contents = draft.get("contents")
    if not contents:
        result.missing.append("contents")
    else:
        decision = classify_contents(contents)
        result.contents_decision = decision.decision
        if decision.is_blocked:
            result.errors.extend(decision.reasons)
        else:
            result.warnings.extend(decision.reasons)
            if decision.requires_document:
                result.requires_document = decision.requires_document

    value = draft.get("declared_value")
    if value in (None, ""):
        result.missing.append("declared_value")
    elif not _positive(value):
        result.errors.append("Declared value must be greater than zero.")
    elif float(value) > MAX_DECLARED_VALUE_INR:
        result.errors.append(
            f"A declared value of Rs {float(value):,.0f} is above the "
            f"Rs {MAX_DECLARED_VALUE_INR:,} ceiling for a parcel. Anything "
            "worth more than that needs a specialist carrier, not this service."
        )
    elif float(value) > HIGH_VALUE_THRESHOLD_INR:
        result.requires_insurance_ack = True
        result.warnings.append(
            f"The declared value is above Rs {HIGH_VALUE_THRESHOLD_INR:,}. "
            "IndiaShipments liability is limited unless the parcel is insured -- "
            "you will need to acknowledge this before booking."
        )

    # Serviceability and city/PIN agreement, using results already fetched from
    # the PIN lookup. Never inferred locally.
    for role in ("sender", "recipient"):
        check = (draft.get("pin_checks") or {}).get(role)
        if not check:
            continue
        status = check.get("status")
        if status == "unavailable":
            result.warnings.append(
                f"The {role} PIN code could not be verified right now because the "
                "PIN lookup service is unavailable. It has not been confirmed as "
                "serviceable."
            )
        elif status == "not_serviceable":
            result.errors.append(
                f"PIN code {check.get('pin')} could not be found, so we cannot "
                f"confirm delivery to the {role} address."
            )
        elif status == "mismatch":
            # The brief requires a city/PIN disagreement to be resolved OR
            # explicitly accepted. Once the user has chosen, it stops being an
            # error and becomes a recorded note -- otherwise the conversation
            # cannot move past it, whatever the user answers.
            if (draft.get(role) or {}).get("city_pin_accepted"):
                result.warnings.append(
                    f"The {role} address says {check.get('given_city')} while PIN "
                    f"{check.get('pin')} is registered to {check.get('api_city')}. "
                    "The user has confirmed the address as written."
                )
            else:
                result.errors.append(
                    f"The {role} PIN code {check.get('pin')} belongs to "
                    f"{check.get('api_city')}, {check.get('api_state')}, but the "
                    f"address says {check.get('given_city')}. Ask which is correct "
                    "and then call resolve_address_conflict -- do not simply ask "
                    "again."
                )

    result.ok = not result.missing and not result.errors
    return result
