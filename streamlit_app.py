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

# Home, Services and About are public. Everything that touches a shipment
# requires an account, so a booking always has an owner.
PUBLIC_PAGES = {
    "home": ("Home", pages.home),
    "services": ("Services", pages.services),
    "about": ("About", pages.about),
}
PRIVATE_PAGES = {
    "track": ("Track", pages.track),
    "orders": ("My Orders", pages.orders),
}
ALL_PAGES = {**PUBLIC_PAGES, **PRIVATE_PAGES, "isa": ("Ask ISA", isa.render),
             "signin": ("Sign in", auth.render)}


def _navigation() -> None:
    """Top navigation. Ask ISA sits on the right, where an assistant belongs."""
    current = st.session_state.get("page", "home")
    signed_in = bool(st.session_state.get("customer"))

    links = [
        ("home", PUBLIC_PAGES["home"]),
        ("services", PUBLIC_PAGES["services"]),
        ("track", PRIVATE_PAGES["track"]),
    ]
    if signed_in:
        links.append(("orders", PRIVATE_PAGES["orders"]))
    links.append(("about", PUBLIC_PAGES["about"]))

    columns = st.columns([1] * len(links) + [0.4, 1.3, 1])
    for column, (key, (label, _)) in zip(columns, links):
        with column:
            if st.button(
                label,
                key=f"nav-{key}",
                use_container_width=True,
                type="secondary" if key != current else "primary",
            ):
                st.session_state["page"] = key
                st.rerun()

    with columns[-2]:
        if st.button("✨ Ask ISA", use_container_width=True,
                     type="primary" if current == "isa" else "secondary"):
            st.session_state["page"] = "isa"
            st.rerun()

    with columns[-1]:
        if signed_in:
            if st.button("Sign out", use_container_width=True):
                for key in ("customer", "session_id", "messages", "state",
                            "options", "greeted"):
                    st.session_state.pop(key, None)
                st.session_state["page"] = "home"
                st.rerun()
        elif st.button("Sign in", use_container_width=True):
            st.session_state["page"] = "signin"
            st.rerun()


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
    if page in PRIVATE_PAGES and not st.session_state.get("customer"):
        # Deep link into a private page while signed out: show sign-in instead,
        # rather than an empty page or an error.
        page = "signin"

    ALL_PAGES[page][1]()


if __name__ == "__main__":
    main()
