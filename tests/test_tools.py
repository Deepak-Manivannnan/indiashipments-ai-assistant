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


SEEDED_REFERENCES = {"IS-1001", "IS-1042", "IS-1077"}


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
    assert booking["reference"].startswith("IS-")


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
    assert reference.startswith("IS-")

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
    result = tools.get_tracking("IS-9999")
    assert result["ok"] is False
    assert "IS-9999" in result["error"]


def test_tracking_a_seeded_shipment_returns_its_real_events():
    result = tools.get_tracking("IS-1077")
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
    assert references == ["IS-1077"]  # not IS-1001 or IS-1042


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
