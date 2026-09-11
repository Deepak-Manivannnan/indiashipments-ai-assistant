"""Guardrail tests for the tool layer -- no LLM involved.

The point of these tests is not the happy path. It is that a caller (later, a
model) who calls tools in the wrong order gets a clear refusal instead of a
silent success.
"""

import uuid

import pytest
from sqlalchemy import select

from app import auth, tools
from app.constants import STATUS_BOOKED, STATUS_DRAFT
from app.db import SessionLocal
from app.models import ConversationState, Document, Shipment
from app.rules import classify_contents

VALID_SENDER = {
    "sender_name": "Rahul Menon",
    "sender_phone": "9847012345",
    "sender_address": "12 Marine Drive",
    "sender_city": "Kochi",
    "sender_state": "Kerala",
    "sender_pin": "682031",
}
VALID_RECIPIENT = {
    "recipient_name": "Anil Kumar",
    "recipient_phone": "9880123456",
    "recipient_address": "44 MG Road",
    "recipient_city": "Bengaluru",
    "recipient_state": "Karnataka",
    "recipient_pin": "560001",
}
VALID_PACKAGE = {
    "weight_g": 2000,
    "length_mm": 300,
    "width_mm": 200,
    "height_mm": 150,
    "service_type": "Standard",
    "contents": "Documents",
    "declared_value": 1500,
}


SEEDED_REFERENCES = {"RI-1001", "RI-1042", "RI-1077"}


DEMO_EMAIL = "rahul@example.com"
DEMO_PASSWORD = "demo1234"


def _conversation(signed_in: bool):
    """A fresh conversation, with everything it created removed afterwards.

    Shipment ids are recorded as the test runs rather than matched by content,
    so cleanup can never reach a shipment this test did not create.
    """
    sid = f"test-{uuid.uuid4().hex[:12]}"
    created: set[int] = set()

    if signed_in:
        account = auth.authenticate(DEMO_EMAIL, DEMO_PASSWORD)
        assert account["ok"], "seed the database before running the tests"
        tools.bind_session(sid, account["customer"]["id"])

    def remember():
        db = SessionLocal()
        try:
            state = db.get(ConversationState, sid)
            if state and state.draft_shipment_id:
                created.add(state.draft_shipment_id)
        finally:
            db.close()

    tools_save_draft = tools.save_draft

    def tracking_save_draft(*args, **kwargs):
        result = tools_save_draft(*args, **kwargs)
        remember()
        return result

    tools.save_draft = tracking_save_draft
    try:
        yield sid
    finally:
        tools.save_draft = tools_save_draft

    db = SessionLocal()
    try:
        state = db.get(ConversationState, sid)
        if state:
            if state.draft_shipment_id:
                created.add(state.draft_shipment_id)
            db.delete(state)
        for shipment_id in created:
            shipment = db.get(Shipment, shipment_id)
            if shipment is not None:
                assert shipment.reference not in SEEDED_REFERENCES
                db.delete(shipment)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def session_id():
    """A conversation with a signed-in customer -- the normal case."""
    yield from _conversation(signed_in=True)


@pytest.fixture
def anonymous_session():
    """A conversation with nobody signed in."""
    yield from _conversation(signed_in=False)


def fill_valid_draft(session_id, **overrides):
    fields = {**VALID_SENDER, **VALID_RECIPIENT, **VALID_PACKAGE, **overrides}
    return tools.save_draft(session_id=session_id, **fields)


# ---------------------------------------------------------------------------
# Out-of-order calls must be refused
# ---------------------------------------------------------------------------

def test_confirm_booking_without_any_draft_is_refused(session_id):
    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert "no shipment draft" in result["error"].lower()


def test_confirm_booking_before_validation_is_refused(session_id):
    fill_valid_draft(session_id)
    result = tools.confirm_booking(session_id)

    assert result["ok"] is False
    assert result["blocked_by"] == "not_validated"


def test_editing_the_draft_after_validation_invalidates_it(session_id):
    fill_valid_draft(session_id)
    assert tools.validate_shipment(session_id)["validated"] is True

    # A quiet change after a successful validation must not remain bookable.
    tools.save_draft(session_id=session_id, declared_value=999999)

    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert result["blocked_by"] == "not_validated"


def test_pending_document_blocks_booking(session_id):
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    tools.request_document(session_id, doc_type="invoice")

    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert result["blocked_by"] == "pending_document"
    assert "invoice" in result["pending_documents"]


def test_supplying_the_requested_document_unblocks_booking(session_id):
    """Review is simulated: a received file satisfies the requirement."""
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    tools.request_document(session_id, doc_type="invoice")
    assert tools.confirm_booking(session_id)["blocked_by"] == "pending_document"

    upload = tools.submit_document(
        session_id, doc_type="invoice", filename="invoice.pdf",
        size_bytes=1024, content_type="application/pdf",
    )
    assert upload["status"] == "accepted"
    assert tools.confirm_booking(session_id)["ok"] is True


