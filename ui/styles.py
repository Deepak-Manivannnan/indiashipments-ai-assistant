"""Visual language for the application.

One place for the palette and component styling, so pages stay readable and
the product looks like one thing rather than a stack of default widgets.
"""

import streamlit as st

INK = "#1A1D21"
MUTED = "#5B6472"
BRAND = "#0F4C81"
BRAND_DARK = "#0B3A63"
ACCENT = "#E8710A"
SURFACE = "#F4F6FA"
BORDER = "#E3E8EF"
SUCCESS = "#146C43"
WARNING = "#B26B00"
DANGER = "#B3261E"

# Lifecycle status -> (text colour, background). Exceptional states are never
# dressed up as normal ones.
STATUS_COLOURS = {
    "Draft": (MUTED, "#EEF1F6"),
    "Booked": (BRAND, "#E7F0F8"),
    "Picked up": (BRAND, "#E7F0F8"),
    "In transit": (WARNING, "#FDF3E3"),
    "Out for delivery": (WARNING, "#FDF3E3"),
    "Delivered": (SUCCESS, "#E4F3EA"),
    "Delivery failed": (DANGER, "#FBE9E7"),
    "Returned": (DANGER, "#FBE9E7"),
    "Cancelled": (MUTED, "#EEF1F6"),
}

