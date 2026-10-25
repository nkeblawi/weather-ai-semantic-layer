import hashlib
import logging
import os
import time
import anthropic
import streamlit as st
import chat_session
import chat_store
import guardrails
from wxcore import charts, db


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# --- Canned replies and error handling (limits live in guardrails.py) ---
CONNECTING_MSG = "Connecting to the weather database server. Please wait..."

# Shown instead of raw exception text, which can leak internals to visitors.
# The raw error is still logged and recorded in chat_history.error.
QUOTA_MSG = (
    "The weather database has reached its daily usage limit. Please try again tomorrow."
)

GENERIC_ERROR_MSG = (
    "Sorry, something went wrong answering that. Please try again in a moment."
)

# Best-effort match for Databricks quota / compute-unavailable errors; the
# exact Free Edition wording isn't documented, so match on common markers.
_QUOTA_MARKERS = ("quota", "resource_exhausted", "usage limit", "limit exceeded")


CLEAR_COMMAND = "/clear"
HELP_COMMAND = "/help"
PLOT_COMMAND = chat_session.PLOT_COMMAND

WELCOME_MSG = (
    "Welcome to the Weather Observations Chat! I answer questions about past weather observations "
    "(temperatures, rain, snow, and wind) at major cities in the US and Canada. Just ask a question, "
    "or type `/help` for example questions you could ask."
)

# Example questions for the /help command.
HELP_EXAMPLES = [
    "What was the hottest day in Chicago this past summer?",
    "How much snow did Dulles Airport get in its last snowstorm over 6 inches?",
    "How many days did it rain in Miami in 2025?",
]
PLOT_EXAMPLES = [
    "/plot average high and low temperature for each month at Chicago",
    "/plot monthly temperatures at New York City in 2025 against normal",
    "/plot the number of 90-degree days per year at Washington DC",
]


# Chat avatars: an emoji on a colored tile (CSS below sets the tile colors).
AVATARS = {"user": "🧑", "assistant": "⛅"}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _start_saving():
    # on_click runs before the rerun, so the button already renders
    # disabled in the run that performs the save
    st.session_state.prefs_saving = True


def _plot_it(turn: dict):
    # on_click runs before the rerun: queue the same question as /plot, once
    turn["plot_it_used"] = True
    st.session_state.pending_question = f"{PLOT_COMMAND} {turn['question']}"


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def friendly_error(error_text: str) -> str:
    lowered = error_text.lower()
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return QUOTA_MSG
    return GENERIC_ERROR_MSG


def get_conn():
    """
    One connection per browser session (a shared connection isn't safe
    across Streamlit's per-session threads). Reopened if it has closed,
    e.g. after the warehouse ends an idle session.
    """
    conn = st.session_state.get("conn")
    if conn is None or not conn.open:
        conn = db.get_databricks_connection()
        st.session_state.conn = conn
        if "session" in st.session_state:
            st.session_state.session.conn = conn
    return conn


# The Anthropic client is thread-safe, so one per process is fine.
@st.cache_resource(show_spinner=False)
def get_client():
    return anthropic.Anthropic()


# _conn: leading underscore tells st.cache_data not to hash the connection.
# Refreshed hourly so newly activated stations show up without a restart.
@st.cache_data(ttl=3600, show_spinner=False)
def get_home_locations(_conn):
    return db.list_home_locations(_conn)


def render_help():
    examples = "\n".join(f"- {q}" for q in HELP_EXAMPLES)
    st.markdown(
        "Ask about past weather for any of the cities in the **Home city** list. "
        f"Some examples to try:\n\n{examples}\n\n"
        f"**Charts:** You can also create charts and graphs! Just start a question with `{PLOT_COMMAND}`. "
        "You can plot daily values (up to a year or less), monthly, yearly, or seasonal values, counts of days "
        "(e.g. 90-degree days), climatologies, comparisons with the 1991-2020 normals, and trend lines:\n\n"
        + "\n".join(f"- {q}" for q in PLOT_EXAMPLES)
        + "\n\n**Tip:** open the settings bar at the top, pick a **Home city**, and press **Update** to omit the city "
        "from your questions. You can also switch the temperature **Units** between °F and °C there.\n\n"
        + f"`{HELP_COMMAND}` doesn't count toward your question limit."
    )