def test_document_contents_are_never_inspected(session_id):
    """Nothing is read from the file, so nothing can be asserted about it."""
    fill_valid_draft(session_id)
    tools.request_document(session_id, doc_type="prescription")
    result = tools.submit_document(
        session_id, doc_type="prescription", filename="scan.pdf",
        size_bytes=2048, content_type="application/pdf",
    )

    # The tool result must tell the model, in so many words, not to claim the
    # document was checked.
    assert "not inspected" in result["note"]
    assert "do not tell the user the document has been verified" in result["note"]

    db = SessionLocal()
    try:
        document = db.scalars(
            select(Document).where(Document.filename == "scan.pdf")
        ).one()
        assert document.file_metadata_json["contents_inspected"] is False
    finally:
        db.close()


def test_the_medicines_journey_end_to_end(session_id):
    """Select medicines -> prescription requested -> file supplied -> booked."""
    fill_valid_draft(session_id, contents="Medicines")
    validation = tools.validate_shipment(session_id)
    assert validation["requires_document"] == "prescription"
    assert tools.confirm_booking(session_id)["blocked_by"] == "pending_document"

    tools.submit_document(
        session_id, doc_type="prescription", filename="prescription.pdf",
        size_bytes=4096, content_type="application/pdf",
    )
    booking = tools.confirm_booking(session_id)
    assert booking["ok"] is True
    assert booking["reference"].startswith("RI-")


def test_high_value_shipment_needs_explicit_insurance_acknowledgement(session_id):
    fill_valid_draft(session_id, declared_value=75000)
    validation = tools.validate_shipment(session_id)
    assert validation["requires_insurance_ack"] is True

    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert result["blocked_by"] == "insurance_not_acknowledged"

    tools.acknowledge_insurance(session_id)
    assert tools.confirm_booking(session_id)["ok"] is True


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------

def test_blocked_contents_fail_validation_and_cannot_be_booked(session_id):
    fill_valid_draft(session_id, contents="a spare lithium battery")
    validation = tools.validate_shipment(session_id)

    assert validation["validated"] is False
    assert any("lithium" in e.lower() for e in validation["errors"])
    assert tools.confirm_booking(session_id)["ok"] is False


def test_medicines_automatically_raise_a_document_requirement(session_id):
    fill_valid_draft(session_id, contents="prescription medicines and tablets")
    validation = tools.validate_shipment(session_id)

    assert validation["requires_document"] == "prescription"
    # The requirement is recorded even though nobody called request_document.
    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert result["blocked_by"] == "pending_document"


def test_undersized_package_is_rejected_with_a_reason(session_id):
    fill_valid_draft(session_id, length_mm=100, width_mm=50, height_mm=5)
    validation = tools.validate_shipment(session_id)

    assert validation["validated"] is False
    assert any("minimum accepted size" in e for e in validation["errors"])


def test_incomplete_draft_reports_what_is_missing(session_id):
    tools.save_draft(session_id=session_id, recipient_city="Chennai")
    validation = tools.validate_shipment(session_id)

    assert validation["validated"] is False
    assert "sender's name" in validation["missing_readable"]
    assert validation["errors"] == []  # missing is not the same as wrong


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_full_booking_journey_persists_a_real_shipment(session_id):
    fill_valid_draft(session_id)
    assert tools.validate_shipment(session_id)["validated"] is True

    summary = tools.get_summary(session_id)
    assert summary["ready_to_book"] is True

    booking = tools.confirm_booking(session_id)
    assert booking["ok"] is True
    reference = booking["reference"]
    assert reference.startswith("RI-")

    # It is in the database, not just in the response.
    db = SessionLocal()
    try:
        stored = db.scalars(
            select(Shipment).where(Shipment.reference == reference)
        ).one()
        assert stored.status == STATUS_BOOKED
        assert len(stored.tracking_events) == 1
        assert stored.tracking_events[0].status == STATUS_BOOKED
    finally:
        db.close()

    tracking = tools.get_tracking(reference)
    assert tracking["ok"] is True
    assert tracking["status"] == STATUS_BOOKED


def test_booking_twice_is_refused(session_id):
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    assert tools.confirm_booking(session_id)["ok"] is True

    # The draft is gone; a second confirmation has nothing to book.
    assert tools.confirm_booking(session_id)["ok"] is False


def test_save_draft_merges_across_turns_and_never_erases(session_id):
    tools.save_draft(session_id=session_id, sender_city="Kochi")
    tools.save_draft(session_id=session_id, recipient_city="Bengaluru")
    result = tools.save_draft(session_id=session_id, weight_g=2000)

    assert result["draft"]["sender"]["city"] == "Kochi"
    assert result["draft"]["recipient"]["city"] == "Bengaluru"
    assert result["draft"]["package"]["weight_g"] == 2000


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------

def test_tracking_an_unknown_reference_fails_clearly():
    result = tools.get_tracking("RI-9999")
    assert result["ok"] is False
    assert "RI-9999" in result["error"]


def test_tracking_a_seeded_shipment_returns_its_real_events():
    result = tools.get_tracking("RI-1077")
    assert result["ok"] is True
    assert result["status"] == "Delivery failed"
    assert len(result["events"]) == 7
    assert "Delivery failed" in result["explanation"] or "Delivery failed" == result["status"]


# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------

