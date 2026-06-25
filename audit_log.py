"""
Audit log: SQLite-backed, structured.

Every attribution decision and every appeal writes a row here. See planning.md
§4 ("What the system does on receipt") for the schema rationale — the table
supports the reviewer view without needing a join because v1 keeps things
simple, but the schema is wide enough to grow.

In M3 only `entry_type='decision'` rows are written. M4 will populate
`llm_score`. M5 will add `entry_type='appeal'` rows and a status update path.
"""

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

DB_PATH = "provenance_guard.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type          TEXT NOT NULL,            -- 'decision' or 'appeal'
    content_id          TEXT NOT NULL,
    creator_id          TEXT,
    timestamp           TEXT NOT NULL,            -- ISO-8601 UTC

    -- decision fields (NULL for appeal rows)
    attribution         TEXT,                     -- 'likely_human' | 'uncertain' | 'likely_ai'
    confidence          REAL,                     -- combined score in [0, 1]
    stylometric_score   REAL,
    llm_score           REAL,                     -- populated starting M4
    llm_rationale       TEXT,                     -- short reason from LLM (M4+)
    features_json       TEXT,                     -- raw stylometric features
    label               TEXT,
    status              TEXT,                     -- 'classified' | 'under_review'

    -- appeal fields (NULL for decision rows)
    appeal_id           TEXT,
    appeal_reasoning    TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_content_id ON audit_log(content_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
"""

# Columns added after M3. ALTER TABLE makes startup idempotent for users
# upgrading from an existing M3 database (CREATE TABLE IF NOT EXISTS would
# skip the new column).
_M4_NEW_COLUMNS = [
    ("llm_rationale", "TEXT"),
]


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema if it doesn't exist. Safe to call on every startup.

    Also applies any post-M3 column migrations idempotently.
    """
    with _conn() as c:
        c.executescript(SCHEMA)
        # Idempotent migrations for users upgrading from M3.
        for col_name, col_type in _M4_NEW_COLUMNS:
            try:
                c.execute(f"ALTER TABLE audit_log ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists


def log_decision(
    *,
    content_id: str,
    creator_id: str,
    timestamp: str,
    attribution: str,
    confidence: float,
    stylometric_score: float,
    stylometric_features: Dict[str, Any],
    label: str,
    status: str = "classified",
    llm_score: Optional[float] = None,
    llm_rationale: Optional[str] = None,
) -> None:
    """Insert a decision row.

    llm_score and llm_rationale are None when the LLM signal was unavailable
    (the fallback path; verdict is forced to 'uncertain' upstream).
    """
    with _conn() as c:
        c.execute(
            """
            INSERT INTO audit_log (
                entry_type, content_id, creator_id, timestamp,
                attribution, confidence, stylometric_score,
                llm_score, llm_rationale,
                features_json, label, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "decision", content_id, creator_id, timestamp,
                attribution, confidence, stylometric_score,
                llm_score, llm_rationale,
                json.dumps(stylometric_features), label, status,
            ),
        )


def get_recent_entries(limit: int = 50) -> List[Dict[str, Any]]:
    """Return audit entries newest-first. Strips NULLs for cleaner output."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        result = []
        for row in rows:
            entry = dict(row)
            # Inline the parsed features and drop the raw JSON column.
            if entry.get("features_json"):
                entry["features"] = json.loads(entry["features_json"])
            entry.pop("features_json", None)
            # Drop None-valued fields so different entry_types stay readable.
            entry = {k: v for k, v in entry.items() if v is not None}
            result.append(entry)
        return result


def get_submission(content_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the decision row for one content_id, or None if not found."""
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM audit_log
               WHERE content_id = ? AND entry_type = 'decision'
               ORDER BY id DESC LIMIT 1""",
            (content_id,),
        ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        if entry.get("features_json"):
            entry["features"] = json.loads(entry["features_json"])
            entry.pop("features_json", None)
        return entry


def log_appeal(
    *,
    appeal_id: str,
    content_id: str,
    creator_id: str,
    timestamp: str,
    reasoning: str,
) -> None:
    """Insert an appeal row (M5).

    The appeal row is separate from the original decision row so that the
    original verdict, scores, and label are preserved unmodified. The
    decision row's `status` is updated via update_status() in the same
    transaction-equivalent sequence (called from app.py).
    """
    with _conn() as c:
        c.execute(
            """
            INSERT INTO audit_log (
                entry_type, content_id, creator_id, timestamp,
                appeal_id, appeal_reasoning, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "appeal", content_id, creator_id, timestamp,
                appeal_id, reasoning, "under_review",
            ),
        )


def update_status(content_id: str, new_status: str) -> int:
    """Update the status field on the most recent decision row.

    Returns the number of rows affected (0 if content_id not found).
    Only modifies the `status` column — the original verdict, scores,
    and label remain unchanged per planning.md §4.
    """
    with _conn() as c:
        cur = c.execute(
            """UPDATE audit_log
               SET status = ?
               WHERE content_id = ? AND entry_type = 'decision'""",
            (new_status, content_id),
        )
        return cur.rowcount
