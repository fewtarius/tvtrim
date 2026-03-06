#!/usr/bin/env python3
"""Database management module for comskip.

Handles SQLite database operations for tracking processed recordings.
The database tracks which files have been processed, their status,
and processing statistics.
"""

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS processed_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'in_progress')),
    commercials_found INTEGER DEFAULT 0,
    original_duration REAL,
    processed_duration REAL,
    bytes_saved INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    comskip_version TEXT,
    ffmpeg_version TEXT
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_file_path ON processed_files(file_path);",
    "CREATE INDEX IF NOT EXISTS idx_status ON processed_files(status);",
]

CREATE_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def get_connection(db_path: str):
    """Context manager for SQLite database connections.

    Handles connection lifecycle, commits on success,
    rolls back on exception.

    Args:
        db_path: Path to the SQLite database file.

    Yields:
        sqlite3.Connection with Row factory enabled.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Initialize the database, creating tables and indexes if needed.

    Args:
        db_path: Path to the SQLite database file.
    """
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEXES_SQL:
            conn.execute(idx_sql)
        conn.execute(CREATE_META_TABLE_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
    logger.info("Database initialized at %s", db_path)


def is_processed(db_path: str, file_path: str) -> bool:
    """Check if a file has already been processed or failed.

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.

    Returns:
        True if the file has a 'completed' or 'failed' status.
    """
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM processed_files WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return False
        return row["status"] in ("completed", "failed")


def is_in_progress(db_path: str, file_path: str) -> bool:
    """Check if a file is currently being processed.

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.

    Returns:
        True if the file has an 'in_progress' status.
    """
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM processed_files WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None:
            return False
        return row["status"] == "in_progress"


def mark_in_progress(db_path: str, file_path: str, file_size: int, file_mtime: float) -> None:
    """Mark a file as currently being processed.

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.
        file_size: File size in bytes.
        file_mtime: File modification time (Unix timestamp).
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO processed_files
               (file_path, file_size, file_mtime, status, started_at)
               VALUES (?, ?, ?, 'in_progress', ?)""",
            (file_path, file_size, file_mtime, datetime.now().isoformat()),
        )
    logger.info("Marked in_progress: %s", file_path)


def mark_completed(
    db_path: str,
    file_path: str,
    commercials_found: int = 0,
    original_duration: Optional[float] = None,
    processed_duration: Optional[float] = None,
    bytes_saved: int = 0,
    comskip_version: Optional[str] = None,
    ffmpeg_version: Optional[str] = None,
) -> None:
    """Mark a file as successfully processed.

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.
        commercials_found: Number of commercial segments detected.
        original_duration: Original file duration in seconds.
        processed_duration: Duration after commercial removal in seconds.
        bytes_saved: Bytes saved by removing commercials.
        comskip_version: Version of Comskip used.
        ffmpeg_version: Version of ffmpeg used.
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """UPDATE processed_files SET
                status = 'completed',
                commercials_found = ?,
                original_duration = ?,
                processed_duration = ?,
                bytes_saved = ?,
                completed_at = ?,
                comskip_version = ?,
                ffmpeg_version = ?
               WHERE file_path = ?""",
            (
                commercials_found,
                original_duration,
                processed_duration,
                bytes_saved,
                datetime.now().isoformat(),
                comskip_version,
                ffmpeg_version,
                file_path,
            ),
        )
    logger.info(
        "Marked completed: %s (commercials=%d, saved=%d bytes)",
        file_path,
        commercials_found,
        bytes_saved,
    )


def mark_failed(db_path: str, file_path: str, error_message: str) -> None:
    """Mark a file as failed processing. Failed files are not retried automatically.

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.
        error_message: Description of the failure.
    """
    with get_connection(db_path) as conn:
        conn.execute(
            """UPDATE processed_files SET
                status = 'failed',
                error_message = ?,
                completed_at = ?
               WHERE file_path = ?""",
            (error_message, datetime.now().isoformat(), file_path),
        )
    logger.error("Marked failed: %s - %s", file_path, error_message)


def reset_file(db_path: str, file_path: str) -> bool:
    """Reset a file's status so it can be reprocessed (used by --retry).

    Args:
        db_path: Path to the SQLite database file.
        file_path: Absolute path to the recording file.

    Returns:
        True if the file was found and reset, False otherwise.
    """
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM processed_files WHERE file_path = ?",
            (file_path,),
        )
        if cursor.rowcount > 0:
            logger.info("Reset file for reprocessing: %s", file_path)
            return True
        logger.warning("File not found in database: %s", file_path)
        return False


def cleanup_stale(db_path: str, max_age_hours: int = 24) -> int:
    """Clean up stale 'in_progress' entries (crash recovery).

    If a file has been 'in_progress' for longer than max_age_hours,
    it's assumed the process crashed and the entry is marked as failed.

    Args:
        db_path: Path to the SQLite database file.
        max_age_hours: Maximum hours an entry can be in_progress.

    Returns:
        Number of stale entries cleaned up.
    """
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """UPDATE processed_files SET
                status = 'failed',
                error_message = 'Stale in_progress entry (crash recovery)',
                completed_at = ?
               WHERE status = 'in_progress' AND started_at < ?""",
            (datetime.now().isoformat(), cutoff),
        )
        count = cursor.rowcount
    if count > 0:
        logger.warning("Cleaned up %d stale in_progress entries", count)
    return count


def get_stats(db_path: str) -> Dict[str, Any]:
    """Get processing statistics.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Dictionary with processing statistics.
    """
    stats = {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "in_progress": 0,
        "total_commercials_found": 0,
        "total_bytes_saved": 0,
        "total_duration_saved": 0.0,
    }

    with get_connection(db_path) as conn:
        # Count by status
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM processed_files GROUP BY status"
        ).fetchall()
        for row in rows:
            stats[row["status"]] = row["cnt"]
            stats["total"] += row["cnt"]

        # Aggregate stats for completed files
        row = conn.execute(
            """SELECT
                COALESCE(SUM(commercials_found), 0) as total_commercials,
                COALESCE(SUM(bytes_saved), 0) as total_bytes,
                COALESCE(SUM(original_duration - processed_duration), 0) as total_duration_saved
               FROM processed_files
               WHERE status = 'completed' AND commercials_found > 0"""
        ).fetchone()
        if row:
            stats["total_commercials_found"] = row["total_commercials"]
            stats["total_bytes_saved"] = row["total_bytes"]
            stats["total_duration_saved"] = row["total_duration_saved"] or 0.0

    return stats


def get_failed_files(db_path: str) -> List[Dict[str, Any]]:
    """Get a list of all failed files with their error messages.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        List of dictionaries with file_path and error_message.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT file_path, error_message, completed_at
               FROM processed_files
               WHERE status = 'failed'
               ORDER BY completed_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