def test_pin_lookup_identifies_a_real_and_an_unknown_pin():
    good = tools.check_pin_serviceability("560001", city="Bengaluru")
    assert good["serviceable"] is True
    assert good["status"] == "ok"

    bad = tools.check_pin_serviceability("999999")
    assert bad["serviceable"] is False
    assert bad["status"] == "not_serviceable"


def test_city_and_pin_disagreement_is_surfaced():
    result = tools.check_pin_serviceability("560001", city="Chennai")
    assert result["status"] == "mismatch"
    assert result["api_city"]


def test_distance_and_price_are_computed_from_real_coordinates():
    distance = tools.calculate_distance("682031", "560001")
    assert distance["status"] == "ok"
    assert 300 < distance["distance_km"] < 420  # Kochi -> Bengaluru straight line

    price = tools.estimate_price(
        weight_g=2000, service_type="Standard", distance_km=distance["distance_km"]
    )
    assert price["status"] == "ok"
    assert price["estimated_price_inr"] > 0
    assert "not a carrier quote" in price["disclaimer"]


def test_price_is_unavailable_rather_than_invented_when_distance_is_unknown():
    price = tools.estimate_price(weight_g=2000, service_type="Standard", distance_km=None)
    assert price["status"] == "unavailable"
    assert "estimated_price_inr" not in price


# ---------------------------------------------------------------------------
# Regressions -- each of these was a real defect found by probing Phase 2
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "contents,expected",
    [
        # Substring matching used to block these: 'cracker' in 'crackers',
        # 'gun' in 'gunny', 'oil' in 'foil'/'boiled'/'coil'.
        ("crackers and namkeen snacks", "allowed"),
        ("gunny bags", "allowed"),
        ("aluminium foil rolls", "allowed"),
        ("coil of copper wire", "allowed"),
        # ...while the rules they were shadowing must still fire.
        ("diwali firecrackers", "blocked"),
        ("a licensed gun", "blocked"),
        ("cooking oil", "conditional"),
        ("cash", "blocked"),
    ],
)
def test_contents_screening_matches_whole_words_only(contents, expected):
    assert classify_contents(contents).decision == expected


def test_document_requirement_is_withdrawn_when_the_rule_stops_applying(session_id):
    """A user who mis-states the contents must not be blocked forever."""
    fill_valid_draft(session_id, contents="medicines and tablets")
    assert tools.validate_shipment(session_id)["requires_document"] == "prescription"

    fill_valid_draft(session_id, contents="Documents")
    validation = tools.validate_shipment(session_id)
    assert validation["requires_document"] is None

    result = tools.confirm_booking(session_id)
    assert result["ok"] is True, result


def test_an_explicitly_requested_document_is_never_withdrawn(session_id):
    """Only rule-driven requirements are automatic; an invoice request stands."""
    fill_valid_draft(session_id, contents="Documents")
    tools.request_document(session_id, doc_type="invoice")
    tools.validate_shipment(session_id)

    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert "invoice" in result["pending_documents"]


def test_changing_the_declared_value_revokes_the_insurance_acknowledgement(session_id):
    """An acknowledgement covers the amount the user was actually shown."""
    fill_valid_draft(session_id, declared_value=60000)
    tools.validate_shipment(session_id)
    tools.acknowledge_insurance(session_id)

    fill_valid_draft(session_id, declared_value=500000)
    tools.validate_shipment(session_id)
    assert tools.get_summary(session_id)["insurance_acknowledged"] is False

    result = tools.confirm_booking(session_id)
    assert result["ok"] is False
    assert result["blocked_by"] == "insurance_not_acknowledged"


def test_an_unchanged_declared_value_keeps_the_acknowledgement(session_id):
    """Re-saving the same value must not force the user to acknowledge twice."""
    fill_valid_draft(session_id, declared_value=60000)
    tools.validate_shipment(session_id)
    tools.acknowledge_insurance(session_id)

    fill_valid_draft(session_id, declared_value=60000)  # unchanged
    tools.validate_shipment(session_id)
    assert tools.confirm_booking(session_id)["ok"] is True


# ---------------------------------------------------------------------------
# Accounts and ownership
# ---------------------------------------------------------------------------

def test_passwords_are_salted_and_never_stored_in_the_clear():
    first = auth.hash_password("demo1234")
    second = auth.hash_password("demo1234")

    assert "demo1234" not in first
    assert first != second  # a fresh salt each time
    assert auth.verify_password("demo1234", first)
    assert not auth.verify_password("demo1233", first)
    assert not auth.verify_password("", first)


def test_sign_in_rejects_a_wrong_password_without_revealing_which_part_failed():
    unknown = auth.authenticate("nobody@example.com", "demo1234")
    wrong = auth.authenticate("rahul@example.com", "not-the-password")

    assert unknown["ok"] is False
    assert wrong["ok"] is False
    assert unknown["error"] == wrong["error"]


def test_demo_accounts_exist_and_each_owns_a_different_scenario():
    accounts = auth.list_demo_accounts()
    assert len(accounts) == 3
    assert all("password_hash" not in a for a in accounts)

    signed_in = auth.authenticate("priya@example.com", "demo1234")
    assert signed_in["ok"] is True


