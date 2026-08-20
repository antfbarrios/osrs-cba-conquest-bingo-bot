"""
Simple SQLite storage for bingo drop submissions.

Using SQLite (a single file, bingo.db) keeps this dependency-free and easy
to back up -- just copy the .db file. If you outgrow it later, the query
functions below are the only place you'd need to touch to swap in Postgres
or similar.
"""

import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "bingo.db"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name       TEXT NOT NULL,
                region          TEXT NOT NULL DEFAULT '',
                boss_name       TEXT NOT NULL,
                drop_name       TEXT NOT NULL,
                rsn             TEXT,
                image_url       TEXT NOT NULL,
                submitter_id    INTEGER NOT NULL,
                submitter_name  TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                message_id      INTEGER,
                channel_id      INTEGER,
                team_message_id INTEGER,
                reviewed_by     TEXT,
                reviewed_at     TEXT,
                nudged_at       TEXT,
                submitted_at    TEXT NOT NULL
            )
            """
        )
        # Migration for databases created before the region column existed.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "region" not in existing_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN region TEXT NOT NULL DEFAULT ''")
        if "rsn" not in existing_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN rsn TEXT")
        if "team_message_id" not in existing_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN team_message_id INTEGER")
        if "nudged_at" not in existing_cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN nudged_at TEXT")
        # "notes" column (if present from before this change) is left in place
        # untouched -- old data stays readable, it's just no longer written to.

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_rsn (
                user_id     INTEGER PRIMARY KEY,
                rsn         TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_preferences (
                user_id  INTEGER PRIMARY KEY,
                enabled  INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                key    TEXT PRIMARY KEY,
                value  TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_config (
                team_name                     TEXT PRIMARY KEY,
                channel_id                    INTEGER NOT NULL,
                sheet_id                      TEXT,
                color_hex                     TEXT,
                starting_screenshot_channel_id INTEGER
            )
            """
        )
        # Migration for databases created before color_hex existed.
        team_cols = {row["name"] for row in conn.execute("PRAGMA table_info(team_config)")}
        if "color_hex" not in team_cols:
            conn.execute("ALTER TABLE team_config ADD COLUMN color_hex TEXT")
        if "starting_screenshot_channel_id" not in team_cols:
            conn.execute("ALTER TABLE team_config ADD COLUMN starting_screenshot_channel_id INTEGER")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS starting_screenshots (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name          TEXT NOT NULL,
                requirement_label  TEXT NOT NULL,
                region             TEXT NOT NULL,
                image_url          TEXT NOT NULL,
                submitter_id       INTEGER NOT NULL,
                submitter_name     TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'pending',
                message_id         INTEGER,
                channel_id         INTEGER,
                team_message_id    INTEGER,
                reviewed_by        TEXT,
                reviewed_at        TEXT,
                submitted_at       TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS missions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                description        TEXT NOT NULL,
                created_by         TEXT NOT NULL,
                created_at         TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'active',
                completed_by_team  TEXT,
                completed_at       TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mission_submissions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                mission_id       INTEGER NOT NULL,
                team_name        TEXT NOT NULL,
                image_url        TEXT NOT NULL,
                submitter_id     INTEGER NOT NULL,
                submitter_name   TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                message_id       INTEGER,
                channel_id       INTEGER,
                team_message_id  INTEGER,
                reviewed_by      TEXT,
                reviewed_at      TEXT,
                submitted_at     TEXT NOT NULL
            )
            """
        )

def create_submission(team_name, region, boss_name, drop_name, rsn, image_url,
                       submitter_id, submitter_name) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions
                (team_name, region, boss_name, drop_name, rsn, image_url,
                 submitter_id, submitter_name, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                team_name, region, boss_name, drop_name, rsn, image_url,
                submitter_id, submitter_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def set_message_ref(submission_id: int, message_id: int, channel_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET message_id = ?, channel_id = ? WHERE id = ?",
            (message_id, channel_id, submission_id),
        )


def set_team_message_ref(submission_id: int, team_message_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET team_message_id = ? WHERE id = ?",
            (team_message_id, submission_id),
        )


def get_submission_by_message(message_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def update_status(submission_id: int, status: str, reviewed_by: str):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE submissions
            SET status = ?, reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (status, reviewed_by, datetime.now(timezone.utc).isoformat(), submission_id),
        )


def get_submission_by_id(submission_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None


def revert_to_pending(submission_id: int):
    """Used by /undoapproval -- puts a submission back in the review queue
    and clears who reviewed it / when, since that decision is being undone."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE submissions
            SET status = 'pending', reviewed_by = NULL, reviewed_at = NULL
            WHERE id = ?
            """,
            (submission_id,),
        )


def get_team_totals(status: str = "approved"):
    """Returns [{'team_name': ..., 'drop_count': ...}, ...] -- handy for a future /leaderboard command."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT team_name, COUNT(*) as drop_count
            FROM submissions
            WHERE status = ?
            GROUP BY team_name
            ORDER BY drop_count DESC
            """,
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_approved_duplicates(team_name: str, region: str, boss_name: str, drop_name: str) -> int:
    """How many times this exact team+region+source+drop has already been approved.
    Informational only (for a heads-up to mods) -- resubmitting the same drop is
    expected and allowed, tiles aren't one-time-only."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as count FROM submissions
            WHERE status = 'approved' AND team_name = ? AND region = ?
              AND boss_name = ? AND drop_name = ?
            """,
            (team_name, region, boss_name, drop_name),
        ).fetchone()
        return row["count"]


def get_submissions_by_submitter(submitter_id: int, limit: int = 15):
    """Most recent submissions by a specific Discord user, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM submissions
            WHERE submitter_id = ?
            ORDER BY submitted_at DESC
            LIMIT ?
            """,
            (submitter_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_submissions():
    """All currently pending submissions, oldest first (so mods clear the backlog in order)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = 'pending' ORDER BY submitted_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_approved_submissions():
    """Every approved submission's team/region/source/drop/rsn -- used to build
    the detailed per-player scoreboard (points come from drop_points.py, not
    the database, so this just returns what's needed to look those up)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT team_name, region, boss_name, drop_name, rsn FROM submissions WHERE status = 'approved'"
        ).fetchall()
        return [dict(r) for r in rows]


def get_approved_pets():
    """Every approved submission whose drop is a pet (drop name starting
    with 'Pet') -- used by /obtainedpets. Filtered in Python rather than
    SQL LIKE, to share the exact same 'starts with Pet' definition used
    for the approval-time duplicate block in main.py."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT team_name, region, boss_name, drop_name, rsn FROM submissions WHERE status = 'approved'"
        ).fetchall()
        return [dict(r) for r in rows if r["drop_name"].startswith("Pet")]


