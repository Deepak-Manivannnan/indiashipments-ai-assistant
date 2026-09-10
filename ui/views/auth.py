"""Sign in and account creation.

Anyone can register at any time. Seeded demo accounts exist in the database
for testing, but their credentials are deliberately not shown here -- they are
listed in the setup instructions, not in the product.
"""

import uuid

import streamlit as st

from ui import api, styles


def _sign_in_as(customer: dict) -> None:
    st.session_state["customer"] = customer
    # A new conversation per sign-in: chat history does not carry across
    # sessions, though any unfinished draft is still held in the database.
    for key in ("session_id", "messages", "state", "options", "greeted"):
        st.session_state.pop(key, None)

    # The conversation is created and bound now rather than when ISA is first
    # opened, so its id can go into the URL and survive a page refresh.
    session_id = f"ui-{uuid.uuid4().hex[:16]}"
    if api.bind_session(session_id, customer["id"]):
        st.session_state["session_id"] = session_id
        st.query_params["sid"] = session_id
    # Land on the home page, like any other site. ISA opens only when the
    # user chooses to open it.
    st.session_state["page"] = "home"
    st.rerun()


def _sign_in_form() -> None:
    with st.form("signin"):
        email = st.text_input("Email", placeholder="you@example.com")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary",
                                          use_container_width=True)

    if submitted:
        ok, payload = api.sign_in(email, password)
        if ok:
            _sign_in_as(payload["customer"])
        else:
            st.error(payload.get("detail", "Sign in failed."))


def _sign_up_form() -> None:
    st.markdown(
        '<p class="is-muted">Your address is optional, but if you add it ISA '
        "will fill in the sender details for you instead of asking.</p>",
        unsafe_allow_html=True,
    )
    with st.form("signup"):
        name = st.text_input("Full name")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password",
                                 help="At least 8 characters.")
        phone = st.text_input("Phone")

        st.markdown("**Your address** (optional)")
        address = st.text_input("Street address")
        city, state_name, pin = st.columns(3)
        with city:
            city_value = st.text_input("City")
        with state_name:
            state_value = st.text_input("State")
        with pin:
            pin_value = st.text_input("PIN code", max_chars=6)

        submitted = st.form_submit_button("Create account", type="primary",
                                          use_container_width=True)

    if submitted:
        ok, payload = api.sign_up(
            email=email, password=password, name=name, phone=phone or None,
            address=address or None, city=city_value or None,
            state=state_value or None, pin=pin_value or None,
        )
        if ok:
            st.success("Account created. Signing you in...")
            _sign_in_as(payload["customer"])
        else:
            st.error(payload.get("detail", "Could not create the account."))


def render() -> None:
    styles.inject()

    # Centred, so the form is not stranded against the left edge of a wide page.
    _, middle, _ = st.columns([1, 1.6, 1])
    with middle:
        st.markdown("## Sign in to IndiaShipments")
        st.markdown(
            '<p class="is-muted">Shipments are tied to an account, so we know '
            "who is sending the parcel and can keep your orders together.</p>",
            unsafe_allow_html=True,
        )

        sign_in_tab, sign_up_tab = st.tabs(["Sign in", "Create an account"])
        with sign_in_tab:
            _sign_in_form()
        with sign_up_tab:
            _sign_up_form()

