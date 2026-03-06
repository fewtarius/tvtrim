# AGENTS.md

**Version:** 1.0
**Date:** 2026-03-06
**Purpose:** Technical reference for comskip development (methodology in .clio/instructions.md)

---

## Project Overview

**tvtrim** is an automated commercial stripping pipeline for HDHomeRun OTA television recordings.

- **Language:** Python 3
- **External Tools:** Comskip (commercial detection), ffmpeg (video processing)
- **Database:** SQLite 3
- **Architecture:** Cron-driven batch processing pipeline
- **Philosophy:** The Unbroken Method (see .clio/instructions.md)

---

## Quick Setup

```bash
# Install (requires sudo for pacman and cron)
cd /home/deck/comskip
sudo ./install_tvtrim.sh

# Run manually (process all eligible files)
python3 /home/deck/comskip/tvtrim.py

# Dry run (see what would be processed)
python3 /home/deck/comskip/tvtrim.py --dry-run

# Process a single file
python3 /home/deck/comskip/tvtrim.py --file "/television/Show/episode.mpg"

# Show statistics
python3 /home/deck/comskip/tvtrim.py --stats
```

---

## Architecture

```
cron (hourly, :00)
    |
    v
tvtrim.py (main entry point)
    |
    v
scanner.py - Find .mpg files > 6 hours old
    |         Check SQLite DB for already-processed
    |         Order: oldest first
    v
For each unprocessed file:
    |
    +-- db.py: mark_in_progress()
    |
    +-- comskip (external binary)
    |   Input: .mpg file
    |   Output: .edl file (commercial timestamps)
    |
    +-- ffmpeg (external binary)
    |   Input: .mpg + .edl
    |   Output: .comskip_temp.mpg (commercials removed, stream copy)
    |
    +-- Verify temp file (size, duration check)
    |
    +-- Atomic replace: mv temp -> original
    |
    +-- db.py: mark_completed() or mark_failed()
    |
    +-- Cleanup: remove .edl, .log, temp files
    |
    v
Log results to /home/deck/comskip/logs/
```

---

## System Context

| Component | Details |
|-----------|---------|
| **Host OS** | SteamOS (Arch Linux-based), x86_64, kernel 6.18+ |
| **Hardware** | 4 CPU cores, 16GB RAM (Steam Deck or similar) |
| **User** | `deck` |
| **Storage** | NFS mount: `192.168.0.4:/volume1/television` on `/television` (Synology NAS, 7TB) |
| **Recording Path** | `/television` (also accessible as `/home/deck/hdhomerun/shows` - same NFS mount) |
| **Software Home** | `/home/deck/comskip` |
| **Recording Software** | HDHomeRun Record (Silicondust), runs as systemd service `hdhomerun` |
| **Frontend** | HDHomeRun app (multi-device), reads directly from recording path |
| **File Format** | MPEG-TS (`.mpg`), ATSC broadcast captures |
| **File Naming** | `Show Name S01E04 19930125 [ID].mpg` |
| **Library Size** | ~719 files, ~1.3TB, 17 show directories |
| **Tuners** | 4 HDHomeRun tuners (max 4 concurrent recordings) |

**SteamOS Considerations:**
- Read-only root filesystem by default. Must run `steamos-readonly disable` before installing packages.
- Packages installed via `pacman` may be overwritten by SteamOS updates.
- The installer script must handle the read-only filesystem toggle.

---

## Directory Structure

| Path | Purpose |
|------|---------|
| `/home/deck/comskip/` | Project root |
| `tvtrim.py` | Main entry point (cron target) |
| `scanner.py` | File discovery and filtering module |
| `db.py` | SQLite database management module |
| `tvtrim.conf` | Application configuration (paths, settings) |
| `comskip.ini` | Comskip detection tuning configuration |
| `install_comskip.sh` | Installer script (deps, build, cron) |
| `tvtrim.db` | SQLite database (runtime, gitignored) |
| `logs/` | Log directory (runtime, gitignored) |
| `.clio/PRD.md` | Product Requirements Document |
| `.clio/instructions.md` | Project methodology |
| `AGENTS.md` | This file - technical reference |
| `README.md` | GitHub documentation |
| `.gitignore` | Git exclusions |
| `LICENSE` | Open source license |