def chat_message(role: str):
    return st.chat_message(role, avatar=AVATARS[role])


def render_help_cards_at(position):
    """Help cards requested when the transcript had `position` turns."""
    for _ in range(st.session_state.get("help_after", []).count(position)):
        with chat_message("assistant"):
            render_help()


def render_preferences(session, locations):
    """
    Header: home city + units, saved only when Update is pressed. Collapsed
    to a one-line summary until opened; saving a change updates the summary,
    and the new label closes it again.
    """
    # Options are station codes (what gets saved); format_func shows the name.
    options = list(locations)
    current = session.home_location
    # Keep a stored value that isn't a known code (e.g. free text seeded from
    # WX_APP_HOME_LOCATION) visible instead of silently dropping it.
    if current and current not in options:
        options.insert(0, current)

    city = locations.get(current, current) if current else "not set"
    summary = f"Home city: {city} · °{session.temp_unit or 'F'}"
    # st.form: widget changes don't rerun the app until Update is pressed
    with st.expander(summary, icon=":material/settings:"), st.form(
        "preferences", border=False
    ):
        loc_col, unit_col, btn_col = st.columns([3, 2, 1], vertical_alignment="bottom")
        loc_col.selectbox(
            "Home city",
            options,
            index=options.index(current) if current else None,
            format_func=lambda code: locations.get(code, code),
            placeholder="Select a city",
            key="pref_home",
        )
        unit_col.radio(
            "Units",
            ["F", "C"],
            index=0 if session.temp_unit != "C" else 1,
            format_func=lambda u: f"°{u}",
            horizontal=True,
            key="pref_unit",
        )
        btn_col.form_submit_button(
            "Update",
            type="primary",
            width="stretch",
            on_click=_start_saving,
            disabled=st.session_state.get("prefs_saving", False),
        )

    # Result of the save from the previous run (toasts don't survive st.rerun)
    if flash := st.session_state.pop("prefs_flash", None):
        kind, msg = flash
        # if/else statement, not a conditional expression: Streamlit "magic"
        # would render the expression's return value onto the page
        if kind == "error":
            st.error(msg)
        else:
            st.toast(msg)


def save_preferences(session):
    """
    Runs at the end of the script, after the page (with the disabled
    button) has rendered; then reruns so the button re-enables.
    """
    home, unit = st.session_state.pref_home, st.session_state.pref_unit
    if (home, unit) == (session.home_location, session.temp_unit):
        flash = ("toast", "No changes to save.")
    else:
        try:
            chat_session.update_preferences(session, temp_unit=unit, home_location=home)
        except Exception as exc:  # noqa: BLE001 -- show it, keep the chat usable
            flash = ("error", f"Couldn't save preferences: {exc}")
        else:
            flash = ("toast", "Preferences updated.")
    st.session_state.prefs_flash = flash
    st.session_state.prefs_saving = False
    st.rerun()


@st.cache_resource(show_spinner=False)
def get_rate_limits():
    return guardrails.RateLimits()


def client_ip() -> str:
    return st.context.ip_address or "local"


def ip_tag(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:10]


def daily_limit_reached(conn) -> bool:
    # Fails open: a counting hiccup shouldn't block a legitimate question
    # (if the database is really down, answering fails anyway).
    try:
        return (
            chat_store.count_resolved_turns_today(conn)
            >= guardrails.DAILY_QUESTION_LIMIT
        )
    except Exception:  # noqa: BLE001
        logger.warning("daily question count failed", exc_info=True)
        return False


