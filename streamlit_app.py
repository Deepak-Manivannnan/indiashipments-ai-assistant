"""IndiaShipments -- web application entry point.

Run the backend first:
    uvicorn app.main:app --port 8000
then:
    streamlit run streamlit_app.py
"""

import streamlit as st

from ui import api, styles
from ui.views import auth, isa, pages

st.set_page_config(
    page_title="IndiaShipments",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

PAGES = {
    "home": ("Home", pages.home),
    "services": ("Services", pages.services),
    "orders": ("My Orders", pages.orders),
    "about": ("About", pages.about),
    "isa": ("Ask ISA", isa.render),
    "signin": ("Sign in", auth.render),
}

# Pages that touch a shipment need an account, so a booking always has an owner.
PRIVATE = {"orders", "isa"}


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
            '<div class="is-brand">India<span>Shipments</span></div>',
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
        if st.button(
            "✨ Ask ISA",
            use_container_width=True,
            type="primary" if current == "isa" else "secondary",
        ):
            st.session_state["page"] = "isa"
            st.rerun()

    with account_slot:
        if customer:
            with st.popover(f"👤 {customer['name'].split()[0]}",
                            use_container_width=True):
                st.markdown(f"**Signed in as {customer['name']}**")
                st.caption(customer["email"])
                if st.button("Sign out", use_container_width=True):
                    for key in ("customer", "session_id", "messages", "state",
                                "options", "greeted"):
                        st.session_state.pop(key, None)
                    st.session_state["page"] = "home"
                    st.rerun()
        elif st.button("Sign in", use_container_width=True, type="secondary"):
            st.session_state["page"] = "signin"
            st.rerun()

    st.markdown('<div class="is-navrule"></div>', unsafe_allow_html=True)


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
    _navigation()

    page = st.session_state["page"]
    if page in PRIVATE and not st.session_state.get("customer"):
        page = "signin"

    PAGES[page][1]()


if __name__ == "__main__":
    main()
