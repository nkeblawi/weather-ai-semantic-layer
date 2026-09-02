"""
Persists application chat history to Unity Catalog: one row per user in
{CATALOG}.app.users, one row per prompt/answer turn in
{CATALOG}.app.chat_history (DDL: src/wx/app/create_chat_tables.py).

{CATALOG} follows WX_CATALOG the same way test_harness.py's does, so this
writes to weather_dev.app.* when the harness is run against dev and
weather.app.* otherwise.

Stub auth: there is no login yet. The caller passes a `username` (email or
handle, from the WX_APP_USER env var or a startup prompt) and user_id is
derived from it deterministically with uuid5 -- the same username always
maps to the same row, on any machine, with no auth server. When real auth
is added, swap derive_user_id() for the provider's subject id and backfill.

Writes go through the databricks-sql-connector connection the app already
holds, using the same ":name" native-parameter style as test_harness.py.
record_turn() never raises: a logging failure must not take down the chat
loop, so it logs a warning and returns.
"""

import os
import uuid
import logging

logger = logging.getLogger(__name__)

CATALOG = os.environ.get("WX_CATALOG", "weather")
USERS_TABLE = f"{CATALOG}.app.users"
CHAT_HISTORY_TABLE = f"{CATALOG}.app.chat_history"

# Fixed namespace UUID so uuid5(username) is stable across processes and
# machines. Arbitrary constant -- do not change it or existing user_ids move.
_USER_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def derive_user_id(username: str) -> str:
    """Deterministic user_id for a username (case/space-insensitive)."""
    return str(uuid.uuid5(_USER_ID_NAMESPACE, username.strip().lower()))


def new_session_id() -> str:
    """A fresh session id; call once per app run to group its turns."""
    return str(uuid.uuid4())


def ensure_user(conn, username, email=None, display_name=None,
                temp_unit=None, home_location=None):
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
            MERGE INTO {USERS_TABLE} AS t
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
            f"SELECT temp_unit, home_location FROM {USERS_TABLE} WHERE user_id = :id",
            parameters={"id": user_id},
        )
        row = cur.fetchone()
    return user_id, row[0], row[1]


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
                INSERT INTO {CHAT_HISTORY_TABLE}
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