def get_recent_approved(limit: int = 15):
    """Most recently-approved submissions, newest first -- for /recent."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM submissions
            WHERE status = 'approved'
            ORDER BY reviewed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def search_approved_submissions(query: str, limit: int = 25):
    """Approved submissions matching `query` against drop/team/RSN/source
    name (case-insensitive substring) -- powers /undoapproval's autocomplete
    so mods can find something older than the 5 most recent approvals."""
    like_query = f"%{query}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM submissions
            WHERE status = 'approved'
              AND (drop_name LIKE ? OR team_name LIKE ? OR rsn LIKE ? OR boss_name LIKE ?)
            ORDER BY reviewed_at DESC
            LIMIT ?
            """,
            (like_query, like_query, like_query, like_query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_submissions():
    """Every submission regardless of status, oldest first -- for /export."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM submissions ORDER BY submitted_at ASC").fetchall()
        return [dict(r) for r in rows]


def get_stale_pending(minutes: float):
    """Pending submissions older than `minutes` that haven't been nudged yet.
    Filtered in Python rather than SQL, since submitted_at is stored as a
    full ISO-8601 string with timezone offset (e.g. 2026-07-23T05:39:12+00:00)
    which doesn't compare correctly against SQLite's datetime('now', ...)
    string format."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = 'pending' AND nudged_at IS NULL"
        ).fetchall()
    stale = []
    for row in rows:
        row = dict(row)
        try:
            submitted = datetime.fromisoformat(row["submitted_at"])
        except (ValueError, TypeError):
            continue
        if submitted <= cutoff:
            stale.append(row)
    return stale


