"""Home, Services, My Orders and About."""

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
    return f'<div class="ri-card"><h4>{title}</h4><p>{body}</p></div>'


def _card_row(cards: list[tuple[str, str]]) -> None:
    """A row of cards that all end up the same height.

    Streamlit columns size to their own content, so three cards with different
    amounts of text come out ragged. The stretching is done in CSS, against any
    column that contains a card -- a wrapper div emitted through st.markdown
    would be a sibling of the columns rather than their parent, and would do
    nothing at all.
    """
    columns = st.columns(len(cards), gap="medium")
    for column, (title, body) in zip(columns, cards):
        with column:
            st.markdown(_card(title, body), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def home() -> None:
    styles.inject()
    customer = st.session_state.get("customer")

    if customer:
        first_name = customer["name"].split()[0]
        st.markdown(
            f"""
            <div class="ri-hero">
              <h1>Hi {first_name}</h1>
              <p>Your parcels, moving across India. Book a collection, follow a
              delivery, or pick up where you left off.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="ri-hero">
              <h1>Parcel delivery across India</h1>
              <p>Rapid India moves parcels between every serviceable PIN code
              in the country, with Standard and Express options, verified
              addresses and tracking from collection to doorstep.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    cards = [
        ("Nationwide coverage",
         "We deliver to every PIN code in the India Post directory. Addresses "
         "are checked before a booking is confirmed, so parcels do not set off "
         "towards somewhere we cannot reach."),
        ("Standard and Express",
         "Choose the everyday service, or Express when it needs to arrive "
         "sooner. Fragile goods, perishables and other special contents are "
         "handled under clear, published conditions."),
        ("Tracking that tells you the truth",
         "Every parcel carries a tracking reference and a timestamped history. "
         "If a delivery is delayed or an attempt fails, we say so plainly "
         "rather than leaving you guessing."),
    ]
    _card_row(cards)

    st.write("")
    st.markdown("### Booking a parcel")
    _card_row([
        ("1", "Give us the collection and delivery addresses"),
        ("2", "Tell us what is inside and what it weighs"),
        ("3", "Review the shipment and the applicable conditions"),
        ("4", "Confirm, and we send you a tracking reference"),
    ])

    st.write("")
    st.caption(
        "Prefer to talk it through? RIA, our booking assistant, can take the "
        "details in conversation and book the parcel for you."
    )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def services() -> None:
    styles.inject()

    st.markdown("## Services")
    st.markdown(
        '<p class="ri-muted">Domestic shipments within India. These are the '
        "conditions we apply, and we apply them the same way every time.</p>",
        unsafe_allow_html=True,
    )

    _card_row([
        ("Standard",
         "Our everyday service for parcels that are not time critical."),
        ("Express",
         "Faster handling for urgent parcels and perishables."),
    ])

    st.write("")
    st.markdown("### What you can send")
    st.markdown(
        '<p class="ri-muted">' + " &middot; ".join(CONTENTS_CATEGORIES) + "</p>",
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
        f"**Contents valued above Rs {HIGH_VALUE_THRESHOLD_INR:,}** — you will be "
        "asked to acknowledge that our liability is limited without insurance.",
    ]:
        st.markdown(f"- {item}")

    st.markdown("### Size and weight")
    st.markdown(
        f"- Minimum parcel size: {MIN_DIMENSIONS_MM[0]} x {MIN_DIMENSIONS_MM[1]} "
        f"x {MIN_DIMENSIONS_MM[2]} mm\n"
        f"- Length, width and height together must not exceed "
        f"{MAX_TOTAL_DIMENSIONS_MM:,} mm\n"
        "- Weight sets the price alongside distance, so we ask for it on every "
        "booking\n"
        "- Delivery PIN codes are checked against the India Post directory"
    )
    st.caption(
        f"Services offered: {', '.join(SERVICE_TYPES)}. These conditions are a "
        "simplified set for this project and are not a statement of India Post "
        "regulations."
    )


# ---------------------------------------------------------------------------
# My orders
# ---------------------------------------------------------------------------

def orders() -> None:
    styles.inject()

    st.markdown("## My orders")
    if not components.require_sign_in("see your shipments"):
        return

    customer = st.session_state["customer"]
    session_id = st.session_state.get("session_id") or f"orders-{customer['id']}"
    st.session_state.setdefault("session_id", session_id)
    api.bind_session(session_id, customer["id"])

    shipments = api.my_shipments(session_id)
    if shipments:
        st.markdown(
            f'<p class="ri-muted">{len(shipments)} shipment'
            f'{"s" if len(shipments) > 1 else ""}.</p>',
            unsafe_allow_html=True,
        )
        for shipment in shipments:
            components.shipment_card(shipment)
            st.write("")
    else:
        st.markdown(
            '<p class="ri-muted">You have no shipments yet.</p>',
            unsafe_allow_html=True,
        )
        if st.button("Book your first parcel", type="primary"):
            st.session_state["page"] = "ria"
            st.rerun()

    st.divider()
    st.markdown("#### Track any reference")
    st.markdown(
        '<p class="ri-muted">Looking for a parcel someone else sent you? Enter '
        "its reference here.</p>",
        unsafe_allow_html=True,
    )
    reference = st.text_input("Shipment reference", placeholder="RI-1042",
                              label_visibility="collapsed")
    if not reference:
        return

    ok, payload = api.tracking(reference)
    if not ok:
        st.error(payload.get("detail", "That shipment could not be found."))
        return

    left, right = st.columns([2, 1])
    with left:
        st.markdown(f'<span class="ri-ref">{payload["reference"]}</span>',
                    unsafe_allow_html=True)
    with right:
        st.markdown(styles.chip(payload["status"]), unsafe_allow_html=True)
    st.markdown(f'<p class="ri-muted">{payload["explanation"]}</p>',
                unsafe_allow_html=True)
    st.write("")
    components.timeline(payload["events"], payload["status"])


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

def about() -> None:
    styles.inject()

    st.markdown("## About Rapid India")
    st.markdown(
        "Rapid India is a domestic parcel carrier. We collect from the "
        "sender's door and deliver anywhere in India that the postal network "
        "reaches, handling everything from documents and clothing to fragile "
        "goods and perishables."
    )

    st.write("")
    _card_row([
        ("Our network",
         "Collections and deliveries across every serviceable PIN code in "
         "India, moving through regional hubs to the destination city and out "
         "for delivery from there."),
        ("How we work",
         "Addresses are verified before a parcel is accepted, contents are "
         "checked against our published conditions, and every parcel is "
         "tracked from collection to delivery."),
        ("Being straight with you",
         "We do not promise delivery dates we cannot support, and when a "
         "delivery is delayed or fails we tell you what actually happened and "
         "what to do next."),
    ])

    st.write("")
    st.markdown("### What we ask for, and why")
    st.markdown(
        "- **Collection and delivery addresses**, with PIN codes, so we can "
        "confirm we serve the destination before accepting the parcel.\n"
        "- **Contents**, so we can apply the right conditions — cushioning for "
        "fragile goods, a prescription for medicines, and so on.\n"
        "- **Weight and size**, which together with distance determine the "
        "charge.\n"
        "- **Value of the contents**, which sets the compensation limit if a "
        "parcel is lost or damaged. It is not the postage, and it is not what "
        "you pay."
    )

    st.markdown("### Booking with RIA")
    st.markdown(
        "RIA is our booking assistant. It takes the details in conversation "
        "rather than as a form, applies the same published conditions as any "
        "other booking, and shows you the full shipment for review before "
        "anything is confirmed."
    )

    st.markdown("### Notes on this build")
    st.caption(
        "This application was built as an AI shipment agent challenge "
        "project. Tracking references are generated by this application and "
        "are not carrier air waybills; prices are this application's own "
        "estimate rather than a carrier quote; and document review is "
        "simulated, so an uploaded file is recorded as received but never "
        "described as verified."
    )
