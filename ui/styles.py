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
  header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
  h1, h2, h3 {{ color: {INK}; letter-spacing: -0.01em; }}

  /* --- brand + navigation --- */
  .is-brand {{
      font-size: 1.4rem; font-weight: 800; color: {BRAND};
      letter-spacing: -0.02em; line-height: 2.4rem;
  }}
  .is-brand span {{ color: {ACCENT}; }}
  .is-navrule {{
      border-bottom: 1px solid {BORDER}; margin: .5rem 0 1.8rem 0;
  }}

  /* --- chat bubbles --- */
  .is-chattitle {{
      font-size: 1.35rem; font-weight: 700; color: {INK}; line-height: 2.2rem;
  }}
  .is-msg {{ display: flex; margin: .3rem 0; }}
  .is-msg.bot {{ justify-content: flex-start; }}
  .is-msg.user {{ justify-content: flex-end; }}
  .is-bubble {{
      max-width: 82%; padding: .55rem .85rem; border-radius: 14px;
      font-size: .93rem; line-height: 1.5; word-wrap: break-word;
  }}
  .is-msg.bot .is-bubble {{
      background: {SURFACE}; color: {INK}; border-bottom-left-radius: 4px;
  }}
  .is-msg.user .is-bubble {{
      background: {BRAND}; color: #fff; border-bottom-right-radius: 4px;
  }}
  .is-bubble.error {{ background: #FBE9E7; color: {DANGER}; }}

  /* --- equal-height card rows ---
     Any column that holds a card is stretched, so a row of cards with
     different amounts of text still lines up. */
  [data-testid="stColumn"]:has(.is-card) {{ display: flex; }}
  [data-testid="stColumn"]:has(.is-card) > div {{
      width: 100%; display: flex; flex-direction: column;
  }}
  [data-testid="stColumn"]:has(.is-card) [data-testid="stVerticalBlock"] {{
      height: 100%;
  }}
  [data-testid="stColumn"]:has(.is-card) [data-testid="stMarkdown"] {{
      flex: 1 1 auto; display: flex;
  }}
  [data-testid="stColumn"]:has(.is-card) [data-testid="stMarkdownContainer"] {{
      width: 100%; display: flex;
  }}
  .is-card {{ display: flex; flex-direction: column; width: 100%; }}

  /* --- hero --- */
  .is-hero {{
      background: linear-gradient(135deg, {BRAND} 0%, {BRAND_DARK} 100%);
      color: #fff; border-radius: 16px; padding: 2.6rem 2.4rem; margin-bottom: 1.6rem;
  }}
  .is-hero h1 {{ color: #fff; margin: 0 0 .6rem 0; font-size: 2.2rem; }}
  .is-hero p {{ color: #DCE7F2; margin: 0; font-size: 1.05rem; max-width: 46rem; }}

  /* --- cards --- */
  .is-card {{
      border: 1px solid {BORDER}; border-radius: 12px; padding: 1.1rem 1.2rem;
      background: #fff; height: 100%;
  }}
  .is-card h4 {{ margin: 0 0 .4rem 0; color: {INK}; font-size: 1rem; }}
  .is-card p {{ margin: 0; color: {MUTED}; font-size: .9rem; line-height: 1.5; }}

  /* --- status chip --- */
  .is-chip {{
      display: inline-block; padding: .18rem .6rem; border-radius: 999px;
      font-size: .78rem; font-weight: 600; white-space: nowrap;
  }}

  /* --- draft panel --- */
  .is-panel {{
      border: 1px solid {BORDER}; border-radius: 12px; background: #fff;
      padding: 1rem 1.1rem;
  }}
  .is-panel h4 {{
      margin: 0 0 .8rem 0; font-size: .82rem; text-transform: uppercase;
      letter-spacing: .06em; color: {MUTED};
  }}
  .is-row {{
      display: flex; justify-content: space-between; gap: 1rem;
      padding: .34rem 0; border-bottom: 1px dashed {BORDER}; font-size: .88rem;
  }}
  .is-row:last-child {{ border-bottom: none; }}
  .is-key {{ color: {MUTED}; }}
  .is-val {{ color: {INK}; font-weight: 600; text-align: right; }}
  .is-val.missing {{ color: #A9B1BD; font-weight: 400; font-style: italic; }}

  /* --- banners --- */
  .is-banner {{
      border-radius: 10px; padding: .8rem 1rem; font-size: .9rem;
      margin-bottom: .9rem; border: 1px solid transparent;
  }}
  .is-banner.blocked {{
      background: #FDF3E3; border-color: #F0D9AE; color: #7A4A00;
  }}
  .is-banner.stub {{
      background: #FFF4CC; border-color: #E8CE72; color: #6B5200;
  }}
  .is-banner.ready {{
      background: #E4F3EA; border-color: #B7DFC7; color: #0E5233;
  }}

  /* --- tracking timeline --- */
  .is-timeline {{ border-left: 2px solid {BORDER}; margin-left: .5rem; padding-left: 1.1rem; }}
  .is-event {{ position: relative; padding: .1rem 0 1.05rem 0; }}
  .is-event:last-child {{ padding-bottom: 0; }}
  .is-dot {{
      position: absolute; left: -1.52rem; top: .34rem; width: .7rem; height: .7rem;
      border-radius: 50%; background: {BORDER}; border: 2px solid #fff;
  }}
  .is-event.current .is-dot {{ background: {BRAND}; box-shadow: 0 0 0 3px #D9E6F2; }}
  .is-event.problem .is-dot {{ background: {DANGER}; box-shadow: 0 0 0 3px #F6DAD7; }}
  .is-event-title {{ font-weight: 600; color: {INK}; font-size: .92rem; }}
  .is-event-meta {{ color: {MUTED}; font-size: .8rem; margin-top: .1rem; }}
  .is-event-note {{ color: {MUTED}; font-size: .86rem; margin-top: .22rem; }}

  /* --- misc --- */
  .is-ref {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700; color: {BRAND};
  }}
  .is-muted {{ color: {MUTED}; font-size: .88rem; }}
  div[data-testid="stChatMessage"] {{ background: transparent; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def chip(status: str) -> str:
    colour, background = STATUS_COLOURS.get(status, (MUTED, SURFACE))
    return (
        f'<span class="is-chip" style="color:{colour};background:{background}">'
        f"{status}</span>"
    )


CHAT_LAYOUT_CSS = """
<style>
  /* The assistant page is sized to fit the window, so the page itself has
     nothing to scroll and only the transcript moves. The main container is
     deliberately left scrollable: forcing overflow:hidden here once put the
     composer out of reach entirely. */
  section[data-testid="stMain"] .block-container {{
      padding-top: 1.6rem !important;
      padding-bottom: 0.4rem !important;
  }}

  /* Everything above the transcript inside its column -- title row, banners --
     plus the composer beneath it, comes to roughly 360px. */
  .st-key-isa-transcript {{
      height: calc(100vh - {offset}px) !important;
      min-height: 220px;
      max-height: 620px;
      border: 1px solid {border}; border-radius: 12px;
      padding: .7rem .9rem; background: #fff;
  }}

  /* Sit the composer directly under the transcript rather than letting it
     stretch across the window. */
  div[data-testid="stChatInput"] {{ margin-top: .5rem; }}
  div[data-testid="stChatInput"] textarea {{ font-size: .93rem; }}
</style>
"""


def _chat_layout_css(offset: int) -> str:
    return CHAT_LAYOUT_CSS.format(border=BORDER, offset=offset)


# Space taken by everything else on the page. Banners and the upload control
# appear conditionally, so the transcript gives up height to make room rather
# than pushing the composer off the bottom of the window.
BASE_CHAT_OFFSET = 380
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