def test_booking_is_refused_when_nobody_is_signed_in(anonymous_session):
    fill_valid_draft(anonymous_session)
    tools.validate_shipment(anonymous_session)

    result = tools.confirm_booking(anonymous_session)
    assert result["ok"] is False
    assert result["blocked_by"] == "not_signed_in"


def test_a_booked_shipment_belongs_to_the_customer_who_booked_it(session_id):
    customer = auth.authenticate("rahul@example.com", "demo1234")["customer"]
    tools.bind_session(session_id, customer["id"])

    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    booking = tools.confirm_booking(session_id)
    assert booking["ok"] is True

    db = SessionLocal()
    try:
        stored = db.scalars(
            select(Shipment).where(Shipment.reference == booking["reference"])
        ).one()
        assert stored.customer_id == customer["id"]
    finally:
        db.close()


def test_the_profile_fills_in_the_sender_so_it_is_not_asked_for_again(session_id):
    customer = auth.authenticate("priya@example.com", "demo1234")["customer"]
    tools.bind_session(session_id, customer["id"])

    result = tools.prefill_sender_from_profile(session_id)
    assert result["ok"] is True
    assert result["sender"]["city"] == "Chennai"
    assert result["still_missing_from_profile"] == []

    summary = tools.get_summary(session_id)
    assert summary["draft"]["sender"]["pin"] == "600002"
    # None of the six sender fields should still be outstanding.
    assert not any("sender" in field for field in summary["still_missing"])


def test_prefill_and_shipment_list_refuse_when_signed_out(anonymous_session):
    assert tools.prefill_sender_from_profile(anonymous_session)["ok"] is False
    assert tools.list_my_shipments(anonymous_session)["ok"] is False


def test_customers_only_see_their_own_shipments(session_id):
    customer = auth.authenticate("imran@example.com", "demo1234")["customer"]
    tools.bind_session(session_id, customer["id"])

    result = tools.list_my_shipments(session_id)
    assert result["ok"] is True
    references = [s["reference"] for s in result["shipments"]]
    assert references == ["RI-1077"]  # not RI-1001 or RI-1042


# ---------------------------------------------------------------------------
# City / PIN conflicts must be resolvable, not a dead end
# ---------------------------------------------------------------------------

def test_a_city_pin_mismatch_blocks_until_it_is_settled(session_id):
    fill_valid_draft(session_id, sender_city="Chennai", sender_pin="641604")
    validation = tools.validate_shipment(session_id)

    assert validation["validated"] is False
    assert any("belongs to" in e for e in validation["errors"])


def test_keeping_the_pin_rewrites_the_city_and_unblocks(session_id):
    fill_valid_draft(session_id, sender_city="Chennai", sender_pin="641604")
    tools.validate_shipment(session_id)

    resolved = tools.resolve_address_conflict(session_id, role="sender", keep="pin")
    assert resolved["ok"] is True
    assert "Tiruppur" in (resolved["city"] or "") or resolved["city"]

    validation = tools.validate_shipment(session_id)
    assert validation["validated"] is True, validation["errors"]
    assert tools.confirm_booking(session_id)["ok"] is True


def test_keeping_the_address_as_written_also_unblocks(session_id):
    """The brief allows a mismatch to be resolved OR explicitly accepted."""
    fill_valid_draft(session_id, sender_city="Chennai", sender_pin="641604")
    tools.validate_shipment(session_id)

    resolved = tools.resolve_address_conflict(session_id, role="sender", keep="city")
    assert resolved["ok"] is True

    validation = tools.validate_shipment(session_id)
    assert validation["validated"] is True, validation["errors"]
    # Accepted, but still recorded -- it is not silently forgotten.
    assert any("confirmed the address as written" in w for w in validation["warnings"])


def test_changing_the_address_again_revokes_the_acceptance(session_id):
    fill_valid_draft(session_id, sender_city="Chennai", sender_pin="641604")
    tools.resolve_address_conflict(session_id, role="sender", keep="city")
    assert tools.validate_shipment(session_id)["validated"] is True

    # A different PIN is a different disagreement, so it must be asked again.
    tools.save_draft(session_id=session_id, sender_pin="560001")
    validation = tools.validate_shipment(session_id)
    assert validation["validated"] is False
    assert any("belongs to" in e for e in validation["errors"])


def test_resolve_rejects_nonsense_arguments(session_id):
    fill_valid_draft(session_id)
    assert tools.resolve_address_conflict(session_id, role="nobody", keep="pin")["ok"] is False
    assert tools.resolve_address_conflict(session_id, role="sender", keep="maybe")["ok"] is False


def test_a_power_bank_is_blocked_even_without_the_word_battery():
    """'Power bank' names the item without saying battery."""
    for phrase in ["power bank", "powerbank", "is a spare power bank allowed?",
                   "I want to send a power bank"]:
        assert classify_contents(phrase).decision == "blocked", phrase
    # A device with its battery fitted is still acceptable.
    assert classify_contents("laptop with the battery installed").decision == "conditional"


def test_acceptance_questions_are_screened_before_the_model_answers():
    """A bare "can I send X?" gets a verdict attached, so the model cannot
    invent one. Regression: it once said a spare power bank was fine."""
    from app.agent.loop import _acceptance_note

    note = _acceptance_note("is a spare power bank allowed?")
    assert "verdict: blocked" in note
    assert "power bank" in note.lower()

    assert "verdict: conditional" in _acceptance_note("can I send medicines?")
    assert _acceptance_note("what is paracetamol used for?") == ""
    assert _acceptance_note("I want to send books") == ""


