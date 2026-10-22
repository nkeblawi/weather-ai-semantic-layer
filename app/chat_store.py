"""
Persists application chat history to Unity Catalog: one row per user in
{catalog}.app.users, one row per prompt/answer turn in
{catalog}.app.chat_history (DDL: src/wx/app/create_chat_tables.py).

{catalog} is WX_CATALOG (wxcore.db.catalog(), read on each call), so this
writes to weather_dev.app.* when run against dev and weather.app.* in
production.

Stub auth: there is no login yet. The caller passes a `username` (email or
handle from the WX_APP_USER env var locally, or new_anonymous_username() for
a public visitor) and user_id is derived from it deterministically with
uuid5, so the same username always maps to the same row, on any machine,
with no auth server. When real auth is added, swap derive_user_id() for the
provider's subject id and backfill.

Anonymous visitors aren't saved to users; their preferences last for the
session.

Writes go through the databricks-sql-connector connection the app already
holds, using the same ":name" native-parameter style binding.
record_turn() never raises: a logging failure must not take down the chat
loop, so it logs a warning and returns.
"""

import uuid
import logging

from wxcore.db import catalog

logger = logging.getLogger(__name__)


def _users_table() -> str:
    return f"{catalog()}.app.users"


def _chat_history_table() -> str:
    return f"{catalog()}.app.chat_history"


# Fixed namespace UUID so uuid5(username) is stable across processes and
# machines. Arbitrary constant -- do not change it or existing user_ids move.
_USER_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def derive_user_id(username: str) -> str:
    """Deterministic user_id for a username (case/space-insensitive)."""
    return str(uuid.uuid5(_USER_ID_NAMESPACE, username.strip().lower()))


ANONYMOUS_PREFIX = "anon-"


def new_anonymous_username() -> str:
    """
    A throwaway username for a visitor with no sign-in (the public MVP):
    unique per browser session, with preferences kept for that session
    only. derive_user_id() still applies to it as usual.
    """
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
    Insert the user on first sight, else refresh last_seen_at. email /
    display_name / temp_unit / home_location are seeded only if not already
    set (the future settings UI owns them after that). Returns
    (user_id, temp_unit, home_location). Raises on write failure -- a
    startup problem should stop the app, not be swallowed.
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
    """
    Overwrite the user's temp_unit / home_location (the settings UI's
    Update button). Unlike ensure_user's coalesce seeding, this replaces
    whatever is stored. Raises on write failure so the UI can show it.
    """
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
    """
    Resolved turns (ones that ran answer_query) recorded since midnight
    (warehouse session time zone, UTC by default) across all users -- the
    app-wide daily question cap. Clarifications and errors aren't counted.
    Raises on failure; the caller decides whether to fail open.
    """
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
    Append one prompt/answer turn to chat_history. Best-effort: on any
    failure this logs a warning and returns rather than raising, so history
    logging can never break the chat.

    station_id is the site the turn resolved to; its name/city are not
    stored -- join station_id to {catalog}.analytics.stations when a report
    needs them.
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to record chat turn: %s", exc)