def mark_nudged(submission_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET nudged_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), submission_id),
        )


def set_user_rsn(user_id: int, rsn: str):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_rsn (user_id, rsn, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET rsn = excluded.rsn, updated_at = excluded.updated_at
            """,
            (user_id, rsn, datetime.now(timezone.utc).isoformat()),
        )


def get_user_rsn(user_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT rsn FROM user_rsn WHERE user_id = ?", (user_id,)).fetchone()
        return row["rsn"] if row else None


def set_notify_preference(user_id: int, enabled: bool):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO notify_preferences (user_id, enabled) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled
            """,
            (user_id, 1 if enabled else 0),
        )


def get_notify_preference(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM notify_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row["enabled"]) if row else False


def get_first_approved_per_region():
    """Returns {region: submission_dict} for the earliest-approved (by
    reviewed_at) submission in each region -- for /firstblood."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = 'approved' ORDER BY reviewed_at ASC"
        ).fetchall()
    first_per_region = {}
    for row in rows:
        row = dict(row)
        if row["region"] not in first_per_region:
            first_per_region[row["region"]] = row
    return first_per_region


def get_first_approved_per_source():
    """Returns {(region, boss_name): submission_dict} for the earliest-approved
    (by reviewed_at) submission from each source -- for /firstblooddetailed."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = 'approved' ORDER BY reviewed_at ASC"
        ).fetchall()
    first_per_source = {}
    for row in rows:
        row = dict(row)
        key = (row["region"], row["boss_name"])
        if key not in first_per_source:
            first_per_source[key] = row
    return first_per_source


# --- Bot config (key/value) -- replaces REVIEW_CHANNEL_ID / MOD_ROLE_NAME
# living only in .env, so they can be set live via Discord commands instead
# of editing files and redeploying. ---

def get_config(key: str, default=None):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


# --- Team config (channel + optional sheet per team) -- replaces
# team_channels.py / team_sheets.py so teams can be added/edited live via
# Discord commands instead of editing files and redeploying. ---

def add_team(team_name: str, channel_id: int):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO team_config (team_name, channel_id) VALUES (?, ?)
            ON CONFLICT(team_name) DO UPDATE SET channel_id = excluded.channel_id
            """,
            (team_name, channel_id),
        )


def remove_team(team_name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM team_config WHERE team_name = ?", (team_name,))
        return cur.rowcount > 0


def rename_team(old_name: str, new_name: str) -> str:
    """Renames a team, updating BOTH its config row and every historical
    submission's team_name -- otherwise old drops would stay attributed to
    a name that no longer exists in team_config, splitting one team's
    history across two names in /scoreboard, /team, /export, etc.

    Returns 'ok', 'not_found' (old_name isn't a configured team), or
    'name_taken' (new_name is already a different configured team).
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM team_config WHERE team_name = ?", (old_name,)
        ).fetchone()
        if not existing:
            return "not_found"

        if new_name != old_name:
            collision = conn.execute(
                "SELECT 1 FROM team_config WHERE team_name = ?", (new_name,)
            ).fetchone()
            if collision:
                return "name_taken"

        conn.execute(
            "UPDATE team_config SET team_name = ? WHERE team_name = ?", (new_name, old_name)
        )
        conn.execute(
            "UPDATE submissions SET team_name = ? WHERE team_name = ?", (new_name, old_name)
        )
        return "ok"