# ---------------------------------------------------------------------------
# The sender is asked about, never assumed
# ---------------------------------------------------------------------------

def test_a_role_word_is_not_accepted_as_a_name(session_id):
    """"Sender" on screen looks like real data and is not."""
    tools.save_draft(session_id, sender_name="Sender", recipient_name="me")
    draft = tools.get_summary(session_id)["draft"]

    assert not draft["sender"].get("name")
    assert not draft["recipient"].get("name")

    tools.save_draft(session_id, sender_name="Aravind Kumar")
    assert tools.get_summary(session_id)["draft"]["sender"]["name"] == "Aravind Kumar"


def test_a_customer_with_a_saved_address_is_asked_before_it_is_used(session_id):
    tools.save_draft(session_id, contents="Books")
    summary = tools.get_summary(session_id)

    assert summary["awaiting_sender_choice"] is True
    assert summary["ask_next_options"] == "sender_address"
    assert "saved on their account" in summary["ask_next_instruction"]
    assert tools.list_options("sender_address")["options"] == [
        "Use my saved details", "A different sender",
    ]


def test_a_customer_with_no_saved_address_is_offered_their_last_shipment():
    """Registering without an address should not mean retyping it forever."""
    email = f"noaddr{uuid.uuid4().hex[:6]}@example.com"
    created = auth.create_customer(email=email, password="demo1234",
                                   name="No Address")
    customer_id = created["customer"]["id"]

    first = f"test-{uuid.uuid4().hex[:12]}"
    tools.bind_session(first, customer_id)
    tools.save_draft(
        first, **{**VALID_SENDER, **VALID_RECIPIENT, **VALID_PACKAGE}
    )
    tools.validate_shipment(first)
    booking = tools.confirm_booking(first)
    assert booking["ok"] is True

    second = f"test-{uuid.uuid4().hex[:12]}"
    tools.bind_session(second, customer_id)
    tools.save_draft(second, contents="Books")
    summary = tools.get_summary(second)
    assert summary["awaiting_sender_choice"] is True
    assert "used on their last shipment" in summary["ask_next_instruction"]
    assert summary["ask_next_options"] == "sender_previous"

    reused = tools.prefill_sender_from_last_shipment(second)
    assert reused["ok"] is True
    assert tools.get_summary(second)["draft"]["sender"]["name"] == "Rahul Menon"

    db = SessionLocal()
    try:
        for sid in (first, second):
            state = db.get(ConversationState, sid)
            if state:
                if state.draft_shipment_id:
                    shipment = db.get(Shipment, state.draft_shipment_id)
                    if shipment is not None:
                        db.delete(shipment)
                db.delete(state)
        for shipment in db.scalars(
            select(Shipment).where(Shipment.customer_id == customer_id)
        ):
            db.delete(shipment)
        customer = db.get(auth.Customer, customer_id)
        if customer is not None:
            db.delete(customer)
        db.commit()
    finally:
        db.close()


def test_details_already_given_survive_the_prefill(session_id):
    """A pickup address the customer typed is never replaced by the profile."""
    tools.save_draft(session_id, sender_address="14 SS Street, Tollgate",
                     sender_pin="600081")
    tools.prefill_sender_from_profile(session_id)

    sender = tools.get_summary(session_id)["draft"]["sender"]
    assert sender["address"] == "14 SS Street, Tollgate"
    assert sender["pin"] == "600081"
    assert sender["name"] == "Rahul Menon"   # the blanks, and only the blanks


# ---------------------------------------------------------------------------
# Values at the edges of what can be stored or accepted
#
# Every case below was found by sweeping the booking flow for inputs a real
# customer could plausibly produce -- a weight in the wrong unit, a value typed
# with too many zeroes, a pasted paragraph where a description belongs.
# ---------------------------------------------------------------------------

def test_a_weight_below_a_gram_is_read_as_a_unit_mistake(session_id):
    """0.5 is grams here, but the customer almost certainly meant kilograms."""
    fill_valid_draft(session_id, weight_g=0.5)
    result = tools.validate_shipment(session_id)

    assert result["validated"] is False
    assert any("500 g" in error for error in result["errors"])


def test_a_parcel_over_the_weight_limit_is_refused(session_id):
    fill_valid_draft(session_id, weight_g=60_000)
    result = tools.validate_shipment(session_id)

    assert result["validated"] is False
    assert any("50 kg" in error for error in result["errors"])


def test_an_impossible_declared_value_is_refused_not_stored(session_id):
    """The number is wider than its column: without a guard this is a crash."""
    payload = fill_valid_draft(session_id, declared_value=10**11)

    assert payload["ok"] is True            # the rest of the draft still saved
    assert payload["not_recorded"]
    assert "1 crore" in payload["not_recorded"][0] or "10,000,000" in payload[
        "not_recorded"][0]
    assert tools.get_summary(session_id)["draft"]["declared_value"] is None


