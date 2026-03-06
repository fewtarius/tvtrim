# tvtrim

Automated commercial detection and stripping for HDHomeRun OTA television recordings.

## What It Does

tvtrim automatically finds your HDHomeRun recordings, detects commercial breaks using [Comskip](https://github.com/erikkaashoek/Comskip), and strips them using ffmpeg with lossless stream copy. The original `.mpg` files are replaced in-place so the HDHomeRun app continues to work seamlessly.

**Features:**
- Scans your recording directory hourly for new recordings
- Only processes files older than 6 hours (avoids in-progress recordings)
- **Multi-pass detection** - runs Comskip up to 3 times to catch hidden commercials
- Uses lossless stream copy (no re-encoding, no quality loss)
- Preserves MPEG-TS format for HDHomeRun app compatibility
- Tracks processed files in SQLite to avoid duplicate work
- Failed files are logged and skipped (no infinite retry loops)
- Processes one file at a time to avoid interfering with active recordings

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See [LICENSE](LICENSE) for details.

## Requirements

- **OS:** SteamOS / Arch Linux (x86_64)
- **Recording System:** HDHomeRun Record (Silicondust)
- **Storage:** Accessible recording directory (NFS, local, etc.)
- **Dependencies:** Installed automatically by the installer:
  - [Comskip](https://github.com/erikkaashoek/Comskip) (compiled from source)
  - ffmpeg
  - Python 3
  - SQLite 3 (bundled with Python)

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/tvtrim.git
cd tvtrim

# Edit configuration (adjust paths if needed)
nano tvtrim.conf

# Run the installer (requires root for package installation)
sudo ./install_tvtrim.sh
```

The installer will:
1. Handle SteamOS read-only filesystem if detected
2. Install ffmpeg and build dependencies via pacman
3. Clone and compile Comskip from source
4. Initialize the SQLite tracking database
5. Set up an hourly cron job

## Configuration

### `tvtrim.conf` - Application Settings

```ini
[paths]
television_dir = /television           # Where your recordings are stored
database_path = /home/deck/tvtrim/tvtrim.db
log_dir = /home/deck/tvtrim/logs
comskip_binary = /usr/local/bin/comskip
comskip_ini = /home/deck/tvtrim/comskip.ini

[processing]
min_age_hours = 6                      # Only process files older than this
file_extension = .mpg                  # Recording file extension
max_passes = 3                         # Multi-pass detection (1-3)

[ffmpeg]
binary = /usr/bin/ffmpeg
```

### `comskip.ini` - Commercial Detection Tuning

The included `comskip.ini` is tuned for US OTA (ATSC) broadcasts. You may need to adjust settings for your specific channels. See the [Comskip wiki](https://github.com/erikkaashoek/Comskip/wiki) for tuning options.

## Usage

```bash
# Automatic (runs via cron every hour)
# Nothing to do - it just works!

# Manual: process all eligible files
python3 tvtrim.py

# See what would be processed (no changes)
python3 tvtrim.py --dry-run

# Process a specific file
python3 tvtrim.py --file "/television/Show Name/episode.mpg"

# Retry a previously failed file
python3 tvtrim.py --retry "/television/Show Name/episode.mpg"

# View processing statistics
python3 tvtrim.py --stats
```

## How It Works

```
1. Scanner finds .mpg files older than 6 hours
2. Database is checked to skip already-processed files
3. For each unprocessed file:
   a. Pass 1: Comskip detects commercials, ffmpeg strips them
   b. Pass 2: Comskip re-analyzes for hidden commercials
   c. Pass 3: Final check (usually clean by now)
   d. Stops early if no commercials found
   e. Output verified and original replaced atomically
   f. Result logged and tracked in database
```

## Monitoring

```bash
# Watch the processing log
tail -f ~/tvtrim/logs/tvtrim_$(date +%Y%m%d).log

# Check cron output
tail -f ~/tvtrim/logs/cron.log

# View statistics
python3 ~/tvtrim/tvtrim.py --stats

# Query the database directly
sqlite3 ~/tvtrim/tvtrim.db "SELECT status, COUNT(*) FROM processed_files GROUP BY status;"
```

## File Structure

```
~/tvtrim/
├── tvtrim.py             # Main processing pipeline
├── scanner.py            # File discovery and filtering
├── db.py                 # SQLite database management
├── tvtrim.conf           # Application configuration
├── comskip.ini           # Comskip detection tuning
├── install_tvtrim.sh     # Installer script
├── tvtrim.db             # SQLite database (runtime)
├── logs/                 # Processing logs (runtime)
├── AGENTS.md             # Technical reference for AI agents
├── README.md             # This file
└── .gitignore
```

## Troubleshooting

**Comskip finds no commercials:**
- This is normal for some content (syndicated reruns, streaming captures)
- The file is marked as "completed" with 0 commercials - it won't be reprocessed

**Comskip cuts content or leaves commercials:**
- Tune `comskip.ini` settings. Key parameters:
  - `max_brightness` / `test_brightness` - Black frame detection sensitivity
  - `max_volume` - Silence detection threshold
  - `min_commercial_break` - Minimum break length to detect
  - `detect_method` - Detection algorithms to use (111 = conservative, 255 = aggressive)
- Multi-pass usually catches remaining commercials on the 2nd or 3rd pass

**Processing seems stuck:**
- Check the log: `tail -f ~/tvtrim/logs/tvtrim_$(date +%Y%m%d).log`
- Large files (6GB+) can take 30+ minutes for Comskip analysis per pass
- Stale `in_progress` entries are automatically cleaned up after 24 hours

**NFS issues:**
- The pipeline writes temp files to the same NFS directory for atomic rename
- SQLite database is stored locally (not on NFS) to avoid locking issues
- If NFS hangs, the process will timeout and mark the file as failed