def render_answer(turn: dict, index: int):
    answer = turn["answer"]
    if answer.startswith("[error]"):
        st.error(friendly_error(answer))
        return
    st.write(answer)
    if turn.get("needs_plot") and not turn.get("plot_it_used"):
        st.button(
            "Plot it",
            key=f"plot_it_{index}",
            type="primary",
            on_click=_plot_it,
            args=(turn,),
        )
    # a ranking drawn as a chart (/plot) keeps its table, collapsed under it
    charted = bool(turn.get("charts"))
    for table in turn.get("tables") or []:
        if charted:
            continue
        st.markdown(f"**{table['title']}**")
        st.dataframe(table["rows"], hide_index=True)
        if table["note"]:
            st.caption(table["note"])
    for j, chart in enumerate(turn.get("charts") or []):
        st.vega_lite_chart(charts.chart_spec(chart), width="stretch")
        rows = chart.get("table") or (
            turn["tables"][j]["rows"]
            if chart["kind"] == "ranking" and turn.get("tables")
            else None
        )
        if rows:
            # collapsed: the chart is the answer, the numbers are on request
            with st.expander("Show table", icon=":material/table:"):
                st.dataframe(rows, hide_index=True)
        for note in chart["notes"]:
            st.caption(note)
        if sum(len(s["points"]) for s in chart["series"]) > 24:
            st.download_button(
                "Download data (CSV)",
                charts.to_csv(chart),
                file_name="weather_chart.csv",
                mime="text/csv",
                key=f"csv_{index}_{j}",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Weather Observations Chatbot", page_icon="🌤️")


# - Preferences header: sticky just under Streamlit's 3.75rem top bar. Set on
#   the form's wrapper, whose parent is the main column that grows with the
#   chat (sticky only works within its parent's height). It needs an opaque
#   background so scrolled messages don't show through: only .stApp carries
#   the theme's, and the elements between it and the header are transparent,
#   so each of them inherits it -- the header then follows a theme switch
#   live (st.context.theme follows the browser, not the app's theme menu).
# - Update and Plot it buttons: sky blue when clickable. Only the enabled
#   state is restyled, so Streamlit's own gray disabled style shows while saving.
# - Chat: user messages on the right (avatar flipped to the right, bubble
#   shrunk to its text), answers stay left -- messaging-app layout. Emoji
#   avatars carry no role test id, so rows are matched by the content's
#   aria-label; the avatar tile is the row's first child.
# st.html (not st.markdown): a style-only block takes no layout space.
st.html(f"""
    <style>
    [data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"]
        > [data-testid="stLayoutWrapper"]:has(> .st-key-prefs_header) {{
        position: sticky;
        top: 3.75rem;
        z-index: 99;
        padding-bottom: 0.5rem;
    }}
    [data-testid="stApp"] :has(.st-key-prefs_header) {{
        background-color: inherit;
    }}
    [data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) {{
        flex-direction: row-reverse;
        width: fit-content;
        max-width: 85%;
        margin-left: auto;
    }}
    [data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) > div:first-child {{
        /* pale enough that the yellow emoji face still stands out */
        background-color: #FFF4B8;
        border-color: #FFF4B8;
    }}
    [data-testid="stChatMessage"]:has(> [aria-label="Chat message from assistant"]) > div:first-child {{
        background-color: #87CEEB;
        border-color: #87CEEB;
    }}
    [data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled),
    [data-testid="stBaseButton-primary"]:not(:disabled) {{
        background-color: #87CEEB;
        border-color: #87CEEB;
        color: #0E1117;
    }}
    [data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled):hover,
    [data-testid="stBaseButton-primary"]:not(:disabled):hover {{
        background-color: #5DB8DE;
        border-color: #5DB8DE;
        color: #0E1117;
    }}
    </style>
    """)


# Page order: header, welcome, then the connecting spinner. The header needs
# the station list, so reserve its slot at the top now and fill it once
# connected; the welcome shows immediately, with the spinner under it.
header_slot = st.container(key="prefs_header")
with chat_message("assistant"):
    st.markdown(WELCOME_MSG)

# Usage caps, checked once per session.
if "admitted" not in st.session_state:
    blocked = get_rate_limits().new_session(client_ip(), time.monotonic())
    if blocked:
        logger.info("session blocked (%s) ip=%s", blocked.reason, ip_tag(client_ip()))
        st.info(blocked.reply)
        st.stop()
    st.session_state.admitted = True

# One spinner over both DB steps (connect + create_session) so the message
# stays up without a blank gap until the chat input renders.
try:
    with st.spinner(CONNECTING_MSG):
        conn, client = get_conn(), get_client()
        if "session" not in st.session_state:
            st.session_state.session = chat_session.create_session(
                conn,
                client,
                # WX_APP_USER for local dev (same as the CLI); otherwise each
                # visitor is anonymous for this session, with no users row
                os.environ.get("WX_APP_USER") or chat_store.new_anonymous_username(),
                email=os.environ.get("WX_APP_EMAIL"),
                display_name=os.environ.get("WX_APP_NAME"),
                temp_unit=os.environ.get("WX_APP_TEMP_UNIT"),
                home_location=os.environ.get("WX_APP_HOME_LOCATION"),
            )
        session = st.session_state.session
        locations = get_home_locations(conn)

        limit = guardrails.session_limit(session)
        # created inside the spinner so the input is on screen before the
        # message clears (no blank frame between them)
        with st.bottom:
            question = st.chat_input(
                limit.placeholder if limit else "Ask a weather question...",
                max_chars=guardrails.MAX_QUESTION_CHARS,
                disabled=limit is not None,
            )
            # trailing double space = markdown line break inside one caption
            st.caption(
                f"Type {CLEAR_COMMAND} to reset the chat context · "
                f"{guardrails.questions_remaining(session)} of "
                f"{guardrails.SESSION_QUESTION_LIMIT} questions left  \n"
                f"Type {HELP_COMMAND} for example questions and tips  \n"
                f"Type {PLOT_COMMAND} with a question for a chart"
            )
except (
    Exception
) as exc:  # noqa: BLE001 -- startup failure: explain, don't dump a traceback
    logger.exception("startup failed")
    st.error(friendly_error(str(exc)))
    st.stop()

with header_slot:
    render_preferences(session, locations)

# Help cards live outside session.history (which is replayed to the model as
# conversation context) as the history lengths they were shown at, so each
# card stays in place in the transcript across reruns.
help_after = st.session_state.setdefault("help_after", [])
# A Plot it click queued its question for this run (the input stays empty)
if pending := st.session_state.pop("pending_question", None):
    question = pending
command = question.strip().lower() if question else None

# /clear: wipe the on-screen chat and the context sent to the model. Turns
# already saved in chat_history are left alone.
if command == CLEAR_COMMAND:
    session.history.clear()
    session.station_info.clear()
    help_after.clear()
    st.toast("Chat cleared.")
    question = None
# /help: never reaches get_answer, so it isn't recorded or counted
elif command == HELP_COMMAND:
    help_after.append(len(session.history))
    question = None

for i, turn in enumerate(session.history):
    render_help_cards_at(i)
    with chat_message("user"):
        st.write(turn["question"])
    with chat_message("assistant"):
        render_answer(turn, i)
render_help_cards_at(len(session.history))

if question:
    with chat_message("user"):
        st.write(question)
    with chat_message("assistant"):
        # Re-checked here: the input may have been rendered enabled just
        # before the last allowed question was answered.
        if limit := guardrails.session_limit(session):
            st.info(limit.message)
        else:
            with st.spinner("Checking data..."):
                answer = chat_session.get_answer(
                    session,
                    question,
                    over_daily_limit=lambda: daily_limit_reached(conn),
                    rate_limited=lambda: get_rate_limits().question(
                        client_ip(), time.monotonic()
                    ),
                )
            if answer.startswith("[error]"):
                logger.warning("answer failed: %s", answer)
            # Rerun so the input/caption reflect the new remaining count
            # (rendered at the top of this run, before this answer). The
            # answer is in session.history, so the rerun redraws it. Limit
            # messages above aren't in history, so those runs don't rerun.
            st.rerun()

# Ended by off-topic messages: say so under the transcript, since the
# input is now disabled
if (limit := guardrails.session_limit(session)) and limit.reason == "ended":
    with chat_message("assistant"):
        st.info(limit.message)

if st.session_state.get("prefs_saving"):
    save_preferences(session)