def test_an_overlong_description_is_refused_not_truncated(session_id):
    """Truncating would hide whatever was written past the cut, rules included."""
    payload = fill_valid_draft(session_id, contents="C" * 400)

    assert payload["not_recorded"]
    assert "shorter" in payload["not_recorded"][0]
    assert tools.get_summary(session_id)["draft"]["contents"] != "C" * 400


def test_a_phone_number_that_cannot_be_dialled_is_refused(session_id):
    fill_valid_draft(session_id, sender_phone="call me maybe")
    result = tools.validate_shipment(session_id)

    assert result["validated"] is False
    assert any("phone" in error for error in result["errors"])


def test_a_phone_number_written_with_a_country_code_is_accepted(session_id):
    fill_valid_draft(session_id, sender_phone="+91 98470 12345")
    result = tools.validate_shipment(session_id)

    assert result["validated"] is True


def test_correcting_the_contents_lifts_the_document_request_at_once(session_id):
    """Not one turn later: the gate is applied after the contents are screened.

    A customer who says "medicines" and corrects it to "books" in the next
    breath was otherwise still asked for a prescription, because save_draft
    gated on documents before it screened the new contents.
    """
    fill_valid_draft(session_id, contents="medicines")
    assert tools.get_summary(session_id).get("awaiting_document") == "prescription"

    payload = tools.save_draft(session_id, contents="books")

    assert payload.get("awaiting_document") is None
    assert tools.get_summary(session_id).get("awaiting_document") is None
    assert tools.validate_shipment(session_id)["validated"] is True


# ---------------------------------------------------------------------------
# The sign-up form
#
# Whatever is accepted here becomes the pre-filled sender block on every
# shipment this customer books, so the profile is held to the same rules as a
# draft -- and to the widths of its own columns, which a form will otherwise
# overflow into a database error.
# ---------------------------------------------------------------------------

def _signup(**overrides):
    fields = {
        "email": f"edge{uuid.uuid4().hex[:8]}@example.com",
        "password": "demo1234", "name": "Edge Case", "phone": "9847012345",
        "address": "1 Test Road", "city": "Kochi", "state": "Kerala",
        "pin": "682031",
    }
    fields.update(overrides)
    result = auth.create_customer(**fields)
    if result["ok"]:
        db = SessionLocal()
        try:
            customer = db.get(auth.Customer, result["customer"]["id"])
            if customer is not None:
                db.delete(customer)
                db.commit()
        finally:
            db.close()
    return result


@pytest.mark.parametrize("label,field,value", [
    ("a seven-digit PIN", "pin", "6820311"),
    ("a phone that is not a number", "phone", "not a number"),
    ("a name longer than its column", "name", "A" * 200),
    ("an address longer than its column", "address", "B" * 300),
])
def test_signup_refuses_bad_profile_details(label, field, value):
    result = _signup(**{field: value})

    assert result["ok"] is False, label
    assert result["error"]          # a sentence, not a database error


@pytest.mark.parametrize("phone", ["9847012345", "+91 98470 12345", None])
def test_signup_accepts_the_ways_people_write_a_phone_number(phone):
    assert _signup(phone=phone)["ok"] is True


def test_talking_on_after_a_booking_starts_a_new_shipment_and_says_so(session_id):
    """"Actually, make it 5 kg" must not quietly alter a confirmed booking."""
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    booking = tools.confirm_booking(session_id)
    assert booking["ok"] is True

    payload = tools.save_draft(session_id, weight_g=5000)

    assert payload["started_new_shipment"] == booking["reference"]
    assert booking["reference"] in payload["started_new_shipment_instruction"]

    # And the booked shipment is untouched by the edit.
    db = SessionLocal()
    try:
        booked = db.scalars(
            select(Shipment).where(Shipment.reference == booking["reference"])
        ).one()
        assert booked.package_json["weight_g"] == 2000
        assert booked.status == STATUS_BOOKED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reading a number the way a person wrote it
#
# The model passes through whatever the customer said. "2 kg" in a field
# counted in grams is 2000 -- and deciding that is this layer's job, because a
# model that converts units in its head is a model that will one day book a
# two-gram parcel.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("written,grams", [
    (2000, 2000), ("2000", 2000), ("2 kg", 2000), ("2kg", 2000),
    ("2.5 kg", 2500), ("500 g", 500), ("1,500 grams", 1500), ("about 2 kg", 2000),
])
def test_a_weight_is_read_in_the_unit_it_was_written_in(session_id, written, grams):
    fill_valid_draft(session_id, weight_g=written)

    assert tools.get_summary(session_id)["draft"]["package"]["weight_g"] == grams


@pytest.mark.parametrize("written,rupees", [
    ("1,500", 1500), ("Rs 900", 900), ("Rs. 2,000", 2000), ("900 INR", 900),
])
def test_a_declared_value_survives_the_way_it_was_typed(session_id, written, rupees):
    fill_valid_draft(session_id, declared_value=written)

    assert tools.get_summary(session_id)["draft"]["declared_value"] == rupees


@pytest.mark.parametrize("written,mm", [("30 cm", 300), ("1 m", 1000),
                                        ("12 inches", 304.8), ("300", 300)])
