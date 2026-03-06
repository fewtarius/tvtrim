#!/usr/bin/env python3
"""File scanner module for comskip.

Walks the television recording directory to find .mpg files
that are eligible for commercial stripping. Filters by age
(must be older than configured threshold) and checks the
database to skip already-processed files.
"""

import logging
import os
import time
from pathlib import Path
from typing import List, Tuple

import db

logger = logging.getLogger(__name__)


def find_eligible_files(
    television_dir: str,
    db_path: str,
    min_age_hours: float = 6.0,
    file_extension: str = ".mpg",
) -> List[Tuple[Path, float]]:
    """Find recording files eligible for commercial stripping.

    Scans the television directory for files matching the extension,
    filters by age (must be older than min_age_hours), and excludes
    files already tracked in the database (completed, failed, or in_progress).

    Args:
        television_dir: Path to the television recording directory.
        db_path: Path to the SQLite database file.
        min_age_hours: Minimum age in hours before a file is eligible.
        file_extension: File extension to scan for (default: .mpg).

    Returns:
        List of (Path, mtime) tuples, sorted oldest first.
    """
    tv_path = Path(television_dir)
    if not tv_path.is_dir():
        logger.error("Television directory not found: %s", television_dir)
        return []

    min_age_seconds = min_age_hours * 3600
    now = time.time()
    eligible = []

    logger.info("Scanning %s for %s files older than %.1f hours", television_dir, file_extension, min_age_hours)

    for dirpath, dirnames, filenames in os.walk(television_dir):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            if not filename.lower().endswith(file_extension):
                continue

            # Skip temp files from our own processing
            if ".comskip_temp" in filename:
                continue

            file_path = Path(dirpath) / filename
            try:
                stat = file_path.stat()
            except OSError as e:
                logger.warning("Cannot stat %s: %s", file_path, e)
                continue

            # Check age
            age_seconds = now - stat.st_mtime
            if age_seconds < min_age_seconds:
                logger.debug("Skipping (too recent, %.1f hours): %s", age_seconds / 3600, file_path)
                continue

            # Check if already processed
            file_path_str = str(file_path)
            if db.is_processed(db_path, file_path_str):
                logger.debug("Skipping (already processed): %s", file_path)
                continue

            if db.is_in_progress(db_path, file_path_str):
                logger.debug("Skipping (in progress): %s", file_path)
                continue

            eligible.append((file_path, stat.st_mtime))

    # Sort oldest first
    eligible.sort(key=lambda x: x[1])

    logger.info("Found %d eligible files for processing", len(eligible))
    return eligible


def get_file_info(file_path: Path) -> dict:
    """Get basic information about a recording file.

    Args:
        file_path: Path to the recording file.

    Returns:
        Dictionary with file_size, file_mtime, and file_name.
    """
    stat = file_path.stat()
    return {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "file_size": stat.st_size,
        "file_mtime": stat.st_mtime,
        "size_mb": stat.st_size / (1024 * 1024),
        "size_gb": stat.st_size / (1024 * 1024 * 1024),
        "age_hours": (time.time() - stat.st_mtime) / 3600,
    }
