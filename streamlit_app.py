"""Rapid India -- web application entry point.

Run the backend first:
    uvicorn app.main:app --port 8000
then:
    streamlit run streamlit_app.py
"""

import os

import streamlit as st

# Streamlit Cloud provides secrets through st.secrets rather than the
# environment, while the application reads plain environment variables so that
# it behaves the same locally. Bridge the two before anything is imported that
# reads configuration.
try:  # pragma: no cover - only meaningful on Streamlit Cloud
    for _key, _value in st.secrets.items():
        # Coerced to text, so a port written without quotes still arrives.
        # Whitespace trimmed, because a stray space in a pasted password is
        # indistinguishable from a wrong password in the error it produces.
        if not isinstance(_value, (dict, list)):
            os.environ.setdefault(_key, str(_value).strip())
except Exception:
    pass  # no secrets file locally, which is fine

from ui import api, styles  # noqa: E402
from ui.views import auth, pages, ria  # noqa: E402

st.set_page_config(
    page_title="Rapid India",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

PAGES = {
    "home": ("Home", pages.home),
    "services": ("Services", pages.services),
    "orders": ("My Orders", pages.orders),
    "about": ("About", pages.about),
    "ria": ("Ask RIA", ria.render),
    "signin": ("Sign in", auth.render),
}

# Pages that touch a shipment need an account, so a booking always has an owner.
PRIVATE = {"orders", "ria"}


def _navigation() -> None:
    """The brand sits on the left, the account on the right, links between."""
    current = st.session_state.get("page", "home")
    customer = st.session_state.get("customer")

    links = ["home", "services"]
    if customer:
        links.append("orders")
    links.append("about")

    brand, *link_slots, isa_slot, account_slot = st.columns(
        [2.2] + [1] * len(links) + [1.2, 1.5], vertical_alignment="center"
    )

    with brand:
        st.markdown(
            '<div class="ri-brand">Rapid<span>India</span></div>',
            unsafe_allow_html=True,
        )

    for slot, key in zip(link_slots, links):
        with slot:
            if st.button(
                PAGES[key][0],
                key=f"nav-{key}",
                use_container_width=True,
                type="primary" if key == current else "tertiary",
            ):
                st.session_state["page"] = key
                st.rerun()

    with isa_slot:
        # Tertiary, like the other nav items: a bordered box here sat oddly
        # beside four borderless links. Ask RIA still fills in when it is the
        # page you are on, which is how the current page is marked everywhere.
        if st.button(
            "✨ Ask RIA",
            use_container_width=True,
            type="primary" if current == "ria" else "tertiary",
        ):
            st.session_state["page"] = "ria"
            st.rerun()

    with account_slot:
        if customer:
            with st.popover(f"👤 {customer['name'].split()[0]}",
                            use_container_width=True, type="tertiary"):
                st.markdown(f"**Signed in as {customer['name']}**")
                st.caption(customer["email"])
                if st.button("Sign out", use_container_width=True):
                    for key in ("customer", "session_id", "messages", "state",
                                "options", "greeted"):
                        st.session_state.pop(key, None)
                    st.query_params.clear()
                    st.session_state["page"] = "home"
                    st.rerun()
        elif st.button("Sign in", use_container_width=True, type="tertiary"):
            st.session_state["page"] = "signin"
            st.rerun()

    st.markdown('<div class="ri-navrule"></div>', unsafe_allow_html=True)


def _restore_session() -> None:
    """Bring a signed-in customer back after a browser refresh.

    Streamlit discards its session state on reload, so the conversation id is
    carried in the URL. The conversation and its owner live in the database,
    which makes the id enough to restore who is signed in -- and the draft
    shipment they were part-way through.
    """
    if st.session_state.get("customer"):
        return

    session_id = st.query_params.get("sid")
    if not session_id:
        return

    try:
        customer = api.session_customer(session_id)
    except api.BackendUnavailable:
        return
    if customer is None:
        st.query_params.clear()
        return

    st.session_state["customer"] = customer
    st.session_state["session_id"] = session_id
    page = st.query_params.get("page")
    if page in PAGES:
        st.session_state["page"] = page


def _remember_page(page: str) -> None:
    """Keep the URL in step, so a refresh lands where the user was."""
    if st.session_state.get("customer") and st.query_params.get("page") != page:
        st.query_params["page"] = page


def _backend_warning() -> None:
    """A dead backend is stated plainly rather than surfacing as odd errors."""
    try:
        ok, payload = api.health()
    except api.BackendUnavailable as exc:
        st.error(f"{exc}\n\nStart it with: `uvicorn app.main:app --port 8000`")
        st.stop()
    if not ok:
        st.error(
            "The API is running but its database is not reachable: "
            f"{payload.get('detail', 'unknown error')}"
        )
        st.stop()


def main() -> None:
    styles.inject()
    st.session_state.setdefault("page", "home")

    _backend_warning()
    _restore_session()
    _navigation()

    page = st.session_state["page"]
    if page in PRIVATE and not st.session_state.get("customer"):
        page = "signin"

    _remember_page(page)
    PAGES[page][1]()


if __name__ == "__main__":
    main()