def set_team_sheet(team_name: str, sheet_id: str) -> bool:
    """Returns False if the team doesn't exist yet (add it with /addteam first)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE team_config SET sheet_id = ? WHERE team_name = ?", (sheet_id, team_name)
        )
        return cur.rowcount > 0


def set_team_starting_screenshot_channel(team_name: str, channel_id: int) -> bool:
    """Returns False if the team doesn't exist yet (add it with /addteam first)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE team_config SET starting_screenshot_channel_id = ? WHERE team_name = ?",
            (channel_id, team_name),
        )
        return cur.rowcount > 0


def set_team_color(team_name: str, color_hex: str | None) -> bool:
    """Set or clear a team's map color. color_hex should be '#RRGGBB' or None to clear.
    Returns False if the team doesn't exist."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE team_config SET color_hex = ? WHERE team_name = ?",
            (color_hex, team_name),
        )
        return cur.rowcount > 0


def get_all_teams():
    """Returns [{'team_name', 'channel_id', 'sheet_id', 'color_hex'}, ...] ordered by name."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM team_config ORDER BY team_name ASC").fetchall()
        return [dict(r) for r in rows]


def get_team_by_channel(channel_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT team_name FROM team_config WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["team_name"] if row else None


def get_channel_by_team(team_name: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT channel_id FROM team_config WHERE team_name = ?", (team_name,)
        ).fetchone()
        return row["channel_id"] if row else None


def get_starting_screenshot_channel_by_team(team_name: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT starting_screenshot_channel_id FROM team_config WHERE team_name = ?", (team_name,)
        ).fetchone()
        return row["starting_screenshot_channel_id"] if row else None


def get_sheet_by_team(team_name: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT sheet_id FROM team_config WHERE team_name = ?", (team_name,)
        ).fetchone()
        return row["sheet_id"] if row else None


def wipe_everything() -> str:
    """Used by /resetcompetition. Backs up the current database to a
    timestamped file NEXT TO bingo.db, then deletes every row from every
    table (submissions, review channel/mod role config, every team, saved
    RSNs, notify preferences) -- the schema itself is untouched, so the
    bot keeps working immediately, just completely empty.

    Returns the backup file's path so the caller can tell the mod where it
    is / offer it as a download.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    backup_path = DB_PATH.parent / f"bingo_backup_{timestamp}.db"
    shutil.copy2(DB_PATH, backup_path)

    with _connect() as conn:
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM bot_config")
        conn.execute("DELETE FROM team_config")
        conn.execute("DELETE FROM user_rsn")
        conn.execute("DELETE FROM notify_preferences")
        conn.execute("DELETE FROM starting_screenshots")
        conn.execute("DELETE FROM missions")
        conn.execute("DELETE FROM mission_submissions")

    return str(backup_path)


# --- Starting screenshots -- some tiles (Mahogany Homes, Tempoross, etc.)
# require a "before" screenshot on file before a team can submit points for
# them, so mods can compare against the eventual submission. Goes through
# its own review flow, posted to its own channel (separate from the main
# submissions review channel) rather than cluttering that one. ---

def create_starting_screenshot(team_name: str, requirement_label: str, region: str,
                                image_url: str, submitter_id: int, submitter_name: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO starting_screenshots
                (team_name, requirement_label, region, image_url,
                 submitter_id, submitter_name, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                team_name, requirement_label, region, image_url,
                submitter_id, submitter_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def set_starting_screenshot_message_ref(entry_id: int, message_id: int, channel_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE starting_screenshots SET message_id = ?, channel_id = ? WHERE id = ?",
            (message_id, channel_id, entry_id),
        )


def set_starting_screenshot_team_message_ref(entry_id: int, team_message_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE starting_screenshots SET team_message_id = ? WHERE id = ?",
            (team_message_id, entry_id),
        )


def get_starting_screenshot_by_message(message_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM starting_screenshots WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def update_starting_screenshot_status(entry_id: int, status: str, reviewed_by: str):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE starting_screenshots
            SET status = ?, reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (status, reviewed_by, datetime.now(timezone.utc).isoformat(), entry_id),
        )


def has_approved_starting_screenshot(team_name: str, requirement_label: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM starting_screenshots
            WHERE team_name = ? AND requirement_label = ? AND status = 'approved'
            LIMIT 1
            """,
            (team_name, requirement_label),
        ).fetchone()
        return row is not None