**Key External Paths:**

| Path | Purpose |
|------|---------|
| `/television` | Recording library (NFS mount, ~719 .mpg files) |
| `/home/deck/hdhomerun/shows` | Symlink/bind to `/television` |
| `/usr/local/bin/comskip` | Comskip binary (compiled from source) |
| `/usr/bin/ffmpeg` | ffmpeg binary (installed via pacman) |
| `/etc/cron.d/tvtrim` | Cron job definition |

---

## Code Style

**Python Conventions:**

- Python 3.9+ (SteamOS ships Python 3)
- Follow PEP 8 style guidelines
- **4 spaces** indentation (never tabs)
- **UTF-8 encoding** for all files
- Type hints for function signatures
- Docstrings for all public functions and classes
- `#!/usr/bin/env python3` shebang on executable scripts
- Minimal external dependencies (prefer stdlib)

**Module Template:**

```python
#!/usr/bin/env python3
"""Module description.

Brief explanation of what this module does and its role
in the comskip pipeline.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def function_name(param: str, optional_param: Optional[int] = None) -> bool:
    """Brief description of function.

    Args:
        param: Description of parameter.
        optional_param: Description of optional parameter.

    Returns:
        Description of return value.
    """
    pass
```

**Logging:**

```python
import logging

logger = logging.getLogger(__name__)

# Use appropriate levels:
logger.info("Processing file: %s", filepath)
logger.warning("Comskip found no commercials in: %s", filepath)
logger.error("ffmpeg failed for %s: %s", filepath, error_msg)
logger.debug("EDL contents: %s", edl_data)
```

**Bash Script Conventions (installer):**

- Follow the `deploy_hdhomerun` style as reference
- `#!/bin/bash` shebang
- `set -e` for fail-fast
- Check command exit codes explicitly
- Echo progress messages
- Quote all variable expansions

---

## Database Schema

```sql
CREATE TABLE processed_files (
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

CREATE INDEX idx_file_path ON processed_files(file_path);
CREATE INDEX idx_status ON processed_files(status);
```

**Status values:**
- `in_progress` - Currently being processed (only 1 at a time)
- `completed` - Successfully processed (commercials stripped or none found)
- `failed` - Processing failed (will NOT be retried automatically)

**Crash recovery:** On startup, any `in_progress` entries older than 24 hours are reset (cleaned up as failed).

---

## Key Design Decisions

These decisions are documented in the PRD and must be followed:

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Output format** | Keep `.mpg` (MPEG-TS) | Required for HDHomeRun app compatibility |
| **Processing method** | Lossless stream copy (`ffmpeg -c copy`) | Fast, no quality loss, preserves original codec |
| **File replacement** | In-place (temp file + atomic rename) | Same file path keeps HDHomeRun frontend working |
| **Concurrency** | Sequential (one file at a time) | Avoid interfering with recording performance over NFS |
| **Processing order** | Oldest files first | Process backlog systematically |
| **Age threshold** | 6 hours minimum | Ensures recording is complete before processing |
| **Failure handling** | Mark as failed, never auto-retry | Prevents infinite retry loops on problematic files |
| **Scheduling** | Cron, hourly at :00 | Simple, consistent with existing hdhomerun cron patterns |
| **Language** | Python 3 | Better SQLite integration, error handling, logging than bash |
| **Commercial detection** | Comskip (compiled from source) | Gold standard for OTA commercial detection |

---

## Testing

**Before Committing:**

```bash
# 1. Python syntax check
python3 -m py_compile tvtrim.py
python3 -m py_compile scanner.py
python3 -m py_compile db.py

# 2. Run linter (if available)
python3 -m flake8 *.py --max-line-length=120

# 3. Dry run test
python3 tvtrim.py --dry-run

# 4. Single file test (use a Star Trek episode)
python3 tvtrim.py --file "/television/Star Trek Deep Space Nine/Star Trek Deep Space Nine S01E04 19930125 [ID].mpg"

# 5. Stats check
python3 tvtrim.py --stats

# 6. Verify processed file plays correctly
ffprobe "/television/Star Trek Deep Space Nine/Star Trek Deep Space Nine S01E04 19930125 [ID].mpg"
```

