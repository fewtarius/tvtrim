#!/usr/bin/env python3
"""tvtrim - Automated commercial stripping for HDHomeRun recordings.

Main processing script that scans for recordings, detects commercials
using Comskip, and strips them using ffmpeg with lossless stream copy.

Usage:
    python3 tvtrim.py              # Process all eligible files
    python3 tvtrim.py --dry-run    # Show what would be processed
    python3 tvtrim.py --stats      # Show processing statistics
    python3 tvtrim.py --file PATH  # Process a single file
    python3 tvtrim.py --retry PATH # Retry a previously failed file
"""

import argparse
import configparser
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import db
import scanner

# Project root directory
PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG = PROJECT_DIR / "tvtrim.conf"


def setup_logging(log_dir: str) -> None:
    """Configure logging to both file and console.

    Creates a daily log file and also logs to stderr.

    Args:
        log_dir: Directory for log files.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / f"tvtrim_{datetime.now().strftime('%Y%m%d')}.log"

    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # File handler - detailed logging
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)

    # Console handler - info and above
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def load_config(config_path: str = None) -> configparser.ConfigParser:
    """Load application configuration.

    Args:
        config_path: Path to config file. Defaults to comskip.conf in project dir.

    Returns:
        ConfigParser instance with loaded configuration.
    """
    config = configparser.ConfigParser()
    path = config_path or str(DEFAULT_CONFIG)

    if not Path(path).is_file():
        logging.error("Configuration file not found: %s", path)
        sys.exit(1)

    config.read(path)
    return config


def get_tool_version(binary: str) -> Optional[str]:
    """Get the version string of an external tool.

    Args:
        binary: Path to the binary.

    Returns:
        Version string, or None if the tool is not found.
    """
    try:
        # Comskip doesn't support --version; running with no args
        # outputs version info on the first line to stderr
        if "comskip" in binary.lower():
            result = subprocess.run(
                [binary],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Comskip outputs "Comskip X.XX.XXX, ..." as first line of stderr
            output = result.stderr.strip() or result.stdout.strip()
        else:
            result = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip() or result.stderr.strip()
        if output:
            return output.split("\n")[0][:100]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def get_duration(file_path: str, ffmpeg_binary: str = "ffmpeg") -> Optional[float]:
    """Get the duration of a media file using ffprobe.

    Args:
        file_path: Path to the media file.
        ffmpeg_binary: Path to ffmpeg binary (ffprobe derived from it).

    Returns:
        Duration in seconds, or None if it cannot be determined.
    """
    ffprobe = ffmpeg_binary.replace("ffmpeg", "ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError, OSError) as e:
        logging.debug("Could not get duration for %s: %s", file_path, e)
    return None


def parse_edl(edl_path: str) -> List[Tuple[float, float]]:
    """Parse an EDL (Edit Decision List) file from Comskip.

    EDL format: start_time  end_time  type
    Type 0 = commercial (cut), Type 1 = show (keep)

    Args:
        edl_path: Path to the EDL file.

    Returns:
        List of (start, end) tuples representing commercial segments.
    """
    commercials = []
    try:
        with open(edl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    start = float(parts[0])
                    end = float(parts[1])
                    seg_type = int(parts[2])
                    if seg_type == 0:  # Commercial
                        commercials.append((start, end))
    except (OSError, ValueError) as e:
        logging.error("Failed to parse EDL %s: %s", edl_path, e)
    return commercials


def get_content_segments(
    commercials: List[Tuple[float, float]], total_duration: float
) -> List[Tuple[float, float]]:
    """Calculate content segments by inverting commercial segments.

    Given a list of commercial breaks and total duration,
    returns the segments of content to keep.

    Args:
        commercials: List of (start, end) tuples for commercial breaks.
        total_duration: Total duration of the recording in seconds.

    Returns:
        List of (start, end) tuples for content segments to keep.
    """
    if not commercials:
        return [(0, total_duration)]

    # Sort commercials by start time
    commercials = sorted(commercials, key=lambda x: x[0])

    segments = []
    current_pos = 0.0

    for comm_start, comm_end in commercials:
        if comm_start > current_pos:
            segments.append((current_pos, comm_start))
        current_pos = max(current_pos, comm_end)

    # Add final segment after last commercial
    if current_pos < total_duration:
        segments.append((current_pos, total_duration))

    return segments


def run_comskip(
    file_path: str,
    comskip_binary: str,
    comskip_ini: str,
    work_dir: str,
) -> Optional[str]:
    """Run Comskip to detect commercials in a recording.

    Args:
        file_path: Path to the recording file.
        comskip_binary: Path to the Comskip binary.
        comskip_ini: Path to the Comskip configuration file.
        work_dir: Working directory for Comskip output files.

    Returns:
        Path to the generated EDL file, or None if detection failed.
    """
    logger = logging.getLogger(__name__)
    logger.info("Running Comskip on: %s", file_path)

    # Comskip generates output files based on input filename
    # We run it with --output to control where files go
    try:
        result = subprocess.run(
            [
                comskip_binary,
                "--ini", comskip_ini,
                "--output", work_dir,
                "--ts",  # Input is MPEG Transport Stream (ATSC recordings)
                "--quiet",  # Reduce console output noise
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=7200,  # 2 hour timeout for very long recordings
        )

        # Comskip returns:
        # 0 = commercials found
        # 1 = no commercials found (or error)
        if result.returncode not in (0, 1):
            logger.error(
                "Comskip failed with exit code %d: %s",
                result.returncode,
                result.stderr[:500],
            )
            return None

    except subprocess.TimeoutExpired:
        logger.error("Comskip timed out (>2h) for: %s", file_path)
        return None
    except FileNotFoundError:
        logger.error("Comskip binary not found: %s", comskip_binary)
        return None

    # Look for the generated EDL file
    input_stem = Path(file_path).stem
    edl_path = Path(work_dir) / f"{input_stem}.edl"

    if edl_path.is_file() and edl_path.stat().st_size > 0:
        logger.info("EDL generated: %s", edl_path)
        return str(edl_path)

    logger.info("No EDL generated (no commercials detected): %s", file_path)
    return None


def strip_commercials(
    file_path: str,
    edl_path: str,
    output_path: str,
    ffmpeg_binary: str,
    total_duration: float,
) -> bool:
    """Strip commercials from a recording using ffmpeg.

    Uses lossless stream copy to extract content segments as MPEG-TS,
    then concatenates them using the ffmpeg concat demuxer with explicit
    mpegts output format to preserve transport stream structure.

    Args:
        file_path: Path to the original recording.
        edl_path: Path to the EDL file with commercial markers.
        output_path: Path for the output file (commercials removed).
        ffmpeg_binary: Path to the ffmpeg binary.
        total_duration: Total duration of the original recording.

    Returns:
        True if stripping succeeded, False otherwise.
    """
    logger = logging.getLogger(__name__)

    # Parse commercial segments
    commercials = parse_edl(edl_path)
    if not commercials:
        logger.warning("No commercial segments in EDL: %s", edl_path)
        return False

    # Calculate content segments to keep
    content_segments = get_content_segments(commercials, total_duration)
    if not content_segments:
        logger.error("No content segments found after filtering: %s", file_path)
        return False

    logger.info(
        "Found %d commercial breaks, keeping %d content segments",
        len(commercials),
        len(content_segments),
    )

    # Use a temporary directory for intermediate segment files
    work_dir = Path(output_path).parent
    segment_files = []

    try:
        # Extract each content segment as MPEG-TS
        for i, (start, end) in enumerate(content_segments):
            segment_path = work_dir / f".comskip_segment_{i:04d}.ts"
            segment_files.append(segment_path)

            duration = end - start
            logger.debug(
                "Extracting segment %d: %.2fs -> %.2fs (%.2fs)",
                i, start, end, duration,
            )

            result = subprocess.run(
                [
                    ffmpeg_binary,
                    "-y",  # Overwrite
                    "-i", str(file_path),
                    "-ss", f"{start:0.3f}",
                    "-to", f"{end:0.3f}",
                    "-c", "copy",
                    "-map", "0",  # Map all streams (video, audio, data)
                    "-f", "mpegts",  # Force MPEG-TS output format
                    "-avoid_negative_ts", "make_zero",
                    "-mpegts_copyts", "1",  # Preserve timestamps
                    str(segment_path),
                ],
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour per segment
            )

            if result.returncode != 0:
                logger.error(
                    "ffmpeg segment extraction failed (segment %d): %s",
                    i, result.stderr[:500],
                )
                return False

        # Create concat file list for the concat demuxer
        concat_list = work_dir / ".comskip_concat.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for seg_path in segment_files:
                # ffmpeg concat needs escaped single quotes in filenames
                escaped = str(seg_path).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # Concatenate all segments using concat demuxer with explicit mpegts output
        # This preserves the MPEG-TS transport stream format, PAT/PMT tables,
        # stream order, and program info required by HDHomeRun playback
        logger.info("Concatenating %d segments into output file", len(segment_files))
        result = subprocess.run(
            [
                ffmpeg_binary,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                "-map", "0",
                "-f", "mpegts",  # Force MPEG-TS output format
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )

        if result.returncode != 0:
            logger.error("ffmpeg concatenation failed: %s", result.stderr[:500])
            return False

        logger.info("Commercial stripping complete: %s", output_path)
        return True

    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out for: %s", file_path)
        return False
    except FileNotFoundError:
        logger.error("ffmpeg binary not found: %s", ffmpeg_binary)
        return False
    finally:
        # Clean up segment files and concat list
        for seg_path in segment_files:
            try:
                seg_path.unlink(missing_ok=True)
            except OSError:
                pass
        concat_list_path = work_dir / ".comskip_concat.txt"
        try:
            concat_list_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_single_pass(
    file_path: str,
    comskip_binary: str,
    comskip_ini: str,
    ffmpeg_binary: str,
    output_path: str,
    pass_num: int,
) -> Optional[int]:
    """Run a single detection + stripping pass on a file.

    Args:
        file_path: Path to the input recording.
        comskip_binary: Path to the Comskip binary.
        comskip_ini: Path to the Comskip configuration.
        ffmpeg_binary: Path to the ffmpeg binary.
        output_path: Path for the stripped output file.
        pass_num: Pass number (1-based, for logging).

    Returns:
        Number of commercial segments found and stripped, or None on error.
        Returns 0 if no commercials were detected.
    """
    logger = logging.getLogger(__name__)

    # Get duration for content segment calculation
    duration = get_duration(file_path, ffmpeg_binary)
    if duration is None:
        logger.warning("Pass %d: Could not determine duration", pass_num)
        return None

    with tempfile.TemporaryDirectory(dir=str(PROJECT_DIR)) as temp_dir:
        edl_path = run_comskip(file_path, comskip_binary, comskip_ini, temp_dir)

        if edl_path is None:
            logger.info("Pass %d: No commercials detected", pass_num)
            return 0

        commercials = parse_edl(edl_path)
        if not commercials:
            logger.info("Pass %d: EDL empty, no commercials", pass_num)
            return 0

        logger.info("Pass %d: Found %d commercial segments", pass_num, len(commercials))

        success = strip_commercials(
            file_path, edl_path, output_path, ffmpeg_binary, duration,
        )

        if not success:
            logger.error("Pass %d: ffmpeg stripping failed", pass_num)
            return None

        # Verify output
        output_file = Path(output_path)
        if not output_file.is_file() or output_file.stat().st_size == 0:
            logger.error("Pass %d: Output file missing or empty", pass_num)
            return None

        out_duration = get_duration(output_path, ffmpeg_binary)
        if out_duration and out_duration < 60:
            logger.error("Pass %d: Output too short (%.0fs)", pass_num, out_duration)
            return None

        return len(commercials)


MAX_PASSES = 3  # Default, overridden by config


def process_file(
    file_path: Path,
    config: configparser.ConfigParser,
    db_path: str,
) -> bool:
    """Process a single recording file through the multi-pass pipeline.

    Uses multiple Comskip passes to catch commercials that are only
    detectable after surrounding commercials have been removed.

    1. Mark as in_progress in database
    2. Run Comskip to detect commercials
    3. If commercials found, strip with ffmpeg
    4. Run Comskip again on the output (up to max_passes total)
    5. Repeat until no more commercials are found
    6. Verify output and replace original
    7. Update database with results

    Args:
        file_path: Path to the recording file.
        config: Application configuration.
        db_path: Path to the SQLite database.

    Returns:
        True if processing succeeded (or no commercials found), False on error.
    """
    logger = logging.getLogger(__name__)
    file_path_str = str(file_path)
    file_info = scanner.get_file_info(file_path)

    comskip_binary = config.get("paths", "comskip_binary")
    comskip_ini = config.get("paths", "comskip_ini")
    ffmpeg_binary = config.get("ffmpeg", "binary")
    max_passes = config.getint("processing", "max_passes", fallback=MAX_PASSES)

    comskip_version = get_tool_version(comskip_binary)
    ffmpeg_version = get_tool_version(ffmpeg_binary)

    logger.info("=" * 60)
    logger.info("Processing: %s", file_path.name)
    logger.info("Size: %.2f GB | Age: %.1f hours", file_info["size_gb"], file_info["age_hours"])
    logger.info("=" * 60)

    start_time = time.time()
    original_size = file_info["file_size"]

    # Mark as in progress
    db.mark_in_progress(db_path, file_path_str, file_info["file_size"], file_info["file_mtime"])

    # Get original duration
    original_duration = get_duration(file_path_str, ffmpeg_binary)
    if original_duration is None:
        logger.warning("Could not determine duration, using estimate")
        original_duration = file_info["size_gb"] * 3600

    # Check disk space before processing
    work_dir = file_path.parent
    disk_usage = shutil.disk_usage(str(work_dir))
    if disk_usage.free < file_info["file_size"] * 1.5:
        error_msg = (
            f"Insufficient disk space: {disk_usage.free / (1024**3):0.1f} GB free, "
            f"need ~{file_info['file_size'] * 1.5 / (1024**3):0.1f} GB"
        )
        logger.error(error_msg)
        db.mark_failed(db_path, file_path_str, error_msg)
        return False

    # Multi-pass commercial detection and stripping
    total_commercials = 0
    current_input = str(file_path)
    temp_output = file_path.parent / f".comskip_temp_{file_path.stem}.mpg"

    try:
        for pass_num in range(1, max_passes + 1):
            logger.info("--- Pass %d of %d ---", pass_num, max_passes)

            result = _run_single_pass(
                current_input,
                comskip_binary,
                comskip_ini,
                ffmpeg_binary,
                str(temp_output),
                pass_num,
            )

            if result is None:
                # Error during processing
                if pass_num == 1:
                    # First pass failed - mark as failed
                    db.mark_failed(db_path, file_path_str, f"Pass {pass_num} failed")
                    return False
                else:
                    # Later pass failed - use results from previous passes
                    logger.warning("Pass %d failed, using results from previous passes", pass_num)
                    break

            if result == 0:
                # No more commercials found
                logger.info("Pass %d: No more commercials, done.", pass_num)
                break

            total_commercials += result
            logger.info(
                "Pass %d: Stripped %d commercials (total so far: %d)",
                pass_num, result, total_commercials,
            )

            # Replace the input with the output for the next pass
            if current_input == str(file_path):
                # First pass: replace original with temp output
                shutil.move(str(temp_output), str(file_path))
                current_input = str(file_path)
            else:
                # Subsequent passes: replace in-place
                shutil.move(str(temp_output), str(file_path))

        # Final result
        processed_duration = get_duration(str(file_path), ffmpeg_binary)
        final_size = file_path.stat().st_size
        bytes_saved = original_size - final_size

        elapsed = time.time() - start_time

        if total_commercials > 0:
            logger.info(
                "Output: %.2f GB (saved %.2f GB, %.1f%%)",
                final_size / (1024**3),
                bytes_saved / (1024**3),
                (bytes_saved / original_size * 100) if original_size > 0 else 0,
            )
        logger.info(
            "Processing complete in %.1fs (%d total commercials across all passes)",
            elapsed, total_commercials,
        )

        # Update database
        db.mark_completed(
            db_path,
            file_path_str,
            commercials_found=total_commercials,
            original_duration=original_duration,
            processed_duration=processed_duration or original_duration,
            bytes_saved=bytes_saved,
            comskip_version=comskip_version,
            ffmpeg_version=ffmpeg_version,
        )
        return True

    except Exception as e:
        logger.exception("Unexpected error processing %s", file_path)
        db.mark_failed(db_path, file_path_str, str(e))
        return False
    finally:
        # Ensure temp file is cleaned up on failure
        if temp_output.is_file():
            try:
                temp_output.unlink()
            except OSError:
                pass


def show_stats(db_path: str) -> None:
    """Display processing statistics.

    Args:
        db_path: Path to the SQLite database.
    """
    stats = db.get_stats(db_path)

    print("\n=== tvtrim Processing Statistics ===\n")
    print(f"  Total files tracked:    {stats['total']}")
    print(f"  Completed:              {stats['completed']}")
    print(f"  Failed:                 {stats['failed']}")
    print(f"  In progress:            {stats['in_progress']}")
    print()
    print(f"  Commercials found:      {stats['total_commercials_found']}")
    print(f"  Space saved:            {stats['total_bytes_saved'] / (1024**3):0.2f} GB")

    duration_saved = stats["total_duration_saved"]
    hours = int(duration_saved // 3600)
    minutes = int((duration_saved % 3600) // 60)
    print(f"  Time saved:             {hours}h {minutes}m")
    print()

    # Show failed files
    failed = db.get_failed_files(db_path)
    if failed:
        print(f"  Failed files ({len(failed)}):")
        for f in failed[:10]:
            print(f"    - {Path(f['file_path']).name}")
            print(f"      Error: {f['error_message']}")
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")
    print()


def main():
    """Main entry point for the comskip processing pipeline."""
    parser = argparse.ArgumentParser(
        description="Automated commercial stripping for HDHomeRun recordings",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to configuration file (default: tvtrim.conf)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without making changes",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show processing statistics and exit",
    )
    parser.add_argument(
        "--file",
        type=str,
        help="Process a specific file (bypasses age check)",
    )
    parser.add_argument(
        "--retry",
        type=str,
        help="Reset and retry a previously failed file",
    )
    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)

    # Set up logging
    log_dir = config.get("paths", "log_dir")
    setup_logging(log_dir)

    logger = logging.getLogger(__name__)
    logger.info("tvtrim starting")

    db_path = config.get("paths", "database_path")
    television_dir = config.get("paths", "television_dir")
    min_age_hours = config.getfloat("processing", "min_age_hours")

    # Initialize database
    db.init_db(db_path)

    # Clean up stale entries from previous crashes
    stale_count = db.cleanup_stale(db_path)
    if stale_count > 0:
        logger.info("Cleaned up %d stale entries from previous runs", stale_count)

    # Handle --stats
    if args.stats:
        show_stats(db_path)
        return

    # Handle --retry
    if args.retry:
        retry_path = str(Path(args.retry).resolve())
        if not Path(retry_path).is_file():
            logger.error("File not found: %s", retry_path)
            sys.exit(1)
        logger.info("Resetting file for retry: %s", retry_path)
        db.reset_file(db_path, retry_path)
        # Fall through to process it
        args.file = retry_path

    # Handle --file (single file mode)
    if args.file:
        file_path = Path(args.file).resolve()
        if not file_path.is_file():
            logger.error("File not found: %s", file_path)
            sys.exit(1)
        logger.info("Single file mode: %s", file_path)

        # Check if already processed (unless --retry was used, which clears DB entry)
        if db.is_processed(db_path, str(file_path)):
            logger.info("File already processed, skipping: %s", file_path)
            logger.info("Use --retry to reprocess a previously processed file")
            return

        if args.dry_run:
            info = scanner.get_file_info(file_path)
            print(f"Would process: {file_path.name} ({info['size_gb']:0.2f} GB)")
            return
        success = process_file(file_path, config, db_path)
        sys.exit(0 if success else 1)

    # Normal operation: scan and process
    eligible = scanner.find_eligible_files(
        television_dir,
        db_path,
        min_age_hours=min_age_hours,
    )

    if not eligible:
        logger.info("No eligible files to process")
        return

    if args.dry_run:
        print(f"\nWould process {len(eligible)} files:\n")
        for file_path, mtime in eligible:
            info = scanner.get_file_info(file_path)
            print(f"  {file_path.name}")
            print(f"    Size: {info['size_gb']:0.2f} GB | Age: {info['age_hours']:0.1f} hours")
        print()
        return

    # Process files sequentially
    logger.info("Processing %d eligible files", len(eligible))
    processed = 0
    failed = 0

    for file_path, mtime in eligible:
        try:
            success = process_file(file_path, config, db_path)
            if success:
                processed += 1
            else:
                failed += 1
        except Exception as e:
            logger.exception("Fatal error processing %s: %s", file_path, e)
            failed += 1

    logger.info(
        "Session complete: %d processed, %d failed, %d total",
        processed, failed, len(eligible),
    )


if __name__ == "__main__":
    main()
