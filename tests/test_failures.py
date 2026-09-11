"""Failure paths.

The brief asks for honest behaviour when things go wrong: an unreachable
service, a model that cannot be called, tracking that cannot support the
answer the user wants. These are the tests for that, and they matter more than
the happy path -- a booking that works when everything is up is not the part
that is hard.
"""

import uuid

import httpx
import pytest

from app import auth, tools
from app.agent import loop
from app.rules import validate_draft
from app.services import geocode, pin_api

VALID_DRAFT = {
    "sender_name": "Rahul Menon", "sender_phone": "9847012345",
    "sender_address": "12 Marine Drive", "sender_city": "Kochi",
    "sender_state": "Kerala", "sender_pin": "682031",
    "recipient_name": "Anil Kumar", "recipient_phone": "9880123456",
    "recipient_address": "44 MG Road", "recipient_city": "Bengaluru",
    "recipient_state": "Karnataka", "recipient_pin": "560001",
    "weight_g": 2000, "length_mm": 300, "width_mm": 200, "height_mm": 150,
    "service_type": "Standard", "contents": "Books", "declared_value": 900,
}


@pytest.fixture
def signed_in_session():
    sid = f"fail-{uuid.uuid4().hex[:12]}"
    account = auth.authenticate("rahul@example.com", "demo1234")
    assert account["ok"], "seed the database before running the tests"
    tools.bind_session(sid, account["customer"]["id"])
    yield sid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ConversationState, Shipment

    db = SessionLocal()
    try:
        state = db.get(ConversationState, sid)
        if state:
            if state.draft_shipment_id:
                shipment = db.get(Shipment, state.draft_shipment_id)
                if shipment is not None:
                    db.delete(shipment)
            db.delete(state)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def pin_service_down(monkeypatch):
    """Every PIN lookup times out."""
    pin_api.clear_cache()

    def unreachable(*args, **kwargs):
        raise httpx.ConnectTimeout("simulated outage")

    monkeypatch.setattr(httpx, "get", unreachable)
    yield
    pin_api.clear_cache()


# ---------------------------------------------------------------------------
# The PIN service is unreachable
# ---------------------------------------------------------------------------

def test_pin_lookup_reports_an_outage_rather_than_raising(pin_service_down):
    result = pin_api.lookup_pin("682031")

    assert result["status"] == "unavailable"
    assert "not reachable" in result["message"]


def test_an_outage_is_not_mistaken_for_an_unserviceable_pin(pin_service_down):
    """A service that is down must never look like a rejected address."""
    result = tools.check_pin_serviceability("682031", city="Kochi")

    assert result["ok"] is True          # the tool itself did not fail
    assert result["serviceable"] is False  # but nothing was confirmed
    assert result["status"] == "unavailable"
    assert result["status"] != pin_api.STATUS_NOT_SERVICEABLE


def test_an_outage_warns_but_does_not_block_the_booking(pin_service_down):
    """The brief: fail clearly and keep the draft. Not: refuse to proceed."""
    draft = {
        "sender": {"name": "A", "phone": "9847012345", "address": "x", "city": "Kochi",
                   "state": "Kerala", "pin": "682031"},
        "recipient": {"name": "B", "phone": "9880123456", "address": "y",
                      "city": "Bengaluru", "state": "Karnataka", "pin": "560001"},
        "package": {"weight_g": 2000, "length_mm": 300, "width_mm": 200,
                    "height_mm": 150},
        "service_type": "Standard", "contents": "Books", "declared_value": 900,
        "pin_checks": {"sender": pin_api.lookup_pin("682031")},
    }
    result = validate_draft(draft)

    assert result.ok is True
    assert any("could not be verified" in w for w in result.warnings)
    assert result.errors == []


def test_the_draft_survives_an_outage(signed_in_session, pin_service_down):
    tools.save_draft(session_id=signed_in_session, **VALID_DRAFT)
    tools.validate_shipment(signed_in_session)

    summary = tools.get_summary(signed_in_session)
    assert summary["ok"] is True
    assert summary["draft"]["sender"]["city"] == "Kochi"
    assert summary["still_missing"] == []


# ---------------------------------------------------------------------------
# Geocoding is unreachable
# ---------------------------------------------------------------------------

def test_distance_is_unknown_rather_than_guessed(monkeypatch):
    geocode.clear_cache()
    monkeypatch.setattr(
        geocode, "_query_nominatim",
        lambda params: (_ for _ in ()).throw(httpx.ConnectTimeout("down")),
    )

    result = tools.calculate_distance("682031", "560001")
    assert result["status"] == "unknown"
    assert result["distance_km"] is None
    geocode.clear_cache()


