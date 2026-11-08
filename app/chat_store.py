"""
Chat history in Unity Catalog ({WX_CATALOG}.app): one row per user in
users, one per turn in chat_history (DDL: src/wx/app/create_chat_tables.py).

No login: user_id is uuid5 of a username (WX_APP_USER locally, or
new_anonymous_username() for a public visitor). Anonymous visitors aren't
saved to users; their preferences last for the session.
"""

import uuid
import logging

from wxcore.db import catalog


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# Arbitrary but fixed: changing it changes every existing user_id
_USER_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


ANONYMOUS_PREFIX = "anon-"


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _users_table() -> str:
    return f"{catalog()}.app.users"


def _chat_history_table() -> str:
    return f"{catalog()}.app.chat_history"


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def derive_user_id(username: str) -> str:
    """Deterministic user_id for a username (case/space-insensitive)."""
    return str(uuid.uuid5(_USER_ID_NAMESPACE, username.strip().lower()))


def new_anonymous_username() -> str:
    """A throwaway username for a public visitor, unique per browser session."""
    return f"{ANONYMOUS_PREFIX}{uuid.uuid4()}"


def is_anonymous(username: str) -> bool:
    """True for a new_anonymous_username() username."""
    return username.strip().startswith(ANONYMOUS_PREFIX)


def check_temp_unit(temp_unit) -> None:
    if temp_unit not in ("F", "C"):
        raise ValueError(f"temp_unit must be 'F' or 'C', got {temp_unit!r}")


def new_session_id() -> str:
    """A fresh session id; call once per app run to group its turns."""
    return str(uuid.uuid4())


def ensure_user(
    conn, username, email=None, display_name=None, temp_unit=None, home_location=None
):
    """
    Inserts the user on first sight, else refreshes last_seen_at; the other
    fields are filled only where empty. Returns (user_id, temp_unit,
    home_location). Raises on write failure.
    """
    username = username.strip()
    user_id = derive_user_id(username)
    if email is None and "@" in username:
        email = username

    with conn.cursor() as cur:
        cur.execute(
            f"""
            MERGE INTO {_users_table()} AS t
            USING (SELECT
                     CAST(:user_id       AS STRING) AS user_id,
                     CAST(:username      AS STRING) AS username,
                     CAST(:email         AS STRING) AS email,
                     CAST(:display_name  AS STRING) AS display_name,
                     CAST(:temp_unit     AS STRING) AS temp_unit,
                     CAST(:home_location AS STRING) AS home_location
                  ) AS s
            ON t.user_id = s.user_id
            WHEN MATCHED THEN UPDATE SET
                t.last_seen_at  = current_timestamp(),
                t.email         = coalesce(t.email, s.email),
                t.display_name  = coalesce(t.display_name, s.display_name),
                t.temp_unit     = coalesce(t.temp_unit, s.temp_unit),
                t.home_location = coalesce(t.home_location, s.home_location)
            WHEN NOT MATCHED THEN INSERT
                (user_id, username, email, display_name,
                 created_at, last_seen_at, is_active, temp_unit, home_location)
            VALUES
                (s.user_id, s.username, s.email, s.display_name,
                 current_timestamp(), current_timestamp(), true,
                 coalesce(s.temp_unit, 'F'), s.home_location)
            """,
            parameters={
                "user_id": user_id,
                "username": username,
                "email": email,
                "display_name": display_name,
                "temp_unit": temp_unit,
                "home_location": home_location,
            },
        )
        cur.execute(
            f"SELECT temp_unit, home_location FROM {_users_table()} WHERE user_id = :id",
            parameters={"id": user_id},
        )
        row = cur.fetchone()
    return user_id, row[0], row[1]


def update_user_prefs(conn, user_id, temp_unit, home_location) -> None:
    """Overwrites the user's temp_unit and home_location. Raises on write failure."""
    check_temp_unit(temp_unit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {_users_table()}
            SET temp_unit = :temp_unit, home_location = :home_location
            WHERE user_id = :user_id
            """,
            parameters={
                "user_id": user_id,
                "temp_unit": temp_unit,
                "home_location": home_location,
            },
        )


def count_resolved_turns_today(conn) -> int:
    """Resolved turns across all users since midnight UTC: the app-wide daily cap. Raises on failure."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {_chat_history_table()} "
            "WHERE created_at >= current_date() AND status = 'resolved'"
        )
        return cur.fetchone()[0]


def record_turn(
    conn,
    user_id,
    session_id,
    turn_index,
    prompt,
    response,
    status,
    resolved_json=None,
    model=None,
    error=None,
    station_id=None,
) -> None:
    """
    Appends one turn to chat_history. Never raises: a failure is logged, so
    history logging can't break the chat.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_chat_history_table()}
                    (turn_id, user_id, session_id, turn_index, created_at,
                     prompt, response, status, resolved_json, model, error,
                     station_id)
                VALUES
                    (:turn_id, :user_id, :session_id, :turn_index, current_timestamp(),
                     :prompt, :response, :status, :resolved_json, :model, :error,
                     :station_id)
                """,
                parameters={
                    "turn_id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "prompt": prompt,
                    "response": response,
                    "status": status,
                    "resolved_json": resolved_json,
                    "model": model,
                    "error": error,
                    "station_id": station_id,
                },
            )
    except Exception as exc:
        logger.warning("failed to record chat turn: %s", exc)
