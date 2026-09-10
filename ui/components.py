"""Reusable pieces of the interface."""

from datetime import datetime

import streamlit as st

from ui import api, styles

PROBLEM_STATUSES = {"Delivery failed", "Returned", "Cancelled"}


def header() -> None:
    customer = st.session_state.get("customer")
    who = f"Signed in as {customer['name']}" if customer else "Not signed in"
    st.markdown(
        f"""
        <div class="is-header">
          <div class="is-logo">India<span>Shipments</span></div>
          <div class="is-who">{who}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return value


def timeline(events: list[dict], status: str) -> None:
    """Tracking history as a vertical timeline, newest last.

    A failed or returned delivery is marked in red rather than being rendered
    like any other milestone.
    """
    if not events:
        st.markdown(
            '<p class="is-muted">No tracking events have been recorded yet.</p>',
            unsafe_allow_html=True,
        )
        return

    rows = []
    for index, event in enumerate(events):
        is_last = index == len(events) - 1
        css = "is-event"
        if is_last:
            css += " problem" if event["status"] in PROBLEM_STATUSES else " current"
        rows.append(
            f'<div class="{css}"><div class="is-dot"></div>'
            f'<div class="is-event-title">{event["status"]}</div>'
            f'<div class="is-event-meta">{_format_time(event.get("event_time"))}'
            + (f' &middot; {event["location"]}' if event.get("location") else "")
            + "</div>"
            + (
                f'<div class="is-event-note">{event["note"]}</div>'
                if event.get("note")
                else ""
            )
            + "</div>"
        )
    st.markdown(
        f'<div class="is-timeline">{"".join(rows)}</div>', unsafe_allow_html=True
    )


def _row(label: str, value) -> str:
    if value in (None, "", []):
        return (
            f'<div class="is-row"><span class="is-key">{label}</span>'
            f'<span class="is-val missing">not given yet</span></div>'
        )
    return (
        f'<div class="is-row"><span class="is-key">{label}</span>'
        f'<span class="is-val">{value}</span></div>'
    )


def _person(block: dict) -> str:
    name = block.get("name")
    place = ", ".join(filter(None, [block.get("city"), block.get("pin")]))
    if not name and not place:
        return ""
    return " &middot; ".join(filter(None, [name, place]))


def draft_panel(state: dict) -> None:
    """The live shipment, filled in as the conversation progresses.

    Read straight from the backend's `get_summary`, so what is on screen is
    what is actually stored -- not what the model said it stored.
    """
    if not state.get("has_draft"):
        st.markdown(
            '<div class="is-panel"><h4>This shipment</h4>'
            '<p class="is-muted">Nothing yet. Tell ISA what you would like to '
            "send and the details will appear here as you go.</p></div>",
            unsafe_allow_html=True,
        )
        return

    draft = state.get("draft") or {}
    package = draft.get("package") or {}
    weight = package.get("weight_g")
    dimensions = [package.get(k) for k in ("length_mm", "width_mm", "height_mm")]
    value = draft.get("declared_value")

    rows = [
        _row("From", _person(draft.get("sender") or {})),
        _row("To", _person(draft.get("recipient") or {})),
        _row("Contents", draft.get("contents")),
        _row("Weight", f"{weight / 1000:g} kg" if weight else None),
        _row(
            "Size",
            " x ".join(f"{d / 10:g}" for d in dimensions) + " cm"
            if all(dimensions)
            else None,
        ),
        _row("Service", draft.get("service_type")),
        _row("Declared value", f"Rs {float(value):,.0f}" if value else None),
    ]

    st.markdown(
        f'<div class="is-panel"><h4>This shipment</h4>{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )


def shipment_card(shipment: dict, *, expandable: bool = True) -> None:
    """One row on the orders page, with its history a click away."""
    reference = shipment["reference"]
    top = st.container()
    with top:
        left, right = st.columns([3, 1])
        with left:
            st.markdown(
                f'<span class="is-ref">{reference}</span>'
                f'<span class="is-muted"> &middot; to '
                f'{shipment.get("to_city") or "unknown"}'
                f' &middot; {shipment.get("contents") or ""}</span>',
                unsafe_allow_html=True,
            )
        with right:
            st.markdown(styles.chip(shipment["status"]), unsafe_allow_html=True)

    if not expandable:
        return

    with st.expander("Tracking history"):
        ok, payload = api.tracking(reference)
        if not ok:
            st.error(payload.get("detail", "Tracking is unavailable."))
            return
        st.markdown(f'<p class="is-muted">{payload["explanation"]}</p>',
                    unsafe_allow_html=True)
        timeline(payload["events"], payload["status"])


def require_sign_in(feature: str) -> bool:
    """Gate a page behind sign-in, explaining why rather than just refusing."""
    if st.session_state.get("customer"):
        return True
    st.info(
        f"Please sign in to {feature}. Your shipments are tied to your account, "
        "so we know who is sending the parcel."
    )
    if st.button("Go to sign in", type="primary"):
        st.session_state["page"] = "signin"
        st.rerun()
    return False
