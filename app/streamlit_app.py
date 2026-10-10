import os
import anthropic
import streamlit as st
import pipeline
import chat_session

st.set_page_config(page_title="Weather Q&A", page_icon="🌤️")

CONNECTING_MSG = "Connecting to the weather database server..."


# show_spinner=False: replaces the default "Running get_conn()." with
# CONNECTING_MSG below
@st.cache_resource(show_spinner=False)
def get_conn():
    return pipeline.get_databricks_connection()


@st.cache_resource(show_spinner=False)
def get_client():
    return anthropic.Anthropic()


# _conn: leading underscore tells st.cache_data not to hash the connection.
# Refreshed hourly so newly activated stations show up without a restart.
@st.cache_data(ttl=3600, show_spinner=False)
def get_home_locations(_conn):
    return pipeline.list_home_locations(_conn)


CLEAR_COMMAND = "/clear"

# Sticky header needs an opaque background so scrolled messages don't show
# through; Streamlit exposes no CSS variable for it, so match the theme.
PAGE_BG = "#FFFFFF" if st.context.theme.type == "light" else "#0E1117"

# - Preferences header: sticky just under Streamlit's 3.75rem top bar. Set on
#   the form's wrapper, whose parent is the main column that grows with the
#   chat (sticky only works within its parent's height).
# - Update button: sky blue when clickable. Only the enabled state is
#   restyled, so Streamlit's own gray disabled style shows while saving.
# - Chat: user messages on the right (avatar flipped to the right, bubble
#   shrunk to its text), answers stay left -- messaging-app layout.
# st.html (not st.markdown): a style-only block takes no layout space.
st.html(
    f"""
    <style>
    [data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"]
        > [data-testid="stLayoutWrapper"]:has(> [data-testid="stForm"]) {{
        position: sticky;
        top: 3.75rem;
        z-index: 99;
        background-color: {PAGE_BG};
        padding-bottom: 0.5rem;
    }}
    [data-testid="stChatMessage"]:has(> [data-testid="stChatMessageAvatarUser"]) {{
        flex-direction: row-reverse;
        width: fit-content;
        max-width: 85%;
        margin-left: auto;
    }}
    [data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled) {{
        background-color: #87CEEB;
        border-color: #87CEEB;
        color: #0E1117;
    }}
    [data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled):hover {{
        background-color: #5DB8DE;
        border-color: #5DB8DE;
        color: #0E1117;
    }}
    </style>
    """
)


def _start_saving():
    # on_click runs before the rerun, so the button already renders
    # disabled in the run that performs the save
    st.session_state.prefs_saving = True


def render_preferences(session, locations):
    """Header: home location + units, saved only when Update is pressed."""
    options = list(locations)
    # Keep a stored value that isn't a station city (e.g. seeded free text
    # from WX_APP_HOME_LOCATION) visible instead of silently dropping it.
    if session.home_location and session.home_location not in options:
        options.insert(0, session.home_location)

    # st.form: widget changes don't rerun the app until Update is pressed
    with st.form("preferences"):
        loc_col, unit_col, btn_col = st.columns([3, 2, 1], vertical_alignment="bottom")
        loc_col.selectbox(
            "Home location",
            options,
            index=options.index(session.home_location) if session.home_location else None,
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


if "session" not in st.session_state:
    # same WX_APP_USER fallback the CLI uses; else a simple sign-in form
    username = os.environ.get("WX_APP_USER")
    if not username:
        username = st.text_input("Sign in with your username (email or handle)")
        if not username:
            st.stop()

# One spinner over both DB steps (connect + create_session) so the message
# stays up without a blank gap until the chat input renders.
with st.spinner(CONNECTING_MSG):
    conn, client = get_conn(), get_client()
    if "session" not in st.session_state:
        st.session_state.session = chat_session.create_session(
            conn,
            client,
            username,
            email=os.environ.get("WX_APP_EMAIL"),
            display_name=os.environ.get("WX_APP_NAME"),
            temp_unit=os.environ.get("WX_APP_TEMP_UNIT"),
            home_location=os.environ.get("WX_APP_HOME_LOCATION"),
        )
    locations = get_home_locations(conn)
    # created inside the spinner so the input is on screen before the
    # message clears (no blank frame between them)
    with st.bottom:
        question = st.chat_input("Ask a weather question...")
        st.caption(f"Type {CLEAR_COMMAND} to reset the chat context")

session = st.session_state.session

render_preferences(session, locations)

# /clear: wipe the on-screen chat and the context sent to the model. Turns
# already saved in chat_history are left alone.
if question and question.strip().lower() == CLEAR_COMMAND:
    session.history.clear()
    session.station_info.clear()
    st.toast("Chat cleared.")
    question = None

for turn in session.history:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        st.write(turn["answer"])

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner("Checking data..."):
            answer = chat_session.get_answer(session, question)
        if answer.startswith("[error]"):
            st.error(answer)
        else:
            st.write(answer)

if st.session_state.get("prefs_saving"):
    save_preferences(session)