**Test Corpus:**

Start with Star Trek episodes (expendable test content):
1. Star Trek: Deep Space Nine (~80+ episodes)
2. Star Trek: The Next Generation (~80+ episodes)
3. Star Trek: Voyager (~60+ episodes)
4. Star Trek: Enterprise (~30+ episodes)

**Test Cases:**

| Test | Validation |
|------|------------|
| Process single episode | File replaced, plays in HDHomeRun app, DB updated |
| Run again on same file | Skipped (already processed) |
| File < 6 hours old | Skipped |
| Comskip can't analyze | Marked as failed, original untouched |
| No commercials found | Marked completed, original untouched |
| Kill process mid-run | Next run cleans up stale `in_progress` entry |
| Special chars in filename | `Hell's Kitchen` episodes work correctly |
| Large file (6GB+) | No memory issues |

**Pre-Commit Checklist:**

- [ ] `python3 -m py_compile` passes on all `.py` files
- [ ] Docstrings updated if API changed
- [ ] Commit message explains WHAT and WHY
- [ ] No `TODO`/`FIXME` left unresolved
- [ ] Tested on at least one real recording

---

## Commit Format

```
type(scope): brief description

Problem: What was broken/incomplete
Solution: How you fixed it
Testing: How you verified the fix
```

**Types:** `feat`, `fix`, `refactor`, `docs`, `test`, `chore`

**Example:**

```bash
git add -A
git commit -m "feat(pipeline): implement comskip + ffmpeg commercial stripping

Problem: Recordings contain commercials that degrade viewing experience
Solution: Integrated Comskip for detection, ffmpeg stream copy for removal
Testing: Successfully processed DS9 S01E04, file plays correctly in app"
```

---

## Common Patterns

**Subprocess Execution:**

```python
import subprocess

result = subprocess.run(
    ["comskip", "--ini", ini_path, input_file],
    capture_output=True,
    text=True,
    timeout=3600  # 1 hour timeout
)

if result.returncode != 0:
    logger.error("Comskip failed: %s", result.stderr)
    raise ProcessingError(f"Comskip exit code {result.returncode}")
```

**Atomic File Replacement (NFS-safe):**

```python
from pathlib import Path
import shutil

temp_path = original_path.with_suffix('.comskip_temp.mpg')

# ... ffmpeg writes to temp_path ...

# Verify before replacing
if temp_path.stat().st_size == 0:
    raise ProcessingError("Output file is empty")

# Atomic replace (on same filesystem)
shutil.move(str(temp_path), str(original_path))
```

**SQLite with Context Manager:**

```python
import sqlite3
from contextlib import contextmanager

@contextmanager
def get_db_connection(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Path Handling (special characters):**

```python
from pathlib import Path

# Always use pathlib for path operations
# It handles spaces, apostrophes, brackets correctly
file_path = Path("/television/Hell's Kitchen/Hell's Kitchen S01E01 [ID].mpg")

# For subprocess calls, pass Path objects or str(path)
subprocess.run(["comskip", str(file_path)], ...)
```

**Configuration Parsing:**

```python
import configparser

config = configparser.ConfigParser()
config.read('/home/deck/comskip/tvtrim.conf')

television_dir = config.get('paths', 'television_dir')
min_age_hours = config.getint('processing', 'min_age_hours')
```

---

## EDL (Edit Decision List) Format

Comskip outputs EDL files with this format:
```
start_seconds    end_seconds    type
0.00             120.50         0
450.25           570.75         0
```

Where type `0` = commercial (cut), type `1` = content (keep).

**To strip commercials:** Keep all segments NOT marked in the EDL, use ffmpeg to concatenate the content segments.

---

## ffmpeg Commercial Stripping Pattern

```bash
# Extract segments to keep (between commercial breaks)
# Use concat demuxer for lossless joining

