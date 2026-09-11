"""Ask RIA -- the conversational booking and tracking assistant.

Laid out as a chat widget rather than a document: the frame is a fixed height
so the page itself does not scroll, and only the transcript moves. On the right
sits the shipment exactly as the backend holds it, so the user can always see
what has been captured and what is blocking a booking.
"""

import html
import re
import uuid

import streamlit as st
from streamlit.components import v1 as st_components

from ui import api, components, styles

# Height of the scrolling transcript. Chosen so the composer and the panel's
# actions both stay on screen at a normal laptop height.
CHAT_HEIGHT = 420


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
    """Ask the backend for a reply to a message already on screen."""
    customer = st.session_state.get("customer") or {}
    try:
        # No spinner here: the typing indicator inside the conversation already
        # says the same thing, in the place the user is looking.
        turn = api.send_message(
            st.session_state["session_id"], message, customer.get("id")
        )
    except api.BackendUnavailable as exc:
        st.session_state["messages"].append(
            {"role": "assistant", "content": f"{exc}", "error": True}
        )
        return

    st.session_state["messages"].append({"role": "assistant", "content": turn["reply"]})
    st.session_state["state"] = turn.get("state") or {}
    st.session_state["options"] = turn.get("options") or []


def _greeting() -> None:
    """Open with what this customer can actually do, based on real data."""
    if st.session_state["greeted"] or st.session_state["messages"]:
        return
    st.session_state["greeted"] = True

    customer = st.session_state.get("customer") or {}
    shipments = api.my_shipments(st.session_state["session_id"])

    first_name = (customer.get("name") or "there").split()[0]

    # Every conversation opens by saying who is answering and what they can do.
    # Someone arriving at a chat window should not have to guess either.
    introduction = (
        f"Hi {first_name}, I'm RIA, the Rapid India booking assistant -- I can "
        "book a parcel for you, or tell you where one you have already sent has "
        "got to."
    )

    if shipments:
        latest = shipments[0]
        greeting = (
            f"{introduction} Your most recent shipment, {latest['reference']}, "
            f"is currently {latest['status'].lower()}."
        )
        options = [
            f"Track {latest['reference']} ({latest['status']})",
            "Create a new shipment",
        ]
    else:
        # No buttons here. Nine contents categories under the greeting answer a
        # question nobody has asked yet, and they turned the opening into a form
        # rather than a conversation.
        #
        # And the opener stays general rather than "what would you like to
        # send?": asking that here, where there is no draft to hang the choices
        # on, only to ask it again a turn later with the buttons attached, is
        # the same question twice. RIA asks it once, when it can be answered
        # with a tap.
        greeting = f"{introduction} How can I help today?"
        options = []

    st.session_state["messages"].append({"role": "assistant", "content": greeting})
    st.session_state["options"] = options


def _reset() -> None:
    api.reset_conversation(st.session_state["session_id"])
    for key in ("session_id", "messages", "state", "options", "greeted"):
        st.session_state.pop(key, None)


def _as_bubble_html(text: str) -> str:
    """Escape the message, then restore the little markdown the agent uses."""
    safe = html.escape(text)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    return safe.replace("\n", "<br>")


def _paint(slot, bubbles: list[str]) -> None:
    slot.markdown("".join(bubbles), unsafe_allow_html=True)


def _bubbles() -> list[str]:
    rendered = []
    for message in st.session_state["messages"]:
        side = "user" if message["role"] == "user" else "bot"
        css = "ri-bubble error" if message.get("error") else "ri-bubble"
        rendered.append(
            f'<div class="ri-msg {side}">'
            f'<div class="{css}">{_as_bubble_html(message["content"])}</div>'
            "</div>"
        )
    return rendered


TYPING_BUBBLE = (
    '<div class="ri-msg bot"><div class="ri-bubble ri-typing">'
    "RIA is typing<span>.</span><span>.</span><span>.</span></div></div>"
)


def _transcript() -> tuple[str | None, object, object]:
    """The conversation, in its own scrolling frame.

    Rendered as bubbles rather than Streamlit's chat rows so the user's
    messages can sit on the right, as they do in every other chat interface.

    The option buttons live inside this frame, directly under the question they
    answer. Outside it they added height to the page and pushed the composer
    off the bottom of the window.
    """
    bubbles = _bubbles()

    with st.container(height=CHAT_HEIGHT, border=False, key="ria-transcript"):
        body = st.empty()
        _paint(body, bubbles)
        choices = st.empty()
        with choices.container():
            chosen = _options()
    return chosen, body, choices