def get_approved_starting_screenshot(team_name: str, requirement_label: str):
    """Like has_approved_starting_screenshot, but returns the actual entry
    (image_url, submitter, etc.) instead of just a bool -- used to show it
    alongside the real submission for mods to compare directly. Most
    recent approved one if there's ever more than one on file."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM starting_screenshots
            WHERE team_name = ? AND requirement_label = ? AND status = 'approved'
            ORDER BY submitted_at DESC
            LIMIT 1
            """,
            (team_name, requirement_label),
        ).fetchone()
        return dict(row) if row else None


def get_starting_screenshots_for_team(team_name: str):
    """Most recent entry per requirement_label for this team -- used by the
    status-check command so players can see what's cleared/pending/missing
    without hitting the /submit block blindly."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM starting_screenshots
            WHERE team_name = ?
            ORDER BY submitted_at DESC
            """,
            (team_name,),
        ).fetchall()
    latest_by_label = {}
    for row in rows:
        row = dict(row)
        if row["requirement_label"] not in latest_by_label:
            latest_by_label[row["requirement_label"]] = row
    return latest_by_label


# --- Missions -- daily/twice-daily race challenges any team can complete
# for a flat bonus (first team to submit an approved screenshot wins it;
# stays open until claimed, even if a newer mission has since started, so
# more than one can be active at once). ---

def create_mission(description: str, created_by: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO missions (description, created_by, created_at, status) VALUES (?, ?, ?, 'active')",
            (description, created_by, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_active_missions():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM missions WHERE status = 'active' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_mission(mission_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        return dict(row) if row else None


def complete_mission(mission_id: int, team_name: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE missions SET status = 'completed', completed_by_team = ?, completed_at = ? WHERE id = ?",
            (team_name, datetime.now(timezone.utc).isoformat(), mission_id),
        )


def cancel_mission(mission_id: int):
    """Marks a mission cancelled (distinct from completed, so it never
    counts as a win) -- used for test runs or calling off a mistake.
    Doesn't touch its submissions; the caller is responsible for
    auto-rejecting any still-pending ones."""
    with _connect() as conn:
        conn.execute(
            "UPDATE missions SET status = 'cancelled', completed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), mission_id),
        )


def create_mission_submission(mission_id: int, team_name: str, image_url: str,
                               submitter_id: int, submitter_name: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO mission_submissions
                (mission_id, team_name, image_url, submitter_id, submitter_name, status, submitted_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (mission_id, team_name, image_url, submitter_id, submitter_name,
             datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def set_mission_submission_message_ref(entry_id: int, message_id: int, channel_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE mission_submissions SET message_id = ?, channel_id = ? WHERE id = ?",
            (message_id, channel_id, entry_id),
        )


def set_mission_submission_team_message_ref(entry_id: int, team_message_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE mission_submissions SET team_message_id = ? WHERE id = ?",
            (team_message_id, entry_id),
        )


def get_mission_submission_by_message(message_id: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM mission_submissions WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def update_mission_submission_status(entry_id: int, status: str, reviewed_by: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE mission_submissions SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (status, reviewed_by, datetime.now(timezone.utc).isoformat(), entry_id),
        )


def get_pending_mission_submissions(mission_id: int, exclude_id: int = None):
    """Every still-pending submission for a mission, used to auto-reject
    the rest once one gets approved (the race is over)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM mission_submissions WHERE mission_id = ? AND status = 'pending'",
            (mission_id,),
        ).fetchall()
    return [dict(r) for r in rows if r["id"] != exclude_id]


def get_mission_wins_by_team():
    """{team_name: win_count} across every completed mission -- used for
    the mission-wins stat and to add mission bonus points to overall
    team totals."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT completed_by_team, COUNT(*) as wins FROM missions "
            "WHERE status = 'completed' GROUP BY completed_by_team"
        ).fetchall()
    return {row["completed_by_team"]: row["wins"] for row in rows}