def test_a_dimension_is_converted_not_taken_at_face_value(session_id, written, mm):
    fill_valid_draft(session_id, length_mm=written)

    assert tools.get_summary(session_id)["draft"]["package"]["length_mm"] == mm


@pytest.mark.parametrize("written", ["2 pounds", "$900", "heavy"])
def test_a_unit_we_do_not_know_is_refused_rather_than_guessed_at(session_id, written):
    """Guessing here would book a parcel at a weight nobody chose."""
    payload = fill_valid_draft(session_id, weight_g=written)

    assert payload["not_recorded"]
    assert tools.get_summary(session_id)["draft"]["package"].get("weight_g") is None


@pytest.mark.parametrize("typed", ["express", "EXPRESS", "Express", "standard"])
def test_a_service_type_is_recognised_whatever_the_casing(session_id, typed):
    fill_valid_draft(session_id, service_type=typed)
    result = tools.validate_shipment(session_id)

    assert result["validated"] is True
    assert tools.get_summary(session_id)["draft"]["service_type"] == typed.capitalize()


def test_a_service_we_do_not_offer_is_still_refused(session_id):
    fill_valid_draft(session_id, service_type="Overnight")
    result = tools.validate_shipment(session_id)

    assert result["validated"] is False
    assert any("Overnight" in error for error in result["errors"])


# ---------------------------------------------------------------------------
# Screening an item makes its document requirement real
#
# The upload control is drawn from the pending documents in the database. If
# screening records nothing, the agent says "please attach the prescription"
# and the customer has nothing to attach it with.
# ---------------------------------------------------------------------------

def test_screening_medicines_raises_the_requirement_that_draws_the_uploader(
    session_id,
):
    result = tools.check_contents("medicines", session_id=session_id)

    assert result["requires_document"] == "prescription"
    assert result["recorded_on_draft"] is True
    assert tools.get_summary(session_id)["pending_documents"] == ["prescription"]
    assert "upload control on screen" in result["note"]


def test_screening_a_blocked_item_records_nothing(session_id):
    """A blocked parcel has no document that would rescue it."""
    result = tools.check_contents("a spare lithium battery", session_id=session_id)

    assert result["decision"] == "blocked"
    assert result["recorded_on_draft"] is False
    assert tools.get_summary(session_id).get("ok") is False   # no draft at all


def test_asking_about_medicines_while_sending_books_changes_nothing(session_id):
    """A question is not a change of parcel, and must not promise an uploader."""
    tools.save_draft(session_id, contents="books")

    result = tools.check_contents("medicines", session_id=session_id)

    assert result["recorded_on_draft"] is False
    assert tools.get_summary(session_id)["draft"]["contents"] == "books"
    assert tools.get_summary(session_id)["pending_documents"] == []
    assert "do NOT ask them to attach anything yet" in result["note"]


def test_screening_after_a_booking_does_not_reopen_it(session_id):
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    booking = tools.confirm_booking(session_id)
    assert booking["ok"] is True

    result = tools.check_contents("medicines", session_id=session_id)

    assert result["recorded_on_draft"] is False


# ---------------------------------------------------------------------------
# A document the agent asks for must be one the application is holding
#
# The model does not always screen the contents before asking -- resuming a
# conversation where it already knows what is being sent, it asks straight out.
# The upload control is drawn from the database, so without this the customer
# is told to attach a prescription and given nothing to attach it with.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "please go ahead and provide the prescription so I can record it",
    "Could you attach the prescription for the medicines?",
    "Please upload the prescription when you can.",
])
def test_asking_for_a_prescription_puts_the_uploader_on_screen(session_id, reply):
    from app.agent.loop import _honour_document_promise

    _honour_document_promise(session_id, reply)

    assert tools.get_summary(session_id)["pending_documents"] == ["prescription"]


@pytest.mark.parametrize("reply", [
    "Books do not need a prescription at all.",
    "If you chose medicines, a prescription would be required.",
    "What is the recipient's phone number?",
])
def test_merely_naming_a_document_promises_nothing(session_id, reply):
    """Only a request creates a requirement; a mention is just an answer."""
    from app.agent.loop import _honour_document_promise

    _honour_document_promise(session_id, reply)

    assert tools.get_summary(session_id).get("ok") is False   # no draft raised


def test_the_promise_guard_leaves_an_unrelated_parcel_alone(session_id):
    from app.agent.loop import _honour_document_promise

    tools.save_draft(session_id, contents="books")
    _honour_document_promise(session_id, "Please provide the prescription.")

    assert tools.get_summary(session_id)["draft"]["contents"] == "books"
    assert tools.get_summary(session_id)["pending_documents"] == []


# ---------------------------------------------------------------------------
# A document that has been supplied must stop looking outstanding
#
# The only trace of an upload used to be the absence of a pending request. The
# rules went on reporting that medicines require a prescription -- true of
# medicines in general, no longer true of this shipment -- so the agent read the
# requirement as open and asked for a file the customer had just attached.
# ---------------------------------------------------------------------------

def _attach_prescription(session_id):
    return tools.submit_document(
        session_id=session_id, doc_type="prescription", filename="scan.png",
        stored_path="uploads/scan.png", size_bytes=1024, content_type="image/png",
    )