def test_no_price_is_invented_when_the_distance_is_unknown():
    price = tools.estimate_price(weight_g=2000, service_type="Standard",
                                 distance_km=None)
    assert price["status"] == "unavailable"
    assert "estimated_price_inr" not in price


# ---------------------------------------------------------------------------
# The model is unreachable
# ---------------------------------------------------------------------------

def test_a_model_outage_keeps_the_draft_and_says_so(signed_in_session, monkeypatch):
    tools.save_draft(session_id=signed_in_session, **VALID_DRAFT)

    def unavailable(*args, **kwargs):
        raise loop.ModelUnavailable("simulated outage")

    monkeypatch.setattr(loop, "_generate", unavailable)

    turn = loop.run_turn(signed_in_session, "is that everything?")
    assert turn["degraded"] is True
    assert "nothing you've told me has been lost" in turn["reply"].lower()
    # And it really is still there.
    assert tools.get_summary(signed_in_session)["draft"]["contents"] == "Books"


def test_a_tool_that_explodes_is_reported_not_hidden(signed_in_session, monkeypatch):
    """An internal fault must reach the model as a refusal it can explain."""
    def boom(*args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setitem(loop.CALLABLE_TOOLS, "get_summary", boom)

    result = loop.execute_tool("get_summary", {}, signed_in_session)
    assert result["ok"] is False
    assert "database on fire" in result["error"]
    assert "do not claim it succeeded" in result["error"]


# ---------------------------------------------------------------------------
# Tracking that cannot support the answer
# ---------------------------------------------------------------------------

def test_an_unknown_reference_is_not_dressed_up():
    result = tools.get_tracking("RI-9999")
    assert result["ok"] is False
    assert "check the reference" in result["error"]


def test_a_failed_delivery_is_reported_as_a_failure():
    result = tools.get_tracking("RI-1077")

    assert result["status"] == "Delivery failed"
    assert "Delivery failed" in result["explanation"]
    assert "recipient not available" in result["explanation"].lower()
    # The delay before it is in the history, so the agent can explain why.
    assert any("delayed" in (e["note"] or "").lower() for e in result["events"])


def test_tracking_never_offers_a_delivery_date():
    """Nothing in a tracking response may imply a future date."""
    for reference in ("RI-1001", "RI-1042", "RI-1077"):
        explanation = tools.get_tracking(reference)["explanation"].lower()
        for phrase in ("will arrive", "expected by", "due on", "estimated delivery",
                       "should arrive"):
            assert phrase not in explanation, (reference, phrase)


def test_a_delivered_shipment_reads_as_finished():
    result = tools.get_tracking("RI-1001")
    assert result["status"] == "Delivered"
    assert result["events"][-1]["status"] == "Delivered"


# ---------------------------------------------------------------------------
# The quote shown in the panel
# ---------------------------------------------------------------------------

def test_distance_and_price_appear_once_the_draft_allows_them(signed_in_session):
    """The application works these out; it does not wait to be asked."""
    tools.save_draft(signed_in_session, contents="Books")
    assert "distance_km" not in loop._build_state(signed_in_session, None)

    # Both PIN codes are enough for the distance -- someone wants to know how
    # far their parcel is going long before they have chosen a service.
    tools.save_draft(signed_in_session, sender_pin="682031",
                     recipient_pin="560001")
    part_way = loop._build_state(signed_in_session, None)
    assert 300 < part_way["distance_km"] < 420
    assert part_way["estimated_price_inr"] is None

    tools.save_draft(signed_in_session, weight_g=2000, service_type="Standard")
    state = loop._build_state(signed_in_session, None)
    assert 300 < state["distance_km"] < 420        # Kochi -> Bengaluru
    assert state["estimated_price_inr"] > 0

    standard = state["estimated_price_inr"]
    tools.save_draft(signed_in_session, service_type="Express")
    assert loop._build_state(signed_in_session, None)["estimated_price_inr"] > standard


def test_no_price_is_shown_when_the_distance_cannot_be_found(signed_in_session,
                                                             monkeypatch):
    tools.save_draft(signed_in_session, sender_pin="682031",
                     recipient_pin="560001", contents="Books",
                     weight_g=2000, service_type="Standard")
    monkeypatch.setattr(
        tools, "calculate_distance",
        lambda *a, **k: {"ok": True, "status": "unknown", "distance_km": None},
    )
    state = loop._build_state(signed_in_session, None)
    assert state["distance_km"] is None
    assert state["estimated_price_inr"] is None


# ---------------------------------------------------------------------------
# Choices made on our own buttons
# ---------------------------------------------------------------------------

def test_tapping_a_category_records_it(signed_in_session):
    """The buttons are ours, so their answers are ours to record.

    Regression: a customer tapped Medicines, was told a prescription was
    needed, and the contents stayed blank because the model screened the word
    without saving it.
    """
    tools.save_draft(signed_in_session, weight_g=5000)
    loop._record_chosen_option(signed_in_session, "Medicines")

    summary = tools.get_summary(signed_in_session)
    assert summary["draft"]["contents"] == "Medicines"
    assert summary["awaiting_document"] == "prescription"

    loop._record_chosen_option(signed_in_session, "Express")
    assert tools.get_summary(signed_in_session)["draft"]["service_type"] == "Express"


def test_other_says_nothing_about_the_contents(signed_in_session):
    tools.save_draft(signed_in_session, weight_g=5000)
    loop._record_chosen_option(signed_in_session, "Other")
    assert not tools.get_summary(signed_in_session)["draft"]["contents"]


def test_no_buttons_are_offered_while_a_document_is_outstanding(signed_in_session):
    """Contents categories under "please attach the prescription" answer a
    question nobody asked."""
    tools.save_draft(signed_in_session, contents="Medicines", weight_g=5000)
    state = loop._build_state(signed_in_session, None)

    stale = [loop.ToolCallRecord(
        name="list_options", args={"field": "contents_category"}, ok=True,
        result=tools.list_options("contents_category"),
    )]
    options, _ = loop._options_for(stale, state)
    assert options == []


def test_answering_the_sender_question_settles_it(signed_in_session):
    """Regression: the customer chose "A different sender" and was offered the
    same two buttons again, because nothing recorded the answer."""
    tools.save_draft(signed_in_session, weight_g=5000)
    assert tools.get_summary(signed_in_session)["awaiting_sender_choice"] is True

    loop._record_chosen_option(signed_in_session, "A different sender")
    after = tools.get_summary(signed_in_session)
    assert after.get("awaiting_sender_choice", False) is False
    assert after["ask_next_options"] != "sender_address"


def test_accepting_the_offer_also_settles_it(signed_in_session):
    tools.save_draft(signed_in_session, weight_g=5000)
    loop._record_chosen_option(signed_in_session, "Use my saved details")

    after = tools.get_summary(signed_in_session)
    assert after.get("awaiting_sender_choice", False) is False
    assert after["draft"]["sender"]["name"] == "Rahul Menon"
    assert after["draft"]["sender"]["phone"] == "9847012345"


def test_the_question_and_its_buttons_always_match(signed_in_session):
    """Regression: the customer was asked for the sender's address and offered
    a list of parcel contents underneath it."""
    tools.save_draft(signed_in_session, weight_g=5000)
    loop._record_chosen_option(signed_in_session, "A different sender")

    after = tools.get_summary(signed_in_session)
    assert after["awaiting_sender_details"] is True
    assert after["ask_next"] == "the sender's details"
    assert after["ask_next_options"] is None      # a free-text question

    # Once answered, the next fixed-choice question brings its own list back.
    tools.save_draft(signed_in_session, sender_name="Priya S",
                     sender_phone="9840055667", sender_address="8 Anna Salai",
                     sender_city="Chennai", sender_pin="600002")
    nxt = tools.get_summary(signed_in_session)
    assert nxt["ask_next_options"] == "contents_category"


def test_the_order_follows_the_customer(signed_in_session):
    """Nothing already given is asked for again, whatever order it arrives in."""
    tools.save_draft(
        signed_in_session, contents="Books", service_type="Express",
        declared_value=900, weight_g=2000,
    )
    loop._record_chosen_option(signed_in_session, "Use my saved details")

    summary = tools.get_summary(signed_in_session)
    for already_given in ("contents", "service type", "value of the contents",
                          "package weight", "sender"):
        assert already_given not in (summary["ask_next"] or "")


def test_buttons_are_withheld_when_they_do_not_answer_the_question():
    """Regression: "Who is the recipient?" was shown with Standard/Express.

    The draft says which field is next; the model does not always ask about
    that field. Buttons under an unrelated question are worse than none,
    because tapping one answers something nobody asked.
    """
    state = {"has_draft": True, "ask_next_options": "service_type"}

    options, field = loop._options_for([], state, "Who is the recipient of this parcel?")
    assert options == [] and field is None

    options, field = loop._options_for([], state, "Which service would you like?")
    assert options == ["Standard", "Express"] and field == "service_type"


def test_buttons_appear_when_the_wording_varies():
    """The model rephrases constantly, so matching cannot be literal."""
    state = {"has_draft": True, "ask_next_options": "service_type"}
    for phrasing in ("How quickly does it need to get there?",
                     "Would you like Standard or Express?",
                     "Which delivery option suits you?"):
        options, _ = loop._options_for([], state, phrasing)
        assert options, phrasing
