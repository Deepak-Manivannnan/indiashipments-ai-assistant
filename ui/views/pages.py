"""Home, Services, Track, My Orders and About."""

import streamlit as st

from app.constants import SERVICE_TYPES
from app.rules import (
    CONTENTS_CATEGORIES,
    HIGH_VALUE_THRESHOLD_INR,
    MAX_TOTAL_DIMENSIONS_MM,
    MIN_DIMENSIONS_MM,
)
from ui import api, components, styles


def _card(title: str, body: str) -> str:
    return f'<div class="is-card"><h4>{title}</h4><p>{body}</p></div>'


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def home() -> None:
    styles.inject()
    components.header()

    st.markdown(
        """
        <div class="is-hero">
          <h1>Send a parcel by describing it</h1>
          <p>IndiaShipments handles domestic deliveries across India. Tell our
          assistant what you want to send, in your own words, and it will work
          out the details, check the rules and book it for you.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.button("Ask ISA to book a shipment", type="primary"):
        st.session_state["page"] = "isa"
        st.rerun()

    st.write("")
    columns = st.columns(3)
    cards = [
        ("Describe it, don't fill a form",
         "Say what you are sending and where. ISA asks only for what it still "
         "needs, one question at a time."),
        ("Checked before it is booked",
         "Addresses are verified against India Post, and every parcel is "
         "screened against our acceptance rules before a booking is created."),
        ("Tracking in plain language",
         "Ask where a parcel is and get a straight answer from its real "
         "delivery history -- including when something has gone wrong."),
    ]
    for column, (title, body) in zip(columns, cards):
        with column:
            st.markdown(_card(title, body), unsafe_allow_html=True)

    st.write("")
    st.markdown("### How it works")
    steps = st.columns(4)
    for column, (number, text) in zip(
        steps,
        [
            ("1", "Tell ISA what you are sending"),
            ("2", "Answer a few short questions"),
            ("3", "Review the shipment summary"),
            ("4", "Confirm and get your tracking reference"),
        ],
    ):
        with column:
            st.markdown(
                f'<div class="is-card"><h4 style="color:#0F4C81">{number}</h4>'
                f'<p>{text}</p></div>',
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def services() -> None:
    styles.inject()
    components.header()

    st.markdown("## Services")
    st.markdown(
        '<p class="is-muted">Domestic shipments within India. These are the '
        "rules the assistant applies, and it applies them the same way every "
        "time.</p>",
        unsafe_allow_html=True,
    )

    left, right = st.columns(2)
    with left:
        st.markdown(
            _card("Standard", "Our everyday service for parcels that are not "
                              "time critical."),
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            _card("Express", "Faster handling for urgent parcels and "
                             "perishables."),
            unsafe_allow_html=True,
        )

    st.write("")
    st.markdown("### What you can send")
    st.markdown(
        '<p class="is-muted">' + " &middot; ".join(CONTENTS_CATEGORIES) + "</p>",
        unsafe_allow_html=True,
    )

    st.markdown("### What we cannot carry")
    for item in [
        "Currency, cash or coins",
        "Explosives, fireworks, flammable liquids or compressed gases",
        "Firearms, ammunition or weapons",
        "Narcotics, poisons or radioactive material",
        "Live animals",
        "Counterfeit or otherwise prohibited goods",
    ]:
        st.markdown(f"- {item}")

    st.markdown("### Accepted with conditions")
    for item in [
        "**Lithium batteries** — only when installed in the equipment they "
        "power. Loose or spare batteries, including power banks, cannot be sent.",
        "**Liquids** — must be in leak-proof packaging.",
        "**Fragile goods** — must be cushioned inside the box.",
        "**Perishables** — need suitable packaging and a fast enough service.",
        "**Medicines** — a prescription or supporting document is required.",
        f"**Declared value above Rs {HIGH_VALUE_THRESHOLD_INR:,}** — you will be "
        "asked to acknowledge that our liability is limited without insurance.",
    ]:
        st.markdown(f"- {item}")

    st.markdown("### Size and weight")
    st.markdown(
        f"- Minimum parcel size: {MIN_DIMENSIONS_MM[0]} x {MIN_DIMENSIONS_MM[1]} "
        f"x {MIN_DIMENSIONS_MM[2]} mm\n"
        f"- Length, width and height together must not exceed "
        f"{MAX_TOTAL_DIMENSIONS_MM:,} mm\n"
        "- Weight, dimensions and declared value must all be greater than zero\n"
        "- Delivery PIN codes are checked against the India Post directory"
    )
    st.caption(
        f"Services offered: {', '.join(SERVICE_TYPES)}. These rules are a "
        "simplified set for this project and are not a statement of India Post "
        "regulations."
    )


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

def track() -> None:
    styles.inject()
    components.header()

    st.markdown("## Track a shipment")
    st.markdown(
        '<p class="is-muted">Enter a shipment reference to see its delivery '
        "history. This is an IndiaShipments tracking reference, not a carrier "
        "air waybill.</p>",
        unsafe_allow_html=True,
    )

    reference = st.text_input("Shipment reference", placeholder="IS-1042")
    if not reference:
        return

    ok, payload = api.tracking(reference)
    if not ok:
        st.error(payload.get("detail", "That shipment could not be found."))
        return

    left, right = st.columns([2, 1])
    with left:
        st.markdown(f'<span class="is-ref">{payload["reference"]}</span>',
                    unsafe_allow_html=True)
    with right:
        st.markdown(styles.chip(payload["status"]), unsafe_allow_html=True)

    st.markdown(f'<p class="is-muted">{payload["explanation"]}</p>',
                unsafe_allow_html=True)
    st.write("")
    components.timeline(payload["events"], payload["status"])


# ---------------------------------------------------------------------------
# My orders
# ---------------------------------------------------------------------------

def orders() -> None:
    styles.inject()
    components.header()

    st.markdown("## My orders")
    if not components.require_sign_in("see your shipments"):
        return

    customer = st.session_state["customer"]
    session_id = st.session_state.get("session_id")
    if not session_id:
        session_id = f"orders-{customer['id']}"
        st.session_state["orders_session"] = session_id
    api.bind_session(session_id, customer["id"])

    shipments = api.my_shipments(session_id)
    if not shipments:
        st.markdown(
            '<p class="is-muted">You have no shipments yet.</p>',
            unsafe_allow_html=True,
        )
        if st.button("Book your first shipment with ISA", type="primary"):
            st.session_state["page"] = "isa"
            st.rerun()
        return

    st.markdown(
        f'<p class="is-muted">{len(shipments)} shipment'
        f'{"s" if len(shipments) > 1 else ""}.</p>',
        unsafe_allow_html=True,
    )
    for shipment in shipments:
        components.shipment_card(shipment)
        st.write("")


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

def about() -> None:
    styles.inject()
    components.header()

    st.markdown("## About IndiaShipments")
    st.markdown(
        "IndiaShipments is a domestic logistics service. This application was "
        "built for the IndiaShipments AI Shipment Agent Challenge."
    )

    st.markdown("### How ISA works")
    st.markdown(
        "ISA is a language model that calls the application's own functions. It "
        "decides **when** to look something up, validate a parcel or create a "
        "booking. It does not decide **what is allowed** — that lives in "
        "ordinary Python, and the tools refuse anything invalid."
    )
    st.markdown(
        "- A booking cannot be created until validation has passed, every "
        "requested document has been supplied, and the high-value insurance "
        "warning has been acknowledged where it applies.\n"
        "- Changing a detail after validation cancels that validation, so a "
        "shipment can never be checked and then quietly altered before booking.\n"
        "- Prices, references, statuses and tracking events shown to you always "
        "come from the application. ISA is not permitted to state one that a "
        "function did not return."
    )

    st.markdown("### External services")
    st.markdown(
        "- **India Post PIN lookup** confirms that a destination exists and "
        "returns its real district and state. If it is unreachable, the "
        "assistant says so and preserves your draft rather than guessing.\n"
        "- **OpenStreetMap geocoding** converts PIN codes to coordinates so the "
        "distance between origin and destination can be calculated. If a PIN "
        "cannot be located, the distance is reported as unknown rather than "
        "estimated."
    )

    st.markdown("### Known limitations")
    st.markdown(
        "- Document review is simulated. A supplied file is recorded as "
        "received; its contents are never read, and it is never described as "
        "verified.\n"
        "- Prices are this application's own estimate, not a carrier quote.\n"
        "- Tracking references are generated by this application and are not "
        "carrier air waybills.\n"
        "- Sign-in is not production-grade: there are no sessions or tokens."
    )