# 1. Create segment list file
# 2. Extract each content segment with stream copy
# 3. Concatenate segments into final output

ffmpeg -i input.mpg -ss START -to END -c copy -avoid_negative_ts make_zero segment_N.mpg
# ... for each content segment ...

# Concatenate with concat protocol or demuxer
ffmpeg -f concat -safe 0 -i segments.txt -c copy output.mpg
```

---

## NFS Considerations

- **Atomic rename:** `os.rename()` / `shutil.move()` is atomic when source and destination are on the same filesystem (both on `/television` NFS mount)
- **Temp file location:** Write temp files to the same NFS directory as the original (ensures same-filesystem atomic rename)
- **File locking:** SQLite DB is on local filesystem (`/home/deck/comskip/tvtrim.db`), NOT on NFS
- **Timeouts:** NFS mount uses `hard` option with 900s timeout - long operations may hang. Set subprocess timeouts accordingly.
- **Space check:** Before processing, verify available space on NFS mount (`shutil.disk_usage()`)

---

## Installer Script Requirements

The `install_comskip.sh` script must:

1. Check for root/sudo access
2. Handle SteamOS read-only filesystem (`steamos-readonly disable`)
3. Install system packages: `ffmpeg`, `base-devel`, `git`, `argtable`
4. Clone Comskip from GitHub: `https://github.com/erikkaashoek/Comskip`
5. Compile Comskip (`./autogen.sh && ./configure && make && make install`)
6. Create directory structure (`logs/`)
7. Initialize SQLite database
8. Install cron job to `/etc/cron.d/tvtrim`
9. Optionally re-enable read-only filesystem (`steamos-readonly enable`)
10. Verify installation (test `comskip --help` and `ffmpeg -version`)

**Reference:** Follow the style of `/home/deck/deploy_hdhomerun/deploy_hdhomerun` for consistency.

---

## Anti-Patterns (What NOT To Do)

| Anti-Pattern | Why It's Wrong | What To Do |
|--------------|----------------|------------|
| Re-encode video | Loses quality, wastes CPU time | Always use `-c copy` stream copy |
| Write temp files to local disk | Can't atomic rename across filesystems | Write temp to same NFS directory |
| Store SQLite DB on NFS | SQLite + NFS = corruption | Keep DB at `/home/deck/comskip/tvtrim.db` |
| Process files < 6 hours old | May process in-progress recordings | Always check mtime age threshold |
| Auto-retry failed files | Infinite loops on broken files | Mark failed, require manual `--retry` |
| Multiple concurrent processes | Overwhelms NFS, interferes with recordings | Sequential, one file at a time |
| Hardcode paths | Breaks portability for GitHub users | Use `tvtrim.conf` configuration file |
| Ignore subprocess return codes | Silent failures, corrupted output | Check every return code, log errors |
| Delete original before verifying output | Data loss if processing fails | Verify temp file, then atomic rename |
| Use `os.system()` | Security risk, no error handling | Use `subprocess.run()` with explicit args |

---

## Quick Reference

**Process recordings:**
```bash
python3 /home/deck/comskip/tvtrim.py
```

**Check what would be processed:**
```bash
python3 /home/deck/comskip/tvtrim.py --dry-run
```

**View processing stats:**
```bash
python3 /home/deck/comskip/tvtrim.py --stats
```

**Check logs:**
```bash
tail -f /home/deck/comskip/logs/comskip_$(date +%Y%m%d).log
```

**Query database:**
```bash
sqlite3 /home/deck/comskip/tvtrim.db "SELECT status, COUNT(*) FROM processed_files GROUP BY status;"
```

**Check cron:**
```bash
cat /etc/cron.d/tvtrim
```

**Test comskip on a file:**
```bash
comskip --ini=/home/deck/comskip/comskip.ini "/television/Show/episode.mpg"
```

**Git operations:**
```bash
git status
git diff
git log --oneline -10
git add -A && git commit -m "type(scope): description"
```

---

*For project methodology and workflow, see .clio/instructions.md*
*For product requirements and design, see .clio/PRD.md*
*For universal agent behavior, see system prompt*
