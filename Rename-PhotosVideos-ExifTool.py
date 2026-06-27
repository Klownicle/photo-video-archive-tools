#!/usr/bin/env python3
r"""
Rename-PhotosVideos-ExifTool.py

Renames images and videos in place using ExifTool date-taken style metadata.

Default behavior uses generic example paths. Change these constants or pass -Root / -ExifTool for your environment:
  Root folder:    D:\MediaArchive\Photos and Videos
  ExifTool path:  C:\Tools\ExifTool\exiftool.exe
  New filename:   YYYY-MM-DD_0000000001_IMG.ext for images, YYYY-MM-DD_0000000001_VID.ext for videos
  State JSON:     stored beside this script
  Missing CSV:    stored beside this script

Examples:
  python Rename-PhotosVideos-ExifTool.py -WhatIf
  python Rename-PhotosVideos-ExifTool.py

  # Reprocess the review CSV after setting Decision to CONFIRM or DENY
  python Rename-PhotosVideos-ExifTool.py -ReviewCsv ".\photo_rename_missing_dates.csv" -WhatIf
  python Rename-PhotosVideos-ExifTool.py -ReviewCsv ".\photo_rename_missing_dates.csv"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXIFTOOL = r"C:\Tools\ExifTool\exiftool.exe"
DEFAULT_ROOT = r"D:\MediaArchive\Photos and Videos"
DEFAULT_TAG = "IMG"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_JSON = SCRIPT_DIR / "photo_rename_state.json"
DEFAULT_MISSING_DATE_CSV = SCRIPT_DIR / "photo_rename_missing_dates.csv"
DEFAULT_RENAME_LOG_CSV = SCRIPT_DIR / "photo_rename_log.csv"
DEFAULT_REMAINING_REVIEW_CSV = SCRIPT_DIR / "photo_rename_missing_dates_remaining.csv"
DEFAULT_CATCHUP_REVIEW_CSV = SCRIPT_DIR / "photo_rename_catchup_missing_dates.csv"

SUPPORTED_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".dng",
    # Videos
    ".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mts", ".m2ts", ".3gp", ".3g2", ".mpg",
    ".mpeg", ".mkv", ".webm",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mts", ".m2ts", ".3gp", ".3g2", ".mpg",
    ".mpeg", ".mkv", ".webm",
}

IMAGE_EXTENSIONS = SUPPORTED_EXTENSIONS - VIDEO_EXTENSIONS

# ExifTool can read many legacy video formats but cannot write metadata to some of them.
# MPG/MPEG date metadata write attempts produce:
#   "Writing of MPG files is not yet supported"
# These files can still be renamed from the confirmed CSV date.
METADATA_WRITE_UNSUPPORTED_EXTENSIONS = {
    ".mpg", ".mpeg",
}

# Tags are checked in this order. FileModifyDate is intentionally excluded because the
# user requested date-taken style values. If you later want fallback to modified time,
# use -AllowFileModifyDate.
DATE_TAGS = [
    "DateTimeOriginal",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "CreationDate",
    "ContentCreateDate",
    "ModifyDate",
]

FILEMODIFY_TAG = "FileModifyDate"

RENAMED_FILE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{10}_[A-Za-z0-9-]+\.[^.]+$",
    re.IGNORECASE,
)


@dataclass
class RenameRecord:
    full_path: Path
    capture_datetime: dt.datetime
    date_source: str
    reason: str = ""


@dataclass
class MissingDateRecord:
    full_path: Path
    reason: str
    date_guess: str = ""
    date_guess_source: str = ""


@dataclass
class ProcessResult:
    source: Path
    target: Path | None = None
    sequence: int | None = None
    status: str = ""
    message: str = ""
    metadata_status: str = "NOT_REQUESTED"
    metadata_message: str = ""

    @property
    def success(self) -> bool:
        if self.status.startswith("ERROR"):
            return False
        if self.metadata_status.startswith("ERROR"):
            return False
        return self.status in {"RENAMED", "WOULD_RENAME", "SKIPPED_ALREADY_RENAMED"}


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def normalize_path_for_compare(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except Exception:
        return os.path.normcase(str(path))


def is_supported_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def looks_already_renamed(path: Path) -> bool:
    return bool(RENAMED_FILE_PATTERN.match(path.name))


def media_tag_for_file(path: Path, fallback_tag: str = DEFAULT_TAG) -> str:
    """Return IMG for image files and VID for video files."""
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "VID"
    if suffix in IMAGE_EXTENSIONS:
        return "IMG"
    return fallback_tag


def scan_supported_files(root: Path, include_already_renamed: bool) -> list[Path]:
    """
    Fast recursive scan using os.scandir.

    The final rename format is skipped as early as possible by checking entry.name
    before constructing a Path object. This keeps reruns faster after a large
    library has already been processed.
    """
    results: list[Path] = []
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        name = entry.name
                        suffix = Path(name).suffix.lower()
                        if suffix not in SUPPORTED_EXTENSIONS:
                            continue

                        # Fast skip for files already renamed by this tool.
                        if not include_already_renamed and RENAMED_FILE_PATTERN.match(name):
                            continue

                        results.append(Path(entry.path))

                    except OSError as ex:
                        eprint(f"WARNING: Could not inspect {entry.path}: {ex}")
        except OSError as ex:
            eprint(f"WARNING: Could not scan {current}: {ex}")

    results.sort(key=lambda p: normalize_path_for_compare(p))
    return results


def print_progress(
    label: str,
    done: int,
    total: int,
    *,
    renamed: int = 0,
    missing: int = 0,
    skipped: int = 0,
    errors: int = 0,
) -> None:
    if total <= 0:
        return

    width = 28
    pct = done / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    msg = (
        f"\r{label} [{bar}] {done:,}/{total:,} {pct * 100:6.2f}%"
        f" | Renamed: {renamed:,} | Missing date: {missing:,}"
        f" | Skipped: {skipped:,} | Errors: {errors:,}"
    )
    print(msg, end="", flush=True)


def clear_progress_line() -> None:
    print("\r" + " " * 150 + "\r", end="", flush=True)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "next_sequence": 1,
            "last_assigned_sequence": 0,
            "total_renamed": 0,
            "created_utc": now_utc_iso(),
            "last_updated_utc": None,
        }

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"State JSON is not a JSON object: {path}")

    data.setdefault("version", 1)
    data.setdefault("next_sequence", 1)
    data.setdefault("last_assigned_sequence", int(data.get("next_sequence", 1)) - 1)
    data.setdefault("total_renamed", 0)
    data.setdefault("created_utc", now_utc_iso())
    data.setdefault("last_updated_utc", None)

    if int(data["next_sequence"]) < 1:
        data["next_sequence"] = 1

    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    state["last_updated_utc"] = now_utc_iso()

    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=2)
        f.write("\n")

    tmp.replace(path)


def parse_exif_date(value: Any) -> dt.datetime | None:
    """
    Handles common ExifTool date outputs:
      2026:06:23 12:34:56
      2026:06:23 12:34:56-04:00
      2026:06:23 12:34:56Z
      2026-06-23 12:34:56
      2026-06-23T12:34:56
    """
    if value is None:
        return None

    if isinstance(value, list):
        for item in value:
            parsed = parse_exif_date(item)
            if parsed:
                return parsed
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.startswith("0000:00:00") or text.startswith("0000-00-00"):
        return None

    # Remove subsecond suffix if present.
    text = re.sub(r"\.\d+", "", text)

    # Remove timezone suffix; for this renaming workflow, date and local clock time are enough.
    text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text).strip()

    # Normalize ISO T separator.
    text = text.replace("T", " ")

    patterns = [
        ("%Y:%m:%d %H:%M:%S", text),
        ("%Y-%m-%d %H:%M:%S", text),
        ("%Y:%m:%d", text),
        ("%Y-%m-%d", text),
    ]

    for fmt, candidate in patterns:
        try:
            parsed = dt.datetime.strptime(candidate, fmt)
            if parsed.year < 1900:
                return None
            return parsed
        except ValueError:
            continue

    # Last chance: pull the first full date/time from a longer string.
    match = re.search(
        r"(?P<y>19\d{2}|20\d{2})[:\-](?P<m>\d{2})[:\-](?P<d>\d{2})"
        r"(?:[ T](?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2}))?",
        text,
    )
    if match:
        try:
            return dt.datetime(
                int(match.group("y")),
                int(match.group("m")),
                int(match.group("d")),
                int(match.group("h") or 0),
                int(match.group("mi") or 0),
                int(match.group("s") or 0),
            )
        except ValueError:
            return None

    return None


def choose_best_date(exif_item: dict[str, Any], allow_filemodify_date: bool) -> tuple[dt.datetime | None, str]:
    tags = list(DATE_TAGS)
    if allow_filemodify_date:
        tags.append(FILEMODIFY_TAG)

    for tag in tags:
        if tag in exif_item:
            parsed = parse_exif_date(exif_item.get(tag))
            if parsed:
                return parsed, tag

    return None, ""


def run_exiftool_json(exiftool: Path, files: list[Path], allow_filemodify_date: bool) -> list[dict[str, Any]]:
    """
    Uses an ExifTool argument file to avoid Windows command-line length limits.
    """
    tags = list(DATE_TAGS)
    if allow_filemodify_date:
        tags.append(FILEMODIFY_TAG)

    arg_lines = [
        "-j",
        "-charset",
        "filename=utf8",
        "-api",
        "LargeFileSupport=1",
        "-api",
        "QuickTimeUTC=1",
        "-m",
    ]
    arg_lines.extend(f"-{tag}" for tag in tags)
    arg_lines.extend(str(path) for path in files)

    arg_file_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            suffix=".exiftool_args.txt",
        ) as arg_file:
            arg_file_path = Path(arg_file.name)
            for line in arg_lines:
                arg_file.write(line)
                arg_file.write("\n")

        completed = subprocess.run(
            [str(exiftool), "-@", str(arg_file_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if completed.returncode not in (0, 1):
            raise RuntimeError(
                f"ExifTool failed with exit code {completed.returncode}.\n"
                f"STDERR:\n{completed.stderr.strip()}"
            )

        if not completed.stdout.strip():
            if completed.stderr.strip():
                eprint(f"WARNING: ExifTool returned no JSON. STDERR: {completed.stderr.strip()}")
            return []

        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as ex:
            raise RuntimeError(
                f"Could not parse ExifTool JSON output: {ex}\n"
                f"First 1000 chars:\n{completed.stdout[:1000]}"
            ) from ex

        if not isinstance(data, list):
            raise RuntimeError("ExifTool JSON output was not a list.")

        if completed.stderr.strip():
            # ExifTool can return warnings while still producing usable JSON.
            for line in completed.stderr.strip().splitlines()[:20]:
                eprint(f"ExifTool warning: {line}")

        return data

    finally:
        if arg_file_path and arg_file_path.exists():
            try:
                arg_file_path.unlink()
            except OSError:
                pass


def read_metadata_dates(
    exiftool: Path,
    files: list[Path],
    *,
    batch_size: int,
    allow_filemodify_date: bool,
) -> tuple[list[RenameRecord], list[MissingDateRecord]]:
    dated: list[RenameRecord] = []
    missing: list[MissingDateRecord] = []

    total = len(files)
    processed = 0

    for start in range(0, total, batch_size):
        batch = files[start : start + batch_size]
        json_items = run_exiftool_json(exiftool, batch, allow_filemodify_date=allow_filemodify_date)

        by_source: dict[str, dict[str, Any]] = {}
        for item in json_items:
            source = item.get("SourceFile")
            if source:
                by_source[normalize_path_for_compare(Path(source))] = item

        for path in batch:
            item = by_source.get(normalize_path_for_compare(path), {})
            capture_dt, source_tag = choose_best_date(item, allow_filemodify_date=allow_filemodify_date)

            if capture_dt:
                dated.append(
                    RenameRecord(
                        full_path=path,
                        capture_datetime=capture_dt,
                        date_source=source_tag,
                    )
                )
            else:
                guess, guess_source = guess_date_from_filename(path.name)
                missing.append(
                    MissingDateRecord(
                        full_path=path,
                        reason="No usable date-taken metadata found by ExifTool",
                        date_guess=guess,
                        date_guess_source=guess_source,
                    )
                )

        processed += len(batch)
        print_progress("Reading metadata", processed, total, missing=len(missing))

    clear_progress_line()
    return dated, missing


def validate_ymd(year: int, month: int, day: int) -> str | None:
    try:
        value = dt.date(year, month, day)
        return value.isoformat()
    except ValueError:
        return None


def guess_date_from_filename(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem

    # YYYY-MM-DD, YYYY_MM_DD, YYYY.MM.DD, YYYY MM DD
    match = re.search(
        r"(?<!\d)(?P<y>19\d{2}|20\d{2})[-_. ](?P<m>0?[1-9]|1[0-2])[-_. ](?P<d>0?[1-9]|[12]\d|3[01])(?!\d)",
        stem,
    )
    if match:
        result = validate_ymd(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if result:
            return result, "filename:YYYY-MM-DD style"

    # YYYYMMDD, common in IMG_20260623, PXL_20260623, VID_20260623, Screenshot_20260623
    match = re.search(
        r"(?<!\d)(?P<y>19\d{2}|20\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)",
        stem,
    )
    if match:
        result = validate_ymd(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if result:
            return result, "filename:YYYYMMDD style"

    # MM-DD-YYYY, MM_DD_YYYY, MM.DD.YYYY
    match = re.search(
        r"(?<!\d)(?P<m>0?[1-9]|1[0-2])[-_. ](?P<d>0?[1-9]|[12]\d|3[01])[-_. ](?P<y>19\d{2}|20\d{2})(?!\d)",
        stem,
    )
    if match:
        result = validate_ymd(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if result:
            return result, "filename:MM-DD-YYYY style"

    return "", ""


def write_missing_date_csv(path: Path, records: list[MissingDateRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "Decision",
        "DateToUse",
        "DateGuessSource",
        "ParentDirectory",
        "FullPath",
        "FileName",
        "Extension",
        "Reason",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for record in sorted(records, key=lambda r: normalize_path_for_compare(r.full_path)):
            writer.writerow(
                {
                    "Decision": "",
                    "DateToUse": record.date_guess,
                    "DateGuessSource": record.date_guess_source,
                    "ParentDirectory": str(record.full_path.parent),
                    "FullPath": str(record.full_path),
                    "FileName": record.full_path.name,
                    "Extension": record.full_path.suffix,
                    "Reason": record.reason,
                }
            )


def parse_renamed_filename_date(path: Path) -> dt.date | None:
    """
    Extract YYYY-MM-DD from an already-renamed archive filename.
    CatchUp uses this instead of ExifTool-scanning the completed archive.
    """
    if not looks_already_renamed(path):
        return None

    try:
        return dt.datetime.strptime(path.name[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def scan_catchup_candidates(root: Path) -> tuple[list[Path], dict[str, list[dt.date]], int]:
    """
    Filesystem-only scan for CatchUp.

    Returns:
      pending_files: supported files not already renamed
      folder_dates: parent folder path -> dates parsed from already-renamed files in that folder
      renamed_date_count: count of already-renamed dated files found
    """
    pending_files: list[Path] = []
    folder_dates: dict[str, list[dt.date]] = {}
    renamed_date_count = 0
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        path = Path(entry.path)
                        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                            continue

                        if looks_already_renamed(path):
                            parsed_date = parse_renamed_filename_date(path)
                            if parsed_date:
                                key = normalize_path_for_compare(path.parent)
                                folder_dates.setdefault(key, []).append(parsed_date)
                                renamed_date_count += 1
                            continue

                        pending_files.append(path)
                    except OSError as ex:
                        eprint(f"WARNING: Could not inspect {entry.path}: {ex}")
        except OSError as ex:
            eprint(f"WARNING: Could not scan {current}: {ex}")

    pending_files.sort(key=lambda p: normalize_path_for_compare(p))
    return pending_files, folder_dates, renamed_date_count


def folder_date_summary(dates: list[dt.date]) -> tuple[int, dt.date | None, dt.date | None, int, dt.date | None]:
    if not dates:
        return 0, None, None, 0, None

    ordered = sorted(dates)
    low = ordered[0]
    high = ordered[-1]
    span_days = (high - low).days
    chosen = ordered[len(ordered) // 2]  # median date, safer than arithmetic average
    return len(ordered), low, high, span_days, chosen


def infer_catchup_records_from_folder_dates(
    missing: list[MissingDateRecord],
    folder_dates: dict[str, list[dt.date]],
    *,
    min_folder_dates: int,
    max_folder_date_span_days: int,
) -> tuple[list[RenameRecord], list[MissingDateRecord]]:
    """
    For files with no ExifTool date, infer a guarded fallback date from
    already-renamed files in the same immediate directory.

    Safety rules:
      - same directory only
      - requires at least min_folder_dates already-renamed dated files
      - requires folder date span <= max_folder_date_span_days
      - uses median date to resist outliers
    """
    inferred: list[RenameRecord] = []
    still_missing: list[MissingDateRecord] = []

    min_folder_dates = max(1, int(min_folder_dates))
    max_folder_date_span_days = max(0, int(max_folder_date_span_days))

    for record in missing:
        key = normalize_path_for_compare(record.full_path.parent)
        dates = folder_dates.get(key, [])
        count, low, high, span_days, chosen = folder_date_summary(dates)

        if count >= min_folder_dates and chosen and span_days <= max_folder_date_span_days:
            inferred_dt = dt.datetime.combine(chosen, dt.time(12, 0, 0))
            source = (
                f"CatchUpFolderMedian:"
                f"count={count};min={low.isoformat() if low else ''};"
                f"max={high.isoformat() if high else ''};spanDays={span_days}"
            )
            inferred.append(
                RenameRecord(
                    full_path=record.full_path,
                    capture_datetime=inferred_dt,
                    date_source=source,
                    reason="CatchUp inferred date from already-renamed files in the same folder.",
                )
            )
            continue

        if count == 0:
            reason = (
                "CatchUp could not infer date: no already-renamed dated files found in the same folder. "
                + record.reason
            )
            date_guess = record.date_guess
            date_guess_source = record.date_guess_source
        elif count < min_folder_dates:
            reason = (
                f"CatchUp could not infer date confidently: same folder has only {count} renamed dated file(s); "
                f"minimum required is {min_folder_dates}. "
                + record.reason
            )
            date_guess = chosen.isoformat() if chosen else record.date_guess
            date_guess_source = (
                f"CatchUpFolderMedianNotApplied:count={count};minRequired={min_folder_dates}"
                if chosen else record.date_guess_source
            )
        else:
            reason = (
                f"CatchUp could not infer date confidently: same folder date span is {span_days} day(s), "
                f"maximum allowed is {max_folder_date_span_days}. "
                + record.reason
            )
            date_guess = chosen.isoformat() if chosen else record.date_guess
            date_guess_source = (
                f"CatchUpFolderMedianNotApplied:count={count};"
                f"min={low.isoformat() if low else ''};max={high.isoformat() if high else ''};"
                f"spanDays={span_days};maxAllowed={max_folder_date_span_days}"
            )

        still_missing.append(
            MissingDateRecord(
                full_path=record.full_path,
                reason=reason,
                date_guess=date_guess,
                date_guess_source=date_guess_source,
            )
        )

    return inferred, still_missing


def run_catchup_mode(
    args: argparse.Namespace,
    *,
    root: Path,
    exiftool: Path,
    state_json: Path,
    rename_log_csv: Path,
    tag: str,
) -> int:
    print(f"Root folder:      {root}")
    if not root.exists():
        print(f"ERROR: Root folder not found: {root}", file=sys.stderr)
        return 2

    min_folder_dates = max(1, int(args.catchup_min_folder_dates))
    max_span_days = max(0, int(args.catchup_max_folder_date_span_days))
    catchup_review_csv = Path(args.catchup_review_csv)

    print("CatchUp mode:     Enabled")
    print("CatchUp scan:     Filesystem scan first; ExifTool only checks non-renamed supported files.")
    print(f"Min folder dates: {min_folder_dates}")
    print(f"Max folder span:  {max_span_days} day(s)")
    print(f"CatchUp CSV:      {catchup_review_csv}")
    print("")

    print("Scanning for non-renamed supported files and same-folder date context...")
    pending_files, folder_dates, renamed_date_count = scan_catchup_candidates(root)

    print(f"Already-renamed dated files used for folder context: {renamed_date_count:,}")
    print(f"Folders with date context:                          {len(folder_dates):,}")
    print(f"Non-renamed supported files found:                  {len(pending_files):,}")

    if not pending_files:
        print("No non-renamed supported files found. CatchUp has nothing to do.")
        return 0

    print("")
    print("Reading ExifTool metadata only for non-renamed supported files...")
    exif_dated, missing = read_metadata_dates(
        exiftool,
        pending_files,
        batch_size=max(1, args.batch_size),
        allow_filemodify_date=args.allow_filemodifydate,
    )

    inferred, still_missing = infer_catchup_records_from_folder_dates(
        missing,
        folder_dates,
        min_folder_dates=min_folder_dates,
        max_folder_date_span_days=max_span_days,
    )

    records_to_rename = exif_dated + inferred

    print(f"CatchUp files with ExifTool date:        {len(exif_dated):,}")
    print(f"CatchUp files inferred from folder:      {len(inferred):,}")
    print(f"CatchUp files still needing review:      {len(still_missing):,}")

    if still_missing:
        if args.whatif and not args.write_missing_csv_in_whatif:
            print(
                f"WHATIF: CatchUp review CSV was not written. "
                f"Use -WriteMissingCsvInWhatIf to create it during a dry run."
            )
            print(f"CSV path would be: {catchup_review_csv}")
        else:
            write_missing_date_csv(catchup_review_csv, still_missing)
            print(f"CatchUp review CSV written: {catchup_review_csv}")
            print("For that CSV: set Decision to CONFIRM or DENY. You may edit DateToUse before CONFIRM.")

    if not records_to_rename:
        print("No CatchUp files are ready to rename.")
        return 0

    renamed, skipped, errors, _process_results = rename_records(
        records_to_rename,
        state_path=state_json,
        log_path=rename_log_csv,
        tag=tag,
        whatif=args.whatif,
    )

    print("")
    print("CatchUp Summary")
    print(f"{'Would rename' if args.whatif else 'Renamed'}: {renamed:,}")
    print(f"  From ExifTool date:     {len(exif_dated):,}")
    print(f"  From folder median:     {len(inferred):,}")
    print(f"Still needs review:       {len(still_missing):,}")
    print(f"Skipped:                  {skipped:,}")
    print(f"Errors:                   {errors:,}")

    if args.whatif:
        print("No files were renamed and the JSON counter was not updated.")
    else:
        print(f"State saved:  {state_json}")
        print(f"Log saved:    {rename_log_csv}")

    return 0 if errors == 0 else 1



def parse_review_date(value: str) -> dt.datetime | None:
    """
    Accepts dates commonly produced by Excel/CSV editing, including:
      YYYY-MM-DD
      YYYY/MM/DD
      YYYY.MM.DD
      YYYY-MM-DD HH:MM[:SS]
      M/D/YYYY
      MM/DD/YYYY
      M-D-YYYY
      MM-DD-YYYY
      M/D/YYYY H:MM AM/PM
      Excel serial dates, e.g. 45000

    If only a date is supplied, midnight is used for metadata time.
    """
    text = (value or "").strip()
    if not text:
        return None

    # Remove wrapping quotes and normalize whitespace.
    text = text.strip().strip('"').strip("'")
    text = re.sub(r"\s+", " ", text)

    # First try the Exif/ISO parser already used by the script.
    parsed = parse_exif_date(text)
    if parsed:
        return parsed

    # Excel serial date support. Excel's Windows date system starts at 1899-12-30.
    # Only accept a reasonable photo-era serial range to avoid misreading years.
    if re.fullmatch(r"\d{4,6}(?:\.0+)?", text):
        try:
            serial = int(float(text))
            if 20000 <= serial <= 80000:
                return dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)
        except Exception:
            pass

    formats = [
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %I:%M:%S %p",
        "%m-%d-%Y",
        "%m-%d-%Y %H:%M",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y %I:%M %p",
        "%m-%d-%Y %I:%M:%S %p",
        "%m.%d.%Y",
        "%m.%d.%Y %H:%M",
        "%m.%d.%Y %H:%M:%S",
        "%m.%d.%Y %I:%M %p",
        "%m.%d.%Y %I:%M:%S %p",
    ]

    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if 1900 <= parsed.year <= 2100:
                return parsed
        except ValueError:
            continue

    # Regex fallback for M/D/YYYY or YYYY/M/D with optional time.
    patterns = [
        # YYYY-M-D optional time
        r"^(?P<y>19\d{2}|20\d{2})[-_/\.](?P<m>0?[1-9]|1[0-2])[-_/\.](?P<d>0?[1-9]|[12]\d|3[01])(?:\s+(?P<time>.+))?$",
        # M-D-YYYY optional time
        r"^(?P<m>0?[1-9]|1[0-2])[-_/\.](?P<d>0?[1-9]|[12]\d|3[01])[-_/\.](?P<y>19\d{2}|20\d{2})(?:\s+(?P<time>.+))?$",
    ]

    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue

        try:
            year = int(match.group("y"))
            month = int(match.group("m"))
            day = int(match.group("d"))
            base = dt.datetime(year, month, day)
        except ValueError:
            continue

        time_text = (match.groupdict().get("time") or "").strip()
        if not time_text:
            return base

        # Optional time parsing if present.
        for fmt in ["%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"]:
            try:
                t = dt.datetime.strptime(time_text.upper(), fmt).time()
                return dt.datetime.combine(base.date(), t)
            except ValueError:
                continue

        return base

    return None


def records_from_review_csv(path: Path) -> tuple[list[RenameRecord], int, int, list[str]]:
    records: list[RenameRecord] = []
    denied = 0
    pending = 0
    errors: list[str] = []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])

        required = {"Decision", "DateToUse", "FullPath"}
        missing_headers = sorted(required - headers)
        if missing_headers:
            raise ValueError(f"Review CSV is missing required column(s): {', '.join(missing_headers)}")

        for line_no, row in enumerate(reader, start=2):
            decision = (row.get("Decision") or "").strip().upper()
            full_path = (row.get("FullPath") or "").strip()
            date_to_use = (row.get("DateToUse") or "").strip()

            if decision in {"DENY", "DENIED", "NO", "N"}:
                denied += 1
                continue

            if decision not in {"CONFIRM", "CONFIRMED", "YES", "Y"}:
                pending += 1
                continue

            if not full_path:
                errors.append(f"Line {line_no}: CONFIRM row has no FullPath.")
                continue

            parsed_dt = parse_review_date(date_to_use)
            if not parsed_dt:
                errors.append(f"Line {line_no}: CONFIRM row has invalid DateToUse: {date_to_use!r}.")
                continue

            file_path = Path(full_path)
            if not file_path.exists():
                errors.append(f"Line {line_no}: File no longer exists: {file_path}")
                continue

            if not is_supported_file(file_path):
                errors.append(f"Line {line_no}: Unsupported or non-file path: {file_path}")
                continue

            records.append(
                RenameRecord(
                    full_path=file_path,
                    capture_datetime=parsed_dt,
                    date_source=f"ReviewCsv:{path.name}",
                    reason="Manual review CSV confirmed",
                )
            )

    return records, denied, pending, errors


def format_exiftool_date(value: dt.datetime) -> str:
    """ExifTool writable date format."""
    return value.strftime("%Y:%m:%d %H:%M:%S")


def metadata_tags_for_file(path: Path, include_filesystem_dates: bool) -> list[str]:
    suffix = path.suffix.lower()

    if suffix in VIDEO_EXTENSIONS:
        tags = [
            "CreateDate",
            "ModifyDate",
            "MediaCreateDate",
            "MediaModifyDate",
            "TrackCreateDate",
            "TrackModifyDate",
            "QuickTime:CreateDate",
            "QuickTime:ModifyDate",
            "Keys:CreationDate",
        ]
    else:
        tags = [
            "DateTimeOriginal",
            "CreateDate",
            "ModifyDate",
        ]

    if include_filesystem_dates:
        tags.extend(["FileCreateDate", "FileModifyDate"])

    # Preserve order but remove duplicates.
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag not in seen:
            result.append(tag)
            seen.add(tag)
    return result


def set_file_dates_with_exiftool(
    exiftool: Path,
    path: Path,
    capture_datetime: dt.datetime,
    *,
    include_filesystem_dates: bool,
    keep_exiftool_backups: bool,
    whatif: bool,
) -> tuple[str, str]:
    """
    Set/update date metadata using ExifTool.

    Returns (status, message). Status values:
      NOT_REQUESTED, WOULD_SET, SET, ERROR_SET
    """
    date_text = format_exiftool_date(capture_datetime)

    unsupported_metadata = path.suffix.lower() in METADATA_WRITE_UNSUPPORTED_EXTENSIONS

    if unsupported_metadata and not include_filesystem_dates:
        if whatif:
            return (
                "WOULD_SKIP_METADATA_UNSUPPORTED",
                f"Would skip metadata write for unsupported {path.suffix.lower()} file; rename can still proceed."
            )
        return (
            "SKIPPED_METADATA_UNSUPPORTED",
            f"ExifTool cannot write date metadata to {path.suffix.lower()} files; rename proceeded without metadata update."
        )

    if unsupported_metadata and include_filesystem_dates:
        # For legacy MPG/MPEG, do not try to write container metadata tags.
        # Only attempt filesystem pseudo-tags when explicitly requested.
        tags = ["FileCreateDate", "FileModifyDate"]
        filesystem_only = True
    else:
        tags = metadata_tags_for_file(path, include_filesystem_dates=include_filesystem_dates)
        filesystem_only = False

    if whatif:
        if filesystem_only:
            return (
                "WOULD_SET_FILESYSTEM_ONLY",
                f"Would set filesystem date tag(s) only to {date_text}; metadata unsupported for {path.suffix.lower()}."
            )
        return "WOULD_SET", f"Would set {len(tags)} date tag(s) to {date_text}"

    args = [
        str(exiftool),
        "-api",
        "QuickTimeUTC=1",
        "-m",
    ]

    # Avoid creating thousands of *_original backup files by default.
    if not keep_exiftool_backups:
        args.append("-overwrite_original")

    # Preserve filesystem modified time unless the user explicitly asks to set filesystem dates.
    if not include_filesystem_dates:
        args.append("-P")

    for tag in tags:
        args.append(f"-{tag}={date_text}")

    args.append(str(path))

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    message = " | ".join(part for part in [stdout, stderr] if part).strip()

    # ExifTool may return 1 for warnings. Treat it as success if output indicates a file was updated/unchanged.
    updated = (
        "1 image files updated" in stdout
        or "1 image files unchanged" in stdout
        or "1 files updated" in stdout
        or "1 file updated" in stdout
        or "1 files unchanged" in stdout
        or "1 file unchanged" in stdout
    )
    if completed.returncode == 0 or updated:
        if filesystem_only:
            return "SET_FILESYSTEM_ONLY", message or f"Set filesystem dates to {date_text}; metadata unsupported for this file type."
        return "SET", message or f"Set date metadata to {date_text}"

    if filesystem_only:
        return "ERROR_SET_FILESYSTEM_ONLY", message or f"ExifTool failed to set filesystem dates with exit code {completed.returncode}"

    return "ERROR_SET", message or f"ExifTool failed with exit code {completed.returncode}"


def default_remaining_csv_path(review_csv: Path) -> Path:
    return review_csv.with_name(review_csv.stem + f"_remaining_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")


def write_remaining_review_csv(
    source_csv: Path,
    remaining_csv: Path,
    results: list[ProcessResult],
    *,
    whatif: bool,
) -> tuple[int, Path]:
    """
    Writes a new CSV containing rows that still need attention:
      - pending / blank decisions
      - denied rows
      - confirmed rows that failed or were not processed
      - confirmed rows where metadata update failed

    Confirmed rows that are successfully processed are removed from this remaining CSV.
    """
    result_by_source = {normalize_path_for_compare(r.source): r for r in results}

    with source_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]

    extra_fields = [
        "ProcessStatus",
        "ProcessNote",
        "NewPath",
        "MetadataStatus",
        "MetadataNote",
        "ProcessedUtc",
    ]

    fieldnames = list(original_fieldnames)
    for name in extra_fields:
        if name not in fieldnames:
            fieldnames.append(name)

    remaining_rows: list[dict[str, str]] = []

    for row in rows:
        decision = (row.get("Decision") or "").strip().upper()
        full_path = (row.get("FullPath") or "").strip()
        result = result_by_source.get(normalize_path_for_compare(Path(full_path))) if full_path else None

        include = False
        process_status = ""
        process_note = ""
        new_path = ""
        metadata_status = ""
        metadata_note = ""

        if decision in {"DENY", "DENIED", "NO", "N"}:
            include = True
            process_status = "DENIED"
            process_note = "User denied this row; no action taken."
        elif decision not in {"CONFIRM", "CONFIRMED", "YES", "Y"}:
            include = True
            process_status = "PENDING"
            process_note = "Decision was blank or not CONFIRM/DENY."
        elif not result:
            include = True
            process_status = "CONFIRM_NOT_PROCESSED"
            process_note = "Row was marked CONFIRM but was not processed; check console errors."
        else:
            process_status = result.status
            process_note = result.message
            new_path = str(result.target or "")
            metadata_status = result.metadata_status
            metadata_note = result.metadata_message

            if not result.success:
                include = True

                # If the file was renamed but metadata failed, update FullPath so a future pass can target the new file.
                if result.target and result.target.exists():
                    row["FullPath"] = str(result.target)
                    row["ParentDirectory"] = str(result.target.parent)
                    row["FileName"] = result.target.name
                    row["Extension"] = result.target.suffix

        if include:
            row["ProcessStatus"] = process_status
            row["ProcessNote"] = process_note
            row["NewPath"] = new_path
            row["MetadataStatus"] = metadata_status
            row["MetadataNote"] = metadata_note
            row["ProcessedUtc"] = now_utc_iso()
            remaining_rows.append(row)

    if whatif:
        return len(remaining_rows), remaining_csv

    remaining_csv.parent.mkdir(parents=True, exist_ok=True)
    with remaining_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(remaining_rows)

    return len(remaining_rows), remaining_csv


def next_available_target(
    source: Path,
    capture_datetime: dt.datetime,
    state: dict[str, Any],
    tag: str,
) -> tuple[Path, int]:
    capture_date = capture_datetime.date().isoformat()
    extension = source.suffix.lower()
    media_tag = media_tag_for_file(source, fallback_tag=tag)

    while True:
        sequence = int(state["next_sequence"])
        target_name = f"{capture_date}_{sequence:010d}_{media_tag}{extension}"
        target = source.with_name(target_name)

        same_file = normalize_path_for_compare(source) == normalize_path_for_compare(target)
        if same_file:
            state["next_sequence"] = sequence + 1
            continue

        if not target.exists():
            return target, sequence

        # Collision protection. This should be rare because the sequence is global.
        state["next_sequence"] = sequence + 1


def append_rename_log(log_path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    exists = log_path.exists()

    fieldnames = [
        "TimestampUtc",
        "Sequence",
        "DateSource",
        "OriginalPath",
        "NewPath",
        "OriginalFileName",
        "NewFileName",
        "MetadataStatus",
        "MetadataMessage",
    ]

    with log_path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def rename_records(
    records: list[RenameRecord],
    *,
    state_path: Path,
    log_path: Path,
    tag: str,
    whatif: bool,
    exiftool: Path | None = None,
    set_metadata_dates: bool = False,
    set_filesystem_dates: bool = False,
    keep_exiftool_backups: bool = False,
) -> tuple[int, int, int, list[ProcessResult]]:
    state = load_state(state_path)

    # Process by actual capture date/time so the sequence counter follows oldest-first order.
    records = sorted(
        records,
        key=lambda r: (
            r.capture_datetime,
            normalize_path_for_compare(r.full_path),
        ),
    )

    renamed = 0
    skipped = 0
    errors = 0
    log_rows: list[dict[str, Any]] = []
    results: list[ProcessResult] = []

    total = len(records)

    for index, record in enumerate(records, start=1):
        source = record.full_path
        result = ProcessResult(source=source)

        if not source.exists():
            clear_progress_line()
            print(f"ERROR   | Missing source file: {source}")
            result.status = "ERROR_MISSING_SOURCE"
            result.message = "Source file does not exist."
            results.append(result)
            errors += 1
            print_progress("Renaming", index, total, renamed=renamed, skipped=skipped, errors=errors)
            continue

        try:
            if set_metadata_dates:
                if not exiftool:
                    result.metadata_status = "ERROR_SET"
                    result.metadata_message = "ExifTool path was not supplied."
                else:
                    meta_action = "WHATIF META" if whatif else "META   "
                    result.metadata_status, result.metadata_message = set_file_dates_with_exiftool(
                        exiftool,
                        source,
                        record.capture_datetime,
                        include_filesystem_dates=set_filesystem_dates,
                        keep_exiftool_backups=keep_exiftool_backups,
                        whatif=whatif,
                    )
                    clear_progress_line()
                    print(
                        f"{meta_action} | {source}"
                        f" | date={record.capture_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                        f" | status={result.metadata_status}"
                    )

            if looks_already_renamed(source):
                clear_progress_line()
                print(f"SKIP    | Already matches rename pattern: {source}")
                result.status = "SKIPPED_ALREADY_RENAMED"
                result.message = "File already matches the final rename pattern."
                skipped += 1
                results.append(result)
                print_progress("Renaming", index, total, renamed=renamed, skipped=skipped, errors=errors)
                continue

            target, sequence = next_available_target(source, record.capture_datetime, state, tag)
            result.target = target
            result.sequence = sequence

            action = "WHATIF " if whatif else "RENAME "
            clear_progress_line()
            print(
                f"{action} | {source} -> {target}"
                f" | date={record.capture_datetime.date().isoformat()}"
                f" | source={record.date_source}"
            )

            if not whatif:
                source.rename(target)
                state["next_sequence"] = sequence + 1
                state["last_assigned_sequence"] = sequence
                state["total_renamed"] = int(state.get("total_renamed", 0)) + 1
                renamed += 1
                result.status = "RENAMED"
                result.message = "File renamed successfully."

                log_rows.append(
                    {
                        "TimestampUtc": now_utc_iso(),
                        "Sequence": sequence,
                        "DateSource": record.date_source,
                        "OriginalPath": str(source),
                        "NewPath": str(target),
                        "OriginalFileName": source.name,
                        "NewFileName": target.name,
                        "MetadataStatus": result.metadata_status,
                        "MetadataMessage": result.metadata_message,
                    }
                )

                # Checkpoint every 25 renames so the counter survives interruptions.
                if renamed % 25 == 0:
                    save_state(state_path, state)
                    append_rename_log(log_path, log_rows)
                    log_rows.clear()
            else:
                # Advance only the in-memory counter so WhatIf shows realistic names.
                state["next_sequence"] = sequence + 1
                state["last_assigned_sequence"] = sequence
                renamed += 1
                result.status = "WOULD_RENAME"
                result.message = "WhatIf preview only; no rename performed."

            results.append(result)

        except Exception as ex:
            clear_progress_line()
            print(f"ERROR   | Failed to process {source}: {ex}")
            result.status = "ERROR_PROCESSING"
            result.message = str(ex)
            results.append(result)
            errors += 1

        print_progress("Renaming", index, total, renamed=renamed, skipped=skipped, errors=errors)

    clear_progress_line()

    if not whatif:
        save_state(state_path, state)
        append_rename_log(log_path, log_rows)

    return renamed, skipped, errors, results

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rename photos and videos in place using ExifTool date-taken metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-Root",
        "--root",
        default=DEFAULT_ROOT,
        help="Root folder containing photos/videos to scan recursively.",
    )
    parser.add_argument(
        "-ExifTool",
        "--exiftool",
        default=DEFAULT_EXIFTOOL,
        help="Path to exiftool.exe.",
    )
    parser.add_argument(
        "-StateJson",
        "--state-json",
        default=str(DEFAULT_STATE_JSON),
        help="JSON counter/state file. Defaults to the script folder.",
    )
    parser.add_argument(
        "-MissingDateCsv",
        "--missing-date-csv",
        default=str(DEFAULT_MISSING_DATE_CSV),
        help="CSV output for files with no usable date metadata.",
    )
    parser.add_argument(
        "-RenameLogCsv",
        "--rename-log-csv",
        default=str(DEFAULT_RENAME_LOG_CSV),
        help="Append-only log of completed renames.",
    )
    parser.add_argument(
        "-ReviewCsv",
        "--review-csv",
        default="",
        help="CSV created by this script. Rows with Decision=CONFIRM will be renamed using DateToUse.",
    )
    parser.add_argument(
        "-CatchUp",
        "--catchup",
        action="store_true",
        help="Only process non-renamed supported files. ExifTool-checks those files only; if no date is found, infer a guarded fallback date from already-renamed files in the same folder.",
    )
    parser.add_argument(
        "-CatchUpMinFolderDates",
        "--catchup-min-folder-dates",
        type=int,
        default=3,
        help="CatchUp: minimum number of already-renamed dated files required in the same folder before using the folder median date.",
    )
    parser.add_argument(
        "-CatchUpMaxFolderDateSpanDays",
        "--catchup-max-folder-date-span-days",
        type=int,
        default=45,
        help="CatchUp: maximum allowed span between earliest/latest same-folder renamed dates before refusing automatic folder-date inference.",
    )
    parser.add_argument(
        "-CatchUpReviewCsv",
        "--catchup-review-csv",
        default=str(DEFAULT_CATCHUP_REVIEW_CSV),
        help="CatchUp: CSV output for files that still cannot be confidently dated.",
    )
    parser.add_argument(
        "-SetMetadataDatesOnConfirm",
        "--set-metadata-dates-on-confirm",
        action="store_true",
        help="When processing -ReviewCsv, set/update date metadata using DateToUse for CONFIRM rows before renaming.",
    )
    parser.add_argument(
        "-SetFileSystemDates",
        "--set-filesystem-dates",
        action="store_true",
        help="With -SetMetadataDatesOnConfirm, also set FileCreateDate and FileModifyDate. Off by default.",
    )
    parser.add_argument(
        "-KeepExifToolBackups",
        "--keep-exiftool-backups",
        action="store_true",
        help="Let ExifTool create *_original backup files when writing metadata. Default is overwrite_original to avoid extra files.",
    )
    parser.add_argument(
        "-RemainingCsv",
        "--remaining-csv",
        default="",
        help="When processing -ReviewCsv, write pending/denied/failed rows to this CSV. Defaults beside the input review CSV.",
    )
    parser.add_argument(
        "-WhatIf",
        "--whatif",
        "--dry-run",
        action="store_true",
        help="Preview changes without renaming files, updating state, or writing logs.",
    )
    parser.add_argument(
        "-Tag",
        "--tag",
        default=DEFAULT_TAG,
        help="Fallback filename suffix tag for unknown supported types. Images use IMG and videos use VID automatically.",
    )
    parser.add_argument(
        "-BatchSize",
        "--batch-size",
        type=int,
        default=200,
        help="Number of files sent to ExifTool per batch.",
    )
    parser.add_argument(
        "-IncludeAlreadyRenamed",
        "--include-already-renamed",
        action="store_true",
        help="Include files already matching YYYY-MM-DD_0000000001_TAG.ext pattern.",
    )
    parser.add_argument(
        "-AllowFileModifyDate",
        "--allow-filemodifydate",
        action="store_true",
        help="Allow ExifTool FileModifyDate as a last-resort date source. Off by default.",
    )
    parser.add_argument(
        "-WriteMissingCsvInWhatIf",
        "--write-missing-csv-in-whatif",
        action="store_true",
        help="Write the missing-date CSV even when -WhatIf is used.",
    )

    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    root = Path(args.root)
    exiftool = Path(args.exiftool)
    state_json = Path(args.state_json)
    missing_date_csv = Path(args.missing_date_csv)
    rename_log_csv = Path(args.rename_log_csv)
    tag = re.sub(r"[^A-Za-z0-9-]", "", args.tag.strip()) or DEFAULT_TAG

    print("Photo/Video ExifTool Rename")
    print(f"Mode:             {'WHATIF / dry run' if args.whatif else 'LIVE rename'}")
    print(f"ExifTool:         {exiftool}")
    print(f"State JSON:       {state_json}")
    print(f"Rename log CSV:   {rename_log_csv}")
    print(f"Filename format:  YYYY-MM-DD_0000000001_IMG.ext or YYYY-MM-DD_0000000001_VID.ext")
    print(f"Skip already renamed files: {'No' if args.include_already_renamed else 'Yes'}")
    print(f"CatchUp mode:     {'Yes' if args.catchup else 'No'}")
    if args.review_csv:
        print(f"Set metadata dates on CONFIRM: {'Yes' if args.set_metadata_dates_on_confirm else 'No'}")
        print(f"Set filesystem dates:          {'Yes' if args.set_filesystem_dates else 'No'}")
        if args.set_metadata_dates_on_confirm:
            print("Unsupported metadata write types: MPG/MPEG will be renamed but metadata write will be skipped unless filesystem dates are requested.")
    print("")

    if not exiftool.exists():
        print(f"ERROR: ExifTool not found: {exiftool}", file=sys.stderr)
        return 2

    if args.catchup and args.review_csv:
        print("ERROR: -CatchUp and -ReviewCsv are separate modes. Run one mode at a time.", file=sys.stderr)
        return 2

    try:
        if args.review_csv:
            review_csv = Path(args.review_csv)
            print(f"Review CSV:       {review_csv}")
            if not review_csv.exists():
                print(f"ERROR: Review CSV not found: {review_csv}", file=sys.stderr)
                return 2

            records, denied, pending, review_errors = records_from_review_csv(review_csv)
            print(f"Review CSV rows ready to rename: {len(records):,}")
            print(f"Review CSV denied rows:          {denied:,}")
            print(f"Review CSV pending/blank rows:   {pending:,}")

            for err in review_errors:
                print(f"CSV ERROR | {err}")
            if review_errors:
                print(f"Review CSV errors:               {len(review_errors):,}")

            if not records:
                print("No CONFIRM rows with valid files/dates to process.")
                return 0

            renamed, skipped, errors, process_results = rename_records(
                records,
                state_path=state_json,
                log_path=rename_log_csv,
                tag=tag,
                whatif=args.whatif,
                exiftool=exiftool,
                set_metadata_dates=args.set_metadata_dates_on_confirm,
                set_filesystem_dates=args.set_filesystem_dates,
                keep_exiftool_backups=args.keep_exiftool_backups,
            )

            remaining_csv = Path(args.remaining_csv) if args.remaining_csv else default_remaining_csv_path(review_csv)
            remaining_count, remaining_path = write_remaining_review_csv(
                review_csv,
                remaining_csv,
                process_results,
                whatif=args.whatif,
            )

            print("")
            print("Summary")
            print(f"{'Would rename' if args.whatif else 'Renamed'}: {renamed:,}")
            print(f"Skipped: {skipped:,}")
            print(f"Errors:  {errors + len(review_errors):,}")
            print(f"Remaining rows needing attention: {remaining_count:,}")
            if args.whatif:
                print(f"WHATIF: Remaining CSV was not written. Path would be: {remaining_path}")
            else:
                print(f"Remaining CSV written: {remaining_path}")
            return 0 if errors == 0 and not review_errors else 1

        if args.catchup:
            return run_catchup_mode(
                args,
                root=root,
                exiftool=exiftool,
                state_json=state_json,
                rename_log_csv=rename_log_csv,
                tag=tag,
            )

        print(f"Root folder:      {root}")
        if not root.exists():
            print(f"ERROR: Root folder not found: {root}", file=sys.stderr)
            return 2

        print("Scanning for supported image/video files...")
        files = scan_supported_files(root, include_already_renamed=args.include_already_renamed)
        print(f"Scan complete. Supported files found: {len(files):,}")

        if not files:
            print("No supported files found.")
            return 0

        dated, missing = read_metadata_dates(
            exiftool,
            files,
            batch_size=max(1, args.batch_size),
            allow_filemodify_date=args.allow_filemodifydate,
        )

        print(f"Files with usable date metadata: {len(dated):,}")
        print(f"Files missing usable date:       {len(missing):,}")

        if missing:
            if args.whatif and not args.write_missing_csv_in_whatif:
                print(
                    f"WHATIF: Missing-date CSV was not written. "
                    f"Use -WriteMissingCsvInWhatIf to create it during a dry run."
                )
                print(f"CSV path would be: {missing_date_csv}")
            else:
                write_missing_date_csv(missing_date_csv, missing)
                print(f"Missing-date review CSV written: {missing_date_csv}")
                print("For that CSV: set Decision to CONFIRM or DENY. You may edit DateToUse before CONFIRM.")

        if not dated:
            print("No files with usable metadata dates to rename.")
            return 0

        renamed, skipped, errors, _process_results = rename_records(
            dated,
            state_path=state_json,
            log_path=rename_log_csv,
            tag=tag,
            whatif=args.whatif,
        )

        print("")
        print("Summary")
        print(f"{'Would rename' if args.whatif else 'Renamed'}: {renamed:,}")
        print(f"Missing date: {len(missing):,}")
        print(f"Skipped:      {skipped:,}")
        print(f"Errors:       {errors:,}")

        if args.whatif:
            print("No files were renamed and the JSON counter was not updated.")
        else:
            print(f"State saved:  {state_json}")
            print(f"Log saved:    {rename_log_csv}")

        return 0 if errors == 0 else 1

    except KeyboardInterrupt:
        clear_progress_line()
        print("Interrupted by user.")
        return 130
    except Exception as ex:
        clear_progress_line()
        print(f"ERROR: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