CSS = f"""
<style>
  /* Streamlit's own toolbar overlaps the first row, which was clipping the
     navigation buttons. Push the content below it. */
  .block-container {{ padding-top: 4.2rem; max-width: 1200px; }}
  header[data-testid="stHeader"] {{
      background: transparent; height: 0;
      /* It stays in the layer above the page whatever its height, so let
         clicks pass straight through to whatever is underneath. */
      pointer-events: none;
  }}
  /* ...except its own controls, which must still be usable. */
  header[data-testid="stHeader"] [data-testid="stToolbar"],
  header[data-testid="stHeader"] button,
  header[data-testid="stHeader"] a {{ pointer-events: auto; }}
  h1, h2, h3 {{ color: {INK}; letter-spacing: -0.01em; }}

  /* --- brand + navigation --- */
  .ri-brand {{
      font-size: 1.75rem; font-weight: 800; color: {BRAND};
      letter-spacing: -0.02em; line-height: 2.4rem;
  }}
  .ri-brand span {{ color: {ACCENT}; }}
  .ri-navrule {{
      border-bottom: 1px solid {BORDER}; margin: .5rem 0 1.8rem 0;
  }}

  /* --- chat bubbles --- */
  .ri-msg {{ display: flex; margin: .3rem 0; }}
  .ri-msg.bot {{ justify-content: flex-start; }}
  .ri-msg.user {{ justify-content: flex-end; }}
  .ri-bubble {{
      max-width: 82%; padding: .55rem .85rem; border-radius: 14px;
      font-size: .93rem; line-height: 1.5; word-wrap: break-word;
  }}
  .ri-msg.bot .ri-bubble {{
      background: {SURFACE}; color: {INK}; border-bottom-left-radius: 4px;
  }}
  .ri-msg.user .ri-bubble {{
      background: {BRAND}; color: #fff; border-bottom-right-radius: 4px;
  }}
  .ri-bubble.error {{ background: #FBE9E7; color: {DANGER}; }}
  .ri-bubble.ri-typing {{ color: {MUTED}; font-style: italic; }}
  .ri-bubble.ri-typing span {{ animation: ri-blink 1.2s infinite; }}
  .ri-bubble.ri-typing span:nth-child(2) {{ animation-delay: .2s; }}
  .ri-bubble.ri-typing span:nth-child(3) {{ animation-delay: .4s; }}
  @keyframes ri-blink {{ 0%, 60%, 100% {{ opacity: .25; }} 30% {{ opacity: 1; }} }}

  /* --- equal-height card rows ---
     Any column that holds a card is stretched, so a row of cards with
     different amounts of text still lines up. */
  [data-testid="stColumn"]:has(.ri-card) {{ display: flex; }}
  [data-testid="stColumn"]:has(.ri-card) > div {{
      width: 100%; display: flex; flex-direction: column;
  }}
  [data-testid="stColumn"]:has(.ri-card) [data-testid="stVerticalBlock"] {{
      height: 100%;
  }}
  [data-testid="stColumn"]:has(.ri-card) [data-testid="stMarkdown"] {{
      flex: 1 1 auto; display: flex;
  }}
  [data-testid="stColumn"]:has(.ri-card) [data-testid="stMarkdownContainer"] {{
      width: 100%; display: flex;
  }}
  .ri-card {{ display: flex; flex-direction: column; width: 100%; }}

  /* --- hero --- */
  .ri-hero {{
      background: linear-gradient(135deg, {BRAND} 0%, {BRAND_DARK} 100%);
      color: #fff; border-radius: 16px; padding: 2.6rem 2.4rem; margin-bottom: 1.6rem;
  }}
  .ri-hero h1 {{ color: #fff; margin: 0 0 .6rem 0; font-size: 2.2rem; }}
  .ri-hero p {{ color: #DCE7F2; margin: 0; font-size: 1.05rem; max-width: 46rem; }}

  /* --- cards --- */
  .ri-card {{
      border: 1px solid {BORDER}; border-radius: 12px; padding: 1.1rem 1.2rem;
      background: #fff; height: 100%;
  }}
  .ri-card h4 {{ margin: 0 0 .4rem 0; color: {INK}; font-size: 1rem; }}
  .ri-card p {{ margin: 0; color: {MUTED}; font-size: .9rem; line-height: 1.5; }}

  /* --- status chip --- */
  .ri-chip {{
      display: inline-block; padding: .18rem .6rem; border-radius: 999px;
      font-size: .78rem; font-weight: 600; white-space: nowrap;
  }}

  /* --- draft panel --- */
  .ri-panel {{
      border: 1px solid {BORDER}; border-radius: 12px; background: #fff;
      padding: 1rem 1.1rem;
  }}
  .ri-panel h4 {{
      margin: 0 0 .8rem 0; font-size: .82rem; text-transform: uppercase;
      letter-spacing: .06em; color: {MUTED};
  }}
  .ri-row {{
      display: flex; justify-content: space-between; gap: 1rem;
      padding: .34rem 0; border-bottom: 1px dashed {BORDER}; font-size: .88rem;
  }}
  .ri-row:last-child {{ border-bottom: none; }}
  .ri-key {{ color: {MUTED}; }}
  .ri-val {{ color: {INK}; font-weight: 600; text-align: right; }}
  .ri-val.missing {{ color: #A9B1BD; font-weight: 400; font-style: italic; }}

  /* --- banners --- */
  .ri-banner {{
      border-radius: 10px; padding: .8rem 1rem; font-size: .9rem;
      margin-bottom: .9rem; border: 1px solid transparent;
  }}
  .ri-banner.blocked {{
      background: #FDF3E3; border-color: #F0D9AE; color: #7A4A00;
  }}
  .ri-banner.ready {{
      background: #E4F3EA; border-color: #B7DFC7; color: #0E5233;
  }}

  /* --- tracking timeline --- */
  .ri-timeline {{ border-left: 2px solid {BORDER}; margin-left: .5rem; padding-left: 1.1rem; }}
  .ri-event {{ position: relative; padding: .1rem 0 1.05rem 0; }}
  .ri-event:last-child {{ padding-bottom: 0; }}
  .ri-dot {{
      position: absolute; left: -1.52rem; top: .34rem; width: .7rem; height: .7rem;
      border-radius: 50%; background: {BORDER}; border: 2px solid #fff;
  }}
  .ri-event.current .ri-dot {{ background: {BRAND}; box-shadow: 0 0 0 3px #D9E6F2; }}
  .ri-event.problem .ri-dot {{ background: {DANGER}; box-shadow: 0 0 0 3px #F6DAD7; }}
  .ri-event-title {{ font-weight: 600; color: {INK}; font-size: .92rem; }}
  .ri-event-meta {{ color: {MUTED}; font-size: .8rem; margin-top: .1rem; }}
  .ri-event-note {{ color: {MUTED}; font-size: .86rem; margin-top: .22rem; }}

  /* --- misc --- */
  .ri-ref {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700; color: {BRAND};
  }}
  .ri-muted {{ color: {MUTED}; font-size: .88rem; }}
  div[data-testid="stChatMessage"] {{ background: transparent; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def chip(status: str) -> str:
    colour, background = STATUS_COLOURS.get(status, (MUTED, SURFACE))
    return (
        f'<span class="ri-chip" style="color:{colour};background:{background}">'
        f"{status}</span>"
    )


CHAT_LAYOUT_CSS = """
<style>
  /* The assistant page is sized to fit the window so that the page itself has
     nothing to scroll and only the transcript moves. Everything above the
     conversation is pulled tighter here than on the other pages, to buy the
     height back for the transcript. The main container is deliberately left
     scrollable: forcing overflow:hidden once put the composer out of reach. */
  section[data-testid="stMain"] .block-container {{
      padding-top: 1.9rem !important;
      padding-bottom: 0.3rem !important;
  }}
  .ri-navrule {{ margin: .35rem 0 .8rem 0 !important; }}
  section[data-testid="stMain"] [data-testid="stVerticalBlock"] {{ gap: .5rem; }}

  .st-key-ria-transcript {{
      height: calc(100vh - {offset}px) !important;
      min-height: 200px;
      border: 1px solid {border}; border-radius: 12px;
      padding: .7rem .9rem; background: #fff;
  }}

  .st-key-ria-transcript [data-testid="stButton"] button {{
      font-size: .88rem; padding: .3rem .6rem; border-radius: 10px;
  }}
  div[data-testid="stChatInput"] {{ margin-top: .3rem; }}
  div[data-testid="stChatInput"] textarea {{ font-size: .93rem; }}

  /* The new-chat icon, sitting beside the composer so it is always in view
     rather than scrolling away with the messages. */
  .st-key-ria-new-chat button {{
      font-size: 1.1rem; opacity: .5; transition: opacity .15s ease;
      min-height: 2.6rem; margin-top: .3rem;
  }}
  .st-key-ria-new-chat button:hover {{ opacity: 1; }}
</style>
"""


def _chat_layout_css(offset: int) -> str:
    return CHAT_LAYOUT_CSS.format(border=BORDER, offset=offset)


# Space taken by everything else on the page. Banners and the upload control
# appear conditionally, so the transcript gives up height to make room rather
# than pushing the composer off the bottom of the window.
BASE_CHAT_OFFSET = 490
BANNER_HEIGHT = 62
UPLOADER_HEIGHT = 110


def inject_chat_layout(banners: int = 0, uploader: bool = False) -> None:
    """Extra styling used only by the assistant page."""
    offset = (
        BASE_CHAT_OFFSET
        + banners * BANNER_HEIGHT
        + (UPLOADER_HEIGHT if uploader else 0)
    )
    st.markdown(_chat_layout_css(offset), unsafe_allow_html=True)