def _scroll_to_latest() -> None:
    """Keep the newest message in view.

    A fixed-height container does not follow its own content, so without this
    each reply lands below the fold. Done with a script rather than in CSS: a
    column-reverse frame also reverses the option buttons inside it.
    """
    if not st.session_state.get("messages"):
        return
    st_components.html(
        """
        <script>
          const pin = () => {
            const box = window.parent.document.querySelector(
              '.st-key-ria-transcript'
            );
            if (box) { box.scrollTop = box.scrollHeight; }
          };
          pin();
          setTimeout(pin, 80);
          setTimeout(pin, 250);
        </script>
        """,
        height=0,
    )


def _outstanding_document(state: dict) -> str | None:
    """The document the booking is waiting on, if any."""
    return next(
        (
            b.replace(" required", "")
            for b in (state.get("blockers") or [])
            if b.endswith("required") and "insurance" not in b
        ),
        None,
    )


def _document_upload(state: dict) -> None:
    """Appears only while a document is actually outstanding."""
    document = _outstanding_document(state)
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
            turn = api.upload_document(st.session_state["session_id"], document, upload)
    except api.BackendUnavailable as exc:
        st.error(str(exc))
        return

    st.session_state["messages"].append({"role": "assistant", "content": turn["reply"]})
    st.session_state["state"] = turn.get("state") or {}
    st.session_state["options"] = turn.get("options") or []
    st.rerun()


def _options() -> str | None:
    """Fixed choices as buttons. Free text always remains available."""
    options = st.session_state.get("options") or []
    if not options:
        return None

    chosen = None
    columns = st.columns(min(len(options), 2))
    for index, option in enumerate(options):
        with columns[index % len(columns)]:
            if st.button(
                option,
                key=f"opt-{index}-{len(st.session_state['messages'])}",
                use_container_width=True,
            ):
                chosen = option
    return chosen


def _panel(state: dict) -> None:
    components.draft_panel(state)

    if state.get("reference"):
        st.success(f"Booked. Your reference is {state['reference']}.")
        return

    if state.get("ready_to_book"):
        st.markdown(
            '<div class="ri-banner ready" style="margin-top:.8rem">Everything '
            "checks out. Review the details above, then confirm.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Confirm booking", type="primary", use_container_width=True):
            _send("Yes, please confirm and book this shipment.")
            st.rerun()
        return

    if state.get("has_draft"):
        missing = state.get("still_missing") or []
        if missing:
            st.markdown(
                f'<p class="ri-muted" style="margin-top:.7rem">'
                f"{len(missing)} detail{'s' if len(missing) > 1 else ''} still "
                "needed before this can be booked.</p>",
                unsafe_allow_html=True,
            )
        elif state.get("blockers"):
            st.markdown(
                '<p class="ri-muted" style="margin-top:.7rem">All the details are '
                "in. Clear the item above and the booking can go ahead.</p>",
                unsafe_allow_html=True,
            )


def render() -> None:
    styles.inject()
    _ensure_session()

    if not components.require_sign_in("use RIA"):
        return

    _greeting()
    state = st.session_state.get("state") or {}

    # Tell the stylesheet how much room the conditional pieces need, so the
    # transcript shrinks instead of the page growing a scrollbar.
    styles.inject_chat_layout(
        uploader=bool(_outstanding_document(state)),
    )

    left, right = st.columns([1.6, 1], gap="large")

    with left:
        chosen, body, choices = _transcript()
        _document_upload(state)

        # The composer, with the new-chat icon beside it. Inside the column so
        # it is the width of the conversation, and beside the input so it stays
        # in view instead of scrolling away with the messages.
        composer, reset = st.columns([12, 1], vertical_alignment="center")
        with composer:
            typed = st.chat_input("Type your message, or pick an option above")
        with reset:
            if st.button(
                "✏️",
                help="New chat",
                type="tertiary",
                use_container_width=True,
                key="ria-new-chat",
            ):
                _reset()
                st.rerun()

    with right:
        _panel(state)

    _scroll_to_latest()

    message = chosen or typed
    if message:
        # Take the buttons off the screen, show the message with a typing
        # indicator, and only then wait for the reply.
        choices.empty()
        st.session_state["options"] = []
        st.session_state["messages"].append({"role": "user", "content": message})
        _paint(body, _bubbles() + [TYPING_BUBBLE])

        _send(message)
        st.rerun()
