"""Ask ISA -- the conversational booking and tracking assistant.

Layout is deliberate: the conversation on the left, and on the right the
shipment as the backend actually holds it. The user can always see what has
been captured, what is still missing, and what is blocking a booking, so the
agent's questions never feel arbitrary.
"""

import uuid

import streamlit as st

from ui import api, components, styles


def _new_session() -> str:
    """A fresh conversation per sign-in. The draft outlives it in the database."""
    return f"ui-{uuid.uuid4().hex[:16]}"


def _ensure_session() -> None:
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = _new_session()
        customer = st.session_state.get("customer")
        if customer:
            # Bind now, so the greeting can name this customer's shipments
            # rather than waiting for their first message.
            api.bind_session(st.session_state["session_id"], customer["id"])
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("state", {})
    st.session_state.setdefault("options", [])
    st.session_state.setdefault("greeted", False)


def _send(message: str) -> None:
    """One turn: show what the user said, then what the backend replied."""
    customer = st.session_state.get("customer") or {}
    st.session_state["messages"].append({"role": "user", "content": message})
    try:
        with st.spinner("ISA is working on that..."):
            turn = api.send_message(
                st.session_state["session_id"], message, customer.get("id")
            )
    except api.BackendUnavailable as exc:
        st.session_state["messages"].append(
            {"role": "assistant", "content": f"{exc}", "error": True}
        )
        return

    st.session_state["messages"].append(
        {"role": "assistant", "content": turn["reply"]}
    )
    st.session_state["state"] = turn.get("state") or {}
    st.session_state["options"] = turn.get("options") or []


def _greeting() -> None:
    """Open with what this customer can actually do, based on real data."""
    if st.session_state["greeted"] or st.session_state["messages"]:
        return
    st.session_state["greeted"] = True

    customer = st.session_state.get("customer") or {}
    shipments = api.my_shipments(st.session_state["session_id"])
    st.session_state["existing"] = shipments

    first_name = (customer.get("name") or "there").split()[0]
    if shipments:
        latest = shipments[0]
        greeting = (
            f"Hi {first_name}, how can I help today? I can start a new "
            f"shipment, or check on {latest['reference']}, which is currently "
            f"{latest['status'].lower()}."
        )
        options = [
            f"Track {latest['reference']} ({latest['status']})",
            "Create a new shipment",
        ]
    else:
        greeting = (
            f"Hi {first_name}, how can I help today? Tell me what you'd like to "
            "send and I'll take it from there."
        )
        options = ["Create a new shipment"]

    st.session_state["messages"].append(
        {"role": "assistant", "content": greeting}
    )
    st.session_state["options"] = options


def _banners(state: dict) -> None:
    if state.get("stub_mode"):
        st.markdown(
            '<div class="is-banner stub"><b>Stub mode</b> &mdash; replies are '
            "scripted and no AI model is being called. The rules, database and "
            "booking guardrails are still real.</div>",
            unsafe_allow_html=True,
        )

    for blocker in state.get("blockers") or []:
        st.markdown(
            f'<div class="is-banner blocked"><b>Booking paused</b> &mdash; '
            f"{blocker}.</div>",
            unsafe_allow_html=True,
        )


def _document_upload(state: dict) -> None:
    """Appears only while a document is actually outstanding."""
    blockers = state.get("blockers") or []
    document = next(
        (b.replace(" required", "") for b in blockers if b.endswith("required")
         and "insurance" not in b),
        None,
    )
    if not document:
        return

    upload = st.file_uploader(
        f"Attach the {document}",
        type=["pdf", "png", "jpg", "jpeg"],
        key=f"upload-{document}-{len(st.session_state['messages'])}",
    )
    if upload is None:
        return

    st.session_state["messages"].append(
        {"role": "user", "content": f"[attached {upload.name}]"}
    )
    try:
        with st.spinner("Recording the document..."):
            turn = api.upload_document(
                st.session_state["session_id"], document, upload
            )
    except api.BackendUnavailable as exc:
        st.error(str(exc))
        return

    st.session_state["messages"].append(
        {"role": "assistant", "content": turn["reply"]}
    )
    st.session_state["state"] = turn.get("state") or {}
    st.session_state["options"] = turn.get("options") or []
    st.rerun()


def _conversation() -> None:
    """The transcript, in its own scrolling area so the page stays put."""
    with st.container(height=460, border=False):
        for message in st.session_state["messages"]:
            avatar = "🧑" if message["role"] == "user" else "📦"
            with st.chat_message(message["role"], avatar=avatar):
                if message.get("error"):
                    st.error(message["content"])
                else:
                    st.markdown(message["content"])


def _options() -> str | None:
    """Fixed choices as buttons. Free text always remains available."""
    options = st.session_state.get("options") or []
    if not options:
        return None

    chosen = None
    columns = st.columns(min(len(options), 4))
    for index, option in enumerate(options):
        with columns[index % len(columns)]:
            if st.button(option, key=f"opt-{index}-{len(st.session_state['messages'])}",
                         use_container_width=True):
                chosen = option
    return chosen


def render() -> None:
    styles.inject()
    _ensure_session()

    if not components.require_sign_in("use ISA"):
        return

    _greeting()

    left, right = st.columns([1.55, 1], gap="large")

    with right:
        state = st.session_state.get("state") or {}
        components.draft_panel(state)

        if state.get("ready_to_book"):
            st.markdown(
                '<div class="is-banner ready" style="margin-top:.8rem">Everything '
                "checks out. Review the details above, then confirm.</div>",
                unsafe_allow_html=True,
            )
            if st.button("Confirm booking", type="primary", use_container_width=True):
                _send("Yes, please confirm and book this shipment.")
                st.rerun()
        elif state.get("has_draft"):
            missing = state.get("still_missing") or []
            if missing:
                st.markdown(
                    f'<p class="is-muted" style="margin-top:.7rem">'
                    f"{len(missing)} detail{'s' if len(missing) > 1 else ''} still "
                    "needed before this can be booked.</p>",
                    unsafe_allow_html=True,
                )

        if state.get("reference"):
            st.success(f"Booked. Your reference is {state['reference']}.")

        st.divider()
        if st.button("Start a new conversation", use_container_width=True):
            api.reset_conversation(st.session_state["session_id"])
            for key in ("session_id", "messages", "state", "options", "greeted"):
                st.session_state.pop(key, None)
            st.rerun()

    with left:
        _banners(st.session_state.get("state") or {})
        _conversation()
        _document_upload(st.session_state.get("state") or {})

        chosen = _options()
        typed = st.chat_input("Type your message, or pick an option above")

        message = chosen or typed
        if message:
            st.session_state["options"] = []
            _send(message)
            st.rerun()