def test_a_supplied_prescription_retires_the_requirement(session_id):
    fill_valid_draft(session_id, contents="medicines")
    assert tools.validate_shipment(session_id)["requires_document"] == "prescription"

    assert _attach_prescription(session_id)["ok"] is True
    after = tools.validate_shipment(session_id)

    assert after["requires_document"] is None
    assert after["documents_received"] == ["prescription"]
    assert "Do NOT ask for any of these again" in after[
        "documents_received_instruction"]


def test_the_medicines_warning_is_not_repeated_once_it_is_met(session_id):
    """A condition that has been satisfied is not a warning to read out again."""
    fill_valid_draft(session_id, contents="medicines")
    assert any("prescription" in w.lower()
               for w in tools.validate_shipment(session_id)["warnings"])

    _attach_prescription(session_id)

    assert tools.validate_shipment(session_id)["warnings"] == []


@pytest.mark.parametrize("tool", ["get_summary", "save_draft"])
def test_every_payload_says_the_document_is_on_file(session_id, tool):
    fill_valid_draft(session_id, contents="medicines")
    _attach_prescription(session_id)

    payload = (tools.get_summary(session_id) if tool == "get_summary"
               else tools.save_draft(session_id, weight_g=500))

    assert payload["documents_received"] == ["prescription"]


def test_the_promise_guard_does_not_reopen_a_supplied_document(session_id):
    from app.agent.loop import _honour_document_promise

    fill_valid_draft(session_id, contents="medicines")
    _attach_prescription(session_id)

    _honour_document_promise(session_id, "Please attach the prescription.")

    assert tools.get_summary(session_id)["pending_documents"] == []


def test_a_supplied_document_lets_the_booking_through(session_id):
    fill_valid_draft(session_id, contents="medicines")
    _attach_prescription(session_id)
    tools.validate_shipment(session_id)

    booking = tools.confirm_booking(session_id)

    assert booking["ok"] is True, booking


# ---------------------------------------------------------------------------
# The choices are on screen as buttons, so the reply must not list them too
# ---------------------------------------------------------------------------

def test_a_bulleted_list_of_the_choices_is_removed_from_the_reply():
    """The prompt already forbids this and the model does it anyway."""
    from app.agent.loop import _strip_listed_options
    from app.rules import CONTENTS_CATEGORIES

    reply = (
        "I'm sorry for the confusion. Please choose the category that best "
        "fits your parcel:\n\n"
        + "\n".join(f"* {c}" for c in CONTENTS_CATEGORIES)
    )

    cleaned = _strip_listed_options(reply, CONTENTS_CATEGORIES)

    assert "* Documents" not in cleaned
    assert "Medicines" not in cleaned
    assert cleaned.endswith("best fits your parcel.")


def test_a_numbered_list_is_removed_too():
    from app.agent.loop import _strip_listed_options

    cleaned = _strip_listed_options(
        "Which service?\n1. Standard\n2. Express", ["Standard", "Express"]
    )

    assert cleaned == "Which service?"


def test_a_choice_named_in_a_sentence_is_left_alone():
    """"Express it is" is a sentence, not a menu."""
    from app.agent.loop import _strip_listed_options

    reply = "Express it is, thanks."

    assert _strip_listed_options(reply, ["Standard", "Express"]) == reply


def test_nothing_is_stripped_when_no_buttons_are_shown():
    from app.agent.loop import _strip_listed_options

    reply = "* Documents\n* Books"

    assert _strip_listed_options(reply, []) == reply


# ---------------------------------------------------------------------------
# Starting again has to actually start again
#
# "Create a new shipment" was only ever a sentence sent to the model, so the
# draft already attached to the conversation stayed attached: a customer who
# had just finished a parcel of medicines was greeted with "I have your
# prescription on file" and a contents field already filled in.
# ---------------------------------------------------------------------------

def test_starting_a_new_shipment_discards_the_previous_draft(session_id):
    fill_valid_draft(session_id, contents="medicines")
    _attach_prescription(session_id)
    assert tools.get_summary(session_id)["documents_received"] == ["prescription"]

    result = tools.start_new_shipment(session_id)

    assert result["ok"] is True
    assert result["discarded_draft"]["contents"] == "medicines"
    assert tools.get_summary(session_id)["ok"] is False   # nothing carried over


def test_the_new_shipment_button_clears_the_draft_without_the_model(session_id):
    """The label is ours, so the application acts on it rather than hoping."""
    from app.agent.loop import _record_chosen_option

    fill_valid_draft(session_id, contents="medicines")
    _attach_prescription(session_id)

    _record_chosen_option(session_id, "Create a new shipment")

    assert tools.get_summary(session_id)["ok"] is False


def test_starting_again_never_touches_a_booked_shipment(session_id):
    fill_valid_draft(session_id)
    tools.validate_shipment(session_id)
    booking = tools.confirm_booking(session_id)

    tools.start_new_shipment(session_id)

    tracked = tools.get_tracking(booking["reference"])
    assert tracked["ok"] is True
    assert tracked["status"] == STATUS_BOOKED


def test_starting_again_on_an_empty_conversation_is_harmless(session_id):
    assert tools.start_new_shipment(session_id)["ok"] is True
    assert tools.start_new_shipment(session_id)["discarded_draft"] is None
