#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Find-SimilarVideos-ReviewDelete.py

Compressed/similar video duplicate finder and confirmed-delete processor.

This is NOT an exact-only duplicate finder. It is designed for the case where
duplicate videos may have been compressed, re-encoded, or copied in a way that
changes file size, codec, bitrate, SHA-256, and Date Modified.

What it uses:
  - FFprobe: duration, width, height
  - FFmpeg: sampled video frames
  - Perceptual frame hashes: visual similarity across sampled frames
  - Duration/length closeness: used as a strong confidence factor
  - Date Modified closeness: included as a weak confidence boost / audit field,
    but never used by itself to declare duplicates

What it produces:
  - review CSV with SuggestedAction=KEEP/DELETE
  - pair CSV showing why files matched
  - inventory CSV
  - summary CSV

Confirmed delete behavior:
  - Only deletes rows where:
        SuggestedAction = DELETE
        ConfirmDelete   = CONFIRM
  - Sends video to Recycle Bin by default.
  - Also sends the matching sidecar to Recycle Bin if it exists:
        video_file.ext.xmp
  - Refuses to delete every file in a group unless -AllowDeleteWholeGroup is used.

Default paths use generic public examples. Change these constants or pass -Root / -OutputFolder for your environment:
  Root:         D:\MediaArchive\Photos and Videos
  OutputFolder: duplicate_reports beside this script
  CacheDb:      similar_video_hash_cache.sqlite beside this script

Requirements:
  pip install Pillow send2trash

  FFmpeg and FFprobe must be available in PATH or passed explicitly:
    -FFmpeg "C:\path\to\ffmpeg.exe"
    -FFprobe "C:\path\to\ffprobe.exe"

Examples:
  # Analyze only, no deletion
  python Find-SimilarVideos-ReviewDelete.py -Root "D:\MediaArchive\Photos and Videos"

  # Analyze using explicit FFmpeg paths
  python Find-SimilarVideos-ReviewDelete.py `
    -Root "D:\MediaArchive\Photos and Videos" `
    -FFmpeg "C:\ffmpeg\bin\ffmpeg.exe" `
    -FFprobe "C:\ffmpeg\bin\ffprobe.exe"

  # Preview delete processing
  python Find-SimilarVideos-ReviewDelete.py -Process -Csv ".\duplicate_reports\<reviewed.csv>" -WhatIf

  # Live delete processing
  python Find-SimilarVideos-ReviewDelete.py -Process -Csv ".\duplicate_reports\<reviewed.csv>"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


DEFAULT_ROOT = r"D:\MediaArchive\Photos and Videos"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_FOLDER = SCRIPT_DIR / "duplicate_reports"
DEFAULT_CACHE_DB = SCRIPT_DIR / "similar_video_hash_cache.sqlite"

SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".qt",
    ".mpg", ".mpeg", ".mpe",
    ".avi", ".wmv", ".asf",
    ".mkv", ".webm",
    ".3gp", ".3g2",
    ".mts", ".m2ts", ".ts",
    ".mod", ".tod",
}

RENAMED_VIDEO_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<seq>\d{10})_VID\.(?P<ext>[^.]+)$",
    re.IGNORECASE,
)

DEFAULT_SKIP_DIR_NAMES = {
    "duplicate_reports",
    "thumbnail_cache",
    ".git",
    "__pycache__",
}

DEFAULT_SAMPLE_POINTS = "0.10,0.25,0.50,0.75,0.90"
HASH_VERSION = "dhash64_v1"

REVIEW_FIELDS = [
    "GroupId",
    "SuggestedAction",
    "ConfirmDelete",
    "KeepReason",
    "DuplicateReason",
    "MatchConfidence",
    "SimilarityScore",
    "AverageFrameHashDistance",
    "MaxFrameHashDistance",
    "DurationDeltaSeconds",
    "DateModifiedDeltaDays",
    "DateModifiedClose",
    "FullPath",
    "FileName",
    "ParentDirectory",
    "Extension",
    "FileSizeBytes",
    "EstimatedBitrateKbps",
    "DateModifiedLocal",
    "DurationSeconds",
    "Width",
    "Height",
    "FrameHashSignature",
    "SampleCount",
    "DateFromArchiveName",
    "SequenceFromArchiveName",
    "HasSidecar",
    "SidecarPath",
    "SidecarSizeBytes",
    "Status",
    "ProcessNote",
    "ProcessedUtc",
]

PAIR_FIELDS = [
    "GroupId",
    "APath",
    "BPath",
    "MatchConfidence",
    "SimilarityScore",
    "AverageFrameHashDistance",
    "MaxFrameHashDistance",
    "DurationDeltaSeconds",
    "DateModifiedDeltaDays",
    "DateModifiedClose",
    "Reason",
]

INVENTORY_FIELDS = [
    "FullPath",
    "FileName",
    "ParentDirectory",
    "Extension",
    "FileSizeBytes",
    "EstimatedBitrateKbps",
    "DateModifiedLocal",
    "DurationSeconds",
    "Width",
    "Height",
    "FrameHashSignature",
    "SampleCount",
    "FrameErrors",
    "DateFromArchiveName",
    "SequenceFromArchiveName",
    "HasSidecar",
    "SidecarPath",
    "SidecarSizeBytes",
    "Status",
    "Message",
]

SUMMARY_FIELDS = ["Metric", "Value"]


@dataclass
class VideoInfo:
    path: Path
    size: int
    mtime_ns: int
    duration: float
    width: int
    height: int
    frame_hashes: list[int]
    frame_errors: int
    date_from_name: str
    sequence_from_name: str
    status: str = "OK"
    message: str = ""

    @property
    def signature(self) -> str:
        return ",".join(f"{value:016x}" for value in self.frame_hashes)

    @property
    def sidecar(self) -> Path:
        return sidecar_path_for_video(self.path)

    @property
    def has_sidecar(self) -> bool:
        return self.sidecar.exists()

    @property
    def sidecar_size(self) -> int:
        return file_size(self.sidecar) if self.has_sidecar else 0

    @property
    def mtime_local(self) -> str:
        try:
            return dt.datetime.fromtimestamp(self.mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    @property
    def bitrate_kbps(self) -> float:
        if self.duration <= 0 or self.size <= 0:
            return 0.0
        return (self.size * 8.0) / self.duration / 1000.0


@dataclass
class Match:
    a: VideoInfo
    b: VideoInfo
    confidence: str
    score: float
    avg_distance: float
    max_distance: int
    duration_delta: float
    modified_delta_days: float
    modified_close: bool
    reason: str


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def normalize_path_for_compare(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except Exception:
        return os.path.normcase(str(path))


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def parse_archive_video_name(path: Path) -> tuple[str, str]:
    match = RENAMED_VIDEO_RE.match(path.name)
    if not match:
        return "", ""
    return match.group("date"), match.group("seq")


def sidecar_path_for_video(path: Path) -> Path:
    return path.with_name(path.name + ".xmp")


def parse_sample_points(value: str) -> list[float]:
    points: list[float] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            point = float(part)
        except ValueError:
            continue
        if 0.0 < point < 1.0:
            points.append(point)
    if not points:
        return [0.10, 0.25, 0.50, 0.75, 0.90]
    return sorted(set(points))


def find_executable(explicit: str, name: str) -> str:
    """
    Resolve FFmpeg/FFprobe in this order:
      1. Explicit -FFmpeg / -FFprobe argument
      2. Tool-local install under .\ffmpeg\
      3. System PATH

    Tool-local layouts supported:
      .\ffmpeg\bin\ffmpeg.exe
      .\ffmpeg\ffmpeg-8.1.2-essentials_build\bin\ffmpeg.exe
    """
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
        raise FileNotFoundError(f"{name} not found: {explicit}")

    exe_name = name if name.lower().endswith(".exe") else f"{name}.exe"

    local_root = SCRIPT_DIR / "ffmpeg"
    direct_candidates = [
        local_root / "bin" / exe_name,
        local_root / exe_name,
    ]
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate)

    if local_root.exists():
        try:
            for candidate in local_root.rglob(exe_name):
                if candidate.is_file():
                    return str(candidate)
        except OSError:
            pass

    found = shutil.which(name)
    if not found:
        found = shutil.which(exe_name)
    if not found:
        raise FileNotFoundError(
            f"{name} was not found. Install FFmpeg under {local_root}, add it to PATH, "
            f"or pass -{name.capitalize()} with the full path."
        )
    return found


def print_progress(label: str, done: int, total: int) -> None:
    if total <= 0:
        return
    width = 28
    pct = done / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r{label} [{bar}] {done:,}/{total:,} {pct * 100:6.2f}%", end="", flush=True)


def clear_progress_line() -> None:
    print("\r" + " " * 180 + "\r", end="", flush=True)


def scan_videos(root: Path, skip_dir_names: set[str], limit: int | None = None) -> list[Path]:
    found: list[Path] = []
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                dirs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if normalize_name(entry.name) in skip_dir_names:
                                continue
                            dirs.append(Path(entry.path))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        path = Path(entry.path)
                        if path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                            found.append(path)
                            if limit and len(found) >= limit:
                                return found

                    except OSError as ex:
                        print(f"WARNING: Could not inspect {entry.path}: {ex}", file=sys.stderr)

                stack.extend(reversed(dirs))
        except OSError as ex:
            print(f"WARNING: Could not scan {current}: {ex}", file=sys.stderr)

    found.sort(key=lambda p: normalize_path_for_compare(p))
    return found


def init_cache(cache_db: Path) -> sqlite3.Connection:
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache_db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sample_points TEXT NOT NULL,
            hash_version TEXT NOT NULL,
            duration REAL NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            frame_hashes_json TEXT NOT NULL,
            frame_errors INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_cache(
    conn: sqlite3.Connection,
    path: Path,
    size: int,
    mtime_ns: int,
    sample_points_key: str,
) -> VideoInfo | None:
    row = conn.execute(
        """
        SELECT duration, width, height, frame_hashes_json, frame_errors, status, message
        FROM video_cache
        WHERE path=? AND size=? AND mtime_ns=? AND sample_points=? AND hash_version=?
        """,
        (str(path), size, mtime_ns, sample_points_key, HASH_VERSION),
    ).fetchone()

    if not row:
        return None

    duration, width, height, hashes_json, frame_errors, status, message = row
    try:
        frame_hashes = [int(x) for x in json.loads(hashes_json)]
    except Exception:
        frame_hashes = []

    date_from_name, sequence_from_name = parse_archive_video_name(path)
    return VideoInfo(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        duration=float(duration),
        width=int(width),
        height=int(height),
        frame_hashes=frame_hashes,
        frame_errors=int(frame_errors),
        date_from_name=date_from_name,
        sequence_from_name=sequence_from_name,
        status=str(status),
        message=str(message),
    )


def set_cache(
    conn: sqlite3.Connection,
    info: VideoInfo,
    sample_points_key: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO video_cache (
            path, size, mtime_ns, sample_points, hash_version,
            duration, width, height, frame_hashes_json, frame_errors,
            status, message, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(info.path),
            info.size,
            info.mtime_ns,
            sample_points_key,
            HASH_VERSION,
            info.duration,
            info.width,
            info.height,
            json.dumps(info.frame_hashes),
            info.frame_errors,
            info.status,
            info.message,
            now_utc_iso(),
        ),
    )


def ffprobe_info(ffprobe: str, path: Path, timeout_seconds: int) -> tuple[float, int, int]:
    args = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        str(path),
    ]

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )

    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip() or f"ffprobe failed with exit code {completed.returncode}")

    data = json.loads(completed.stdout or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    width = 0
    height = 0
    duration = 0.0

    if streams:
        stream = streams[0]
        width = int(float(stream.get("width") or 0))
        height = int(float(stream.get("height") or 0))
        try:
            duration = float(stream.get("duration") or 0.0)
        except Exception:
            duration = 0.0

    if duration <= 0:
        try:
            duration = float(fmt.get("duration") or 0.0)
        except Exception:
            duration = 0.0

    return duration, width, height


def dhash_image(image: Image.Image) -> int:
    """
    64-bit difference hash.
    """
    img = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    # Pillow 14 deprecates Image.getdata(); use raw bytes for this small grayscale image.
    pixels = img.tobytes()
    value = 0

    for y in range(8):
        for x in range(8):
            left = pixels[y * 9 + x]
            right = pixels[y * 9 + x + 1]
            value = (value << 1) | (1 if left > right else 0)

    return value


def sample_frame_hash(ffmpeg: str, path: Path, timestamp: float, timeout_seconds: int) -> int | None:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-threads", "1",
        "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(path),
        "-an",
        "-sn",
        "-dn",
        "-frames:v", "1",
        "-vf", "scale=128:128:force_original_aspect_ratio=decrease,pad=128:128:(ow-iw)/2:(oh-ih)/2",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
    ]

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None
    except KeyboardInterrupt:
        raise
    except Exception:
        return None

    if completed.returncode != 0 or not completed.stdout:
        return None

    try:
        with Image.open(io.BytesIO(completed.stdout)) as img:
            return dhash_image(img)
    except Exception:
        return None


def analyze_one_video(
    path: Path,
    ffmpeg: str,
    ffprobe: str,
    sample_points: list[float],
    ffmpeg_timeout: int,
    ffprobe_timeout: int,
) -> VideoInfo:
    size = file_size(path)
    mtime_ns = file_mtime_ns(path)
    date_from_name, sequence_from_name = parse_archive_video_name(path)

    try:
        duration, width, height = ffprobe_info(ffprobe, path, timeout_seconds=ffprobe_timeout)
    except Exception as ex:
        return VideoInfo(
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            duration=0.0,
            width=0,
            height=0,
            frame_hashes=[],
            frame_errors=len(sample_points),
            date_from_name=date_from_name,
            sequence_from_name=sequence_from_name,
            status="ERROR_FFPROBE",
            message=str(ex),
        )

    frame_hashes: list[int] = []
    errors = 0

    if duration <= 0:
        # Try one frame at zero if duration is unknown.
        h = sample_frame_hash(ffmpeg, path, 0.0, timeout_seconds=ffmpeg_timeout)
        if h is None:
            errors += 1
        else:
            frame_hashes.append(h)
    else:
        for point in sample_points:
            # Avoid exact beginning/end; some files decode poorly at boundaries.
            ts = duration * point
            ts = min(max(0.1, ts), max(0.1, duration - 0.1))
            h = sample_frame_hash(ffmpeg, path, ts, timeout_seconds=ffmpeg_timeout)
            if h is None:
                errors += 1
            else:
                frame_hashes.append(h)

    status = "OK" if frame_hashes else "ERROR_NO_FRAMES"
    message = "" if frame_hashes else "No sample frames could be decoded."

    return VideoInfo(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        duration=duration,
        width=width,
        height=height,
        frame_hashes=frame_hashes,
        frame_errors=errors,
        date_from_name=date_from_name,
        sequence_from_name=sequence_from_name,
        status=status,
        message=message,
    )


def load_or_analyze_videos(
    paths: list[Path],
    cache_db: Path,
    ffmpeg: str,
    ffprobe: str,
    sample_points: list[float],
    ffmpeg_timeout: int,
    ffprobe_timeout: int,
    rebuild_cache: bool,
) -> list[VideoInfo]:
    conn = init_cache(cache_db)
    sample_points_key = ",".join(f"{p:.4f}" for p in sample_points)

    infos: list[VideoInfo] = []
    try:
        for index, path in enumerate(paths, start=1):
            size = file_size(path)
            mtime_ns = file_mtime_ns(path)

            info = None
            if not rebuild_cache:
                info = get_cache(conn, path, size, mtime_ns, sample_points_key)

            if info is None:
                info = analyze_one_video(
                    path=path,
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    sample_points=sample_points,
                    ffmpeg_timeout=ffmpeg_timeout,
                    ffprobe_timeout=ffprobe_timeout,
                )
                set_cache(conn, info, sample_points_key)
                if index % 25 == 0:
                    conn.commit()

            infos.append(info)
            print_progress("Analyzing videos", index, len(paths))

        conn.commit()
        clear_progress_line()
    finally:
        conn.close()

    return infos


def hamming(a: int, b: int) -> int:
    return int(a ^ b).bit_count()


def compare_hashes(a: list[int], b: list[int]) -> tuple[float, int, int]:
    count = min(len(a), len(b))
    if count <= 0:
        return 999.0, 999, 0

    distances = [hamming(a[i], b[i]) for i in range(count)]
    return sum(distances) / count, max(distances), count


def modified_delta_days(a: VideoInfo, b: VideoInfo) -> float:
    if not a.mtime_ns or not b.mtime_ns:
        return 999999.0
    return abs(a.mtime_ns - b.mtime_ns) / 1_000_000_000 / 86400.0


def duration_tolerance_seconds(a: VideoInfo, b: VideoInfo, abs_seconds: float, percent: float) -> float:
    longer = max(a.duration, b.duration, 1.0)
    percent_seconds = longer * max(0.0, percent) / 100.0
    return max(abs_seconds, percent_seconds)


def score_match(avg_distance: float, max_distance: int, duration_delta: float, duration_tol: float, mod_delta_days: float, mod_close_days: float) -> float:
    # Visual similarity is the largest factor.
    visual = max(0.0, 100.0 - (avg_distance * 5.0) - (max_distance * 1.2))

    # Duration closeness matters a lot for videos.
    if duration_tol <= 0:
        duration_score = 0.0
    else:
        duration_score = max(0.0, 100.0 - min(100.0, (duration_delta / duration_tol) * 100.0))

    # Date modified is weak and only boosts audit confidence.
    modified_score = 100.0 if mod_delta_days <= mod_close_days else max(0.0, 40.0 - min(40.0, mod_delta_days))

    return round((visual * 0.70) + (duration_score * 0.25) + (modified_score * 0.05), 2)


def compare_videos(a: VideoInfo, b: VideoInfo, args: argparse.Namespace) -> Match | None:
    if a.status != "OK" or b.status != "OK":
        return None

    duration_delta = abs(a.duration - b.duration)
    dur_tol = duration_tolerance_seconds(
        a,
        b,
        abs_seconds=float(args.duration_tolerance_seconds),
        percent=float(args.duration_tolerance_percent),
    )

    if duration_delta > dur_tol:
        return None

    avg_distance, max_distance, sample_count = compare_hashes(a.frame_hashes, b.frame_hashes)
    if sample_count < int(args.min_matching_samples):
        return None

    mod_days = modified_delta_days(a, b)
    mod_close = mod_days <= float(args.date_modified_tolerance_days)

    confidence = ""
    if (
        avg_distance <= float(args.very_high_avg_hash_distance)
        and max_distance <= int(args.very_high_max_hash_distance)
        and duration_delta <= min(1.0, dur_tol)
    ):
        confidence = "Very High"
    elif (
        avg_distance <= float(args.high_avg_hash_distance)
        and max_distance <= int(args.high_max_hash_distance)
    ):
        confidence = "High"
    elif (
        avg_distance <= float(args.review_avg_hash_distance)
        and max_distance <= int(args.review_max_hash_distance)
    ):
        confidence = "Review Carefully"
    else:
        return None

    score = score_match(avg_distance, max_distance, duration_delta, dur_tol, mod_days, float(args.date_modified_tolerance_days))

    reason_parts = [
        f"duration delta {duration_delta:.3f}s within tolerance {dur_tol:.3f}s",
        f"average frame hash distance {avg_distance:.2f}",
        f"max frame hash distance {max_distance}",
    ]

    if mod_close:
        reason_parts.append(f"Date Modified close: {mod_days:.2f} day(s)")
    else:
        reason_parts.append(f"Date Modified not close: {mod_days:.2f} day(s)")

    if a.width and b.width and a.height and b.height:
        if a.width == b.width and a.height == b.height:
            reason_parts.append(f"same resolution {a.width}x{a.height}")
        else:
            reason_parts.append(f"resolution differs {a.width}x{a.height} vs {b.width}x{b.height}")

    return Match(
        a=a,
        b=b,
        confidence=confidence,
        score=score,
        avg_distance=round(avg_distance, 2),
        max_distance=max_distance,
        duration_delta=round(duration_delta, 3),
        modified_delta_days=round(mod_days, 3),
        modified_close=mod_close,
        reason="; ".join(reason_parts),
    )


def find_matches(infos: list[VideoInfo], args: argparse.Namespace) -> list[Match]:
    valid = [i for i in infos if i.status == "OK" and i.frame_hashes]
    valid.sort(key=lambda i: (i.duration, normalize_path_for_compare(i.path)))

    matches: list[Match] = []
    total = len(valid)
    compared = 0

    for i, a in enumerate(valid):
        for j in range(i + 1, total):
            b = valid[j]

            # Since sorted by duration, break when even the broad tolerance cannot match.
            max_possible_tol = duration_tolerance_seconds(
                a,
                b,
                abs_seconds=float(args.duration_tolerance_seconds),
                percent=float(args.duration_tolerance_percent),
            )
            if (b.duration - a.duration) > max_possible_tol:
                break

            match = compare_videos(a, b, args)
            compared += 1
            if match:
                matches.append(match)

        if i % 50 == 0 or i == total - 1:
            print_progress("Comparing candidates", i + 1, total)

    clear_progress_line()
    print(f"Candidate comparisons performed: {compared:,}")
    return matches


def connected_components(matches: list[Match]) -> list[list[VideoInfo]]:
    parent: dict[str, str] = {}
    info_by_key: dict[str, VideoInfo] = {}

    def key(info: VideoInfo) -> str:
        return normalize_path_for_compare(info.path)

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    for match in matches:
        ka = key(match.a)
        kb = key(match.b)
        info_by_key[ka] = match.a
        info_by_key[kb] = match.b
        union(ka, kb)

    groups_by_root: dict[str, list[VideoInfo]] = {}
    for k, info in info_by_key.items():
        groups_by_root.setdefault(find(k), []).append(info)

    groups = [sorted(v, key=lambda i: normalize_path_for_compare(i.path)) for v in groups_by_root.values() if len(v) > 1]
    groups.sort(key=lambda g: normalize_path_for_compare(choose_keeper(g).path))
    return groups


def choose_keeper(group: list[VideoInfo]) -> VideoInfo:
    """
    Keeper preference for videos.

    The first version ranked resolution before size. That could keep a tiny,
    heavily-compressed 1080p file over a much larger/lower-compression copy.

    Current preference:
      1. Already-renamed archive filename
      2. Has sidecar
      3. Higher estimated bitrate / larger file size, with strong weight
      4. Higher resolution, but only after bitrate/size
      5. Shorter path
      6. Alphabetical path

    Rationale:
      For compressed duplicates, file size/estimated bitrate is usually a better
      preservation signal than resolution alone. A very small 1080p file can be
      worse than a larger 1440x1080 or 720p file.
    """
    def key(info: VideoInfo) -> tuple[int, int, float, int, int, str]:
        already_renamed_rank = 0 if info.date_from_name and info.sequence_from_name else 1
        sidecar_rank = 0 if info.has_sidecar else 1

        # Negative values because sorted() picks the lowest tuple.
        bitrate_rank = -info.bitrate_kbps
        pixels_rank = -(info.width * info.height)
        path_len = len(str(info.path))

        return (
            already_renamed_rank,
            sidecar_rank,
            bitrate_rank,
            pixels_rank,
            path_len,
            normalize_path_for_compare(info.path),
        )

    return sorted(group, key=key)[0]


def match_key(a: VideoInfo, b: VideoInfo) -> tuple[str, str]:
    ka = normalize_path_for_compare(a.path)
    kb = normalize_path_for_compare(b.path)
    return (ka, kb) if ka <= kb else (kb, ka)


def best_match_for_row(info: VideoInfo, keeper: VideoInfo, group: list[VideoInfo], match_lookup: dict[tuple[str, str], Match]) -> Match | None:
    direct = match_lookup.get(match_key(info, keeper))
    if direct:
        return direct

    candidates = []
    for other in group:
        if normalize_path_for_compare(other.path) == normalize_path_for_compare(info.path):
            continue
        m = match_lookup.get(match_key(info, other))
        if m:
            candidates.append(m)

    if not candidates:
        return None

    confidence_rank = {"Very High": 3, "High": 2, "Review Carefully": 1}
    return sorted(candidates, key=lambda m: (confidence_rank.get(m.confidence, 0), m.score), reverse=True)[0]


def row_for_video(
    group_id: str,
    info: VideoInfo,
    keeper: VideoInfo,
    group: list[VideoInfo],
    match_lookup: dict[tuple[str, str], Match],
) -> dict[str, Any]:
    is_keeper = normalize_path_for_compare(info.path) == normalize_path_for_compare(keeper.path)
    match = None if is_keeper else best_match_for_row(info, keeper, group, match_lookup)

    if is_keeper:
        suggested = "KEEP"
        keep_reason = "Selected keeper: archive-named/sidecar/estimated-bitrate/resolution/path preference."
        duplicate_reason = ""
        confidence = ""
        score = ""
        avg_distance = ""
        max_distance = ""
        duration_delta = ""
        mod_delta = ""
        mod_close = ""
    else:
        suggested = "DELETE"
        keep_reason = ""
        duplicate_reason = match.reason if match else "Similar-video group member. Review before confirming."
        confidence = match.confidence if match else "Review Carefully"
        score = match.score if match else ""
        avg_distance = match.avg_distance if match else ""
        max_distance = match.max_distance if match else ""
        duration_delta = match.duration_delta if match else ""
        mod_delta = match.modified_delta_days if match else ""
        mod_close = "YES" if match and match.modified_close else "NO"

    return {
        "GroupId": group_id,
        "SuggestedAction": suggested,
        "ConfirmDelete": "",
        "KeepReason": keep_reason,
        "DuplicateReason": duplicate_reason,
        "MatchConfidence": confidence,
        "SimilarityScore": score,
        "AverageFrameHashDistance": avg_distance,
        "MaxFrameHashDistance": max_distance,
        "DurationDeltaSeconds": duration_delta,
        "DateModifiedDeltaDays": mod_delta,
        "DateModifiedClose": mod_close,
        "FullPath": str(info.path),
        "FileName": info.path.name,
        "ParentDirectory": str(info.path.parent),
        "Extension": info.path.suffix.lower(),
        "FileSizeBytes": info.size,
        "EstimatedBitrateKbps": round(info.bitrate_kbps, 2),
        "DateModifiedLocal": info.mtime_local,
        "DurationSeconds": round(info.duration, 3),
        "Width": info.width,
        "Height": info.height,
        "FrameHashSignature": info.signature,
        "SampleCount": len(info.frame_hashes),
        "DateFromArchiveName": info.date_from_name,
        "SequenceFromArchiveName": info.sequence_from_name,
        "HasSidecar": "YES" if info.has_sidecar else "NO",
        "SidecarPath": str(info.sidecar),
        "SidecarSizeBytes": info.sidecar_size,
        "Status": "",
        "ProcessNote": "",
        "ProcessedUtc": "",
    }


def build_review_rows(groups: list[list[VideoInfo]], matches: list[Match]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    match_lookup = {match_key(m.a, m.b): m for m in matches}

    review_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []

    for idx, group in enumerate(groups, start=1):
        group_id = f"VG{idx:06d}"
        keeper = choose_keeper(group)

        ordered_group = [keeper] + [
            info for info in sorted(group, key=lambda i: normalize_path_for_compare(i.path))
            if normalize_path_for_compare(info.path) != normalize_path_for_compare(keeper.path)
        ]

        for info in ordered_group:
            review_rows.append(row_for_video(group_id, info, keeper, ordered_group, match_lookup))

        group_keys = {normalize_path_for_compare(i.path) for i in group}
        for match in matches:
            if normalize_path_for_compare(match.a.path) in group_keys and normalize_path_for_compare(match.b.path) in group_keys:
                pair_rows.append({
                    "GroupId": group_id,
                    "APath": str(match.a.path),
                    "BPath": str(match.b.path),
                    "MatchConfidence": match.confidence,
                    "SimilarityScore": match.score,
                    "AverageFrameHashDistance": match.avg_distance,
                    "MaxFrameHashDistance": match.max_distance,
                    "DurationDeltaSeconds": match.duration_delta,
                    "DateModifiedDeltaDays": match.modified_delta_days,
                    "DateModifiedClose": "YES" if match.modified_close else "NO",
                    "Reason": match.reason,
                })

    return review_rows, pair_rows


def inventory_row(info: VideoInfo) -> dict[str, Any]:
    return {
        "FullPath": str(info.path),
        "FileName": info.path.name,
        "ParentDirectory": str(info.path.parent),
        "Extension": info.path.suffix.lower(),
        "FileSizeBytes": info.size,
        "EstimatedBitrateKbps": round(info.bitrate_kbps, 2),
        "DateModifiedLocal": info.mtime_local,
        "DurationSeconds": round(info.duration, 3),
        "Width": info.width,
        "Height": info.height,
        "FrameHashSignature": info.signature,
        "SampleCount": len(info.frame_hashes),
        "FrameErrors": info.frame_errors,
        "DateFromArchiveName": info.date_from_name,
        "SequenceFromArchiveName": info.sequence_from_name,
        "HasSidecar": "YES" if info.has_sidecar else "NO",
        "SidecarPath": str(info.sidecar),
        "SidecarSizeBytes": info.sidecar_size,
        "Status": info.status,
        "Message": info.message,
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {}
            for field in fieldnames:
                value = row.get(field, "")
                clean[field] = "" if value is None else str(value).replace("\r", " ").replace("\n", " ").strip()
            writer.writerow(clean)


def analyze_mode(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    output_folder = Path(args.output_folder).expanduser().resolve()
    cache_db = Path(args.cache_db).expanduser().resolve()

    if not root.exists():
        print(f"ERROR: Root folder not found: {root}", file=sys.stderr)
        return 2

    try:
        ffmpeg = find_executable(args.ffmpeg, "ffmpeg")
        ffprobe = find_executable(args.ffprobe, "ffprobe")
    except FileNotFoundError as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 2

    skip_dir_names = set(DEFAULT_SKIP_DIR_NAMES)
    for item in args.skip_dir_name or []:
        if item.strip():
            skip_dir_names.add(normalize_name(item))

    sample_points = parse_sample_points(args.sample_points)
    stamp = now_stamp()

    review_csv = output_folder / f"{stamp}_similar_videos_review.csv"
    pair_csv = output_folder / f"{stamp}_similar_videos_pairs.csv"
    inventory_csv = output_folder / f"{stamp}_similar_videos_inventory.csv"
    summary_csv = output_folder / f"{stamp}_similar_videos_summary.csv"

    print("Find Similar Videos")
    print("Mode:                  ANALYZE / read-only")
    print(f"Root:                  {root}")
    print(f"Output folder:         {output_folder}")
    print(f"Cache DB:              {cache_db}")
    print(f"FFmpeg:                {ffmpeg}")
    print(f"FFprobe:               {ffprobe}")
    print(f"Sample points:         {','.join(str(x) for x in sample_points)}")
    print(f"Duration tolerance:    {args.duration_tolerance_seconds}s or {args.duration_tolerance_percent}%")
    print(f"Date modified close:   <= {args.date_modified_tolerance_days} day(s)")
    print()

    print("Scanning for video files...")
    limit = int(args.limit) if args.limit else None
    video_paths = scan_videos(root, skip_dir_names=skip_dir_names, limit=limit)
    print(f"Video files found: {len(video_paths):,}")

    if not video_paths:
        print("No video files found.")
        return 0

    infos = load_or_analyze_videos(
        paths=video_paths,
        cache_db=cache_db,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        sample_points=sample_points,
        ffmpeg_timeout=int(args.ffmpeg_timeout_seconds),
        ffprobe_timeout=int(args.ffprobe_timeout_seconds),
        rebuild_cache=bool(args.rebuild_cache),
    )

    ok_infos = [i for i in infos if i.status == "OK"]
    error_infos = [i for i in infos if i.status != "OK"]

    print(f"Videos analyzed successfully: {len(ok_infos):,}")
    print(f"Videos with read/decode errors: {len(error_infos):,}")

    print("Finding similar video candidates...")
    matches = find_matches(ok_infos, args)
    groups = connected_components(matches)

    review_rows, pair_rows = build_review_rows(groups, matches)
    inventory_rows = [inventory_row(i) for i in infos]

    duplicate_delete_candidates = sum(1 for r in review_rows if r.get("SuggestedAction") == "DELETE")
    sidecar_count = sum(1 for i in infos if i.has_sidecar)

    summary_rows = [
        {"Metric": "Video files scanned", "Value": len(video_paths)},
        {"Metric": "Videos analyzed successfully", "Value": len(ok_infos)},
        {"Metric": "Videos with read/decode errors", "Value": len(error_infos)},
        {"Metric": "Sidecars detected", "Value": sidecar_count},
        {"Metric": "Similar pairs found", "Value": len(matches)},
        {"Metric": "Similar groups found", "Value": len(groups)},
        {"Metric": "Suggested delete candidates", "Value": duplicate_delete_candidates},
        {"Metric": "Review CSV", "Value": review_csv},
        {"Metric": "Pair CSV", "Value": pair_csv},
        {"Metric": "Inventory CSV", "Value": inventory_csv},
    ]

    write_csv(review_csv, REVIEW_FIELDS, review_rows)
    write_csv(pair_csv, PAIR_FIELDS, pair_rows)
    write_csv(inventory_csv, INVENTORY_FIELDS, inventory_rows)
    write_csv(summary_csv, SUMMARY_FIELDS, summary_rows)

    print()
    print("Done.")
    print(f"Similar groups found:       {len(groups):,}")
    print(f"Suggested delete candidates:{duplicate_delete_candidates:,}")
    print(f"Review CSV:                 {review_csv}")
    print(f"Pair CSV:                   {pair_csv}")
    print(f"Inventory CSV:              {inventory_csv}")
    print(f"Summary CSV:                {summary_csv}")
    print()
    print("Next step: review the CSV, set ConfirmDelete=CONFIRM only for rows you want removed, then run -Process -WhatIf.")

    return 0


def read_review_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def delete_path(path: Path, *, permanent: bool) -> tuple[bool, str]:
    if not path.exists():
        return True, "Already missing."

    try:
        if permanent:
            path.unlink()
            return True, "Permanently deleted."
        try:
            from send2trash import send2trash
        except ImportError:
            return False, "send2trash is not installed. Run: pip install send2trash or use -PermanentDelete."

        send2trash(str(path))
        return True, "Sent to Recycle Bin."
    except Exception as ex:
        return False, str(ex)


def confirmed_delete(row: dict[str, str]) -> bool:
    return (
        (row.get("SuggestedAction") or "").strip().upper() == "DELETE"
        and (row.get("ConfirmDelete") or "").strip().upper() == "CONFIRM"
    )


def group_id(row: dict[str, str]) -> str:
    return (row.get("GroupId") or "").strip() or "UNKNOWN"


def process_mode(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    rows = read_review_csv(csv_path)

    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(group_id(row), []).append(row)

    confirmed_rows = [row for row in rows if confirmed_delete(row)]

    print("Process Similar Video Deletes")
    print(f"Mode:             {'WHATIF / dry run' if args.whatif else 'LIVE'}")
    print(f"CSV:              {csv_path}")
    print(f"Confirmed deletes:{len(confirmed_rows):,}")
    print(f"Permanent delete: {'Yes' if args.permanent_delete else 'No - Recycle Bin'}")
    print(f"Delete sidecars:  {'No' if args.no_sidecars else 'Yes'}")
    print()

    if not confirmed_rows:
        print("No rows with SuggestedAction=DELETE and ConfirmDelete=CONFIRM.")
        return 0

    # Safety: refuse deleting every row in a group unless explicitly allowed.
    blocked_groups: set[str] = set()
    for gid, group_rows in groups.items():
        existing_video_rows = [
            r for r in group_rows
            if (r.get("FullPath") or "").strip() and Path((r.get("FullPath") or "").strip()).exists()
        ]
        if not existing_video_rows:
            continue
        confirmed_in_group = [r for r in existing_video_rows if confirmed_delete(r)]
        if len(confirmed_in_group) >= len(existing_video_rows) and not args.allow_delete_whole_group:
            blocked_groups.add(gid)

    if blocked_groups:
        print("ERROR: Refusing to delete every existing file in these group(s):")
        for gid in sorted(blocked_groups):
            print(f"  {gid}")
        print("Fix the CSV so at least one file remains KEEP, or rerun with -AllowDeleteWholeGroup if intentional.")
        return 2

    process_rows: list[dict[str, Any]] = []
    errors = 0
    videos_deleted = 0
    sidecars_deleted = 0

    for idx, row in enumerate(confirmed_rows, start=1):
        gid = group_id(row)
        video_path_text = (row.get("FullPath") or "").strip()
        video_path = Path(video_path_text)

        sidecar_text = (row.get("SidecarPath") or "").strip()
        sidecar_path = Path(sidecar_text) if sidecar_text else sidecar_path_for_video(video_path)

        status_parts = []
        note_parts = []

        if gid in blocked_groups:
            status = "BLOCKED_WHOLE_GROUP"
            note = "Blocked because every file in the group was marked for delete."
            errors += 1
        elif args.whatif:
            status = "WHATIF"
            note = f"Would delete video: {video_path}"
            if not args.no_sidecars and sidecar_path.exists():
                note += f" | Would delete sidecar: {sidecar_path}"
        else:
            ok_video, msg_video = delete_path(video_path, permanent=bool(args.permanent_delete))
            if ok_video:
                videos_deleted += 1
                status_parts.append("VIDEO_DELETED")
            else:
                errors += 1
                status_parts.append("ERROR_VIDEO")
            note_parts.append(f"Video: {msg_video}")

            if not args.no_sidecars:
                if sidecar_path.exists():
                    ok_sidecar, msg_sidecar = delete_path(sidecar_path, permanent=bool(args.permanent_delete))
                    if ok_sidecar:
                        sidecars_deleted += 1
                        status_parts.append("SIDECAR_DELETED")
                    else:
                        errors += 1
                        status_parts.append("ERROR_SIDECAR")
                    note_parts.append(f"Sidecar: {msg_sidecar}")
                else:
                    status_parts.append("NO_SIDECAR")
                    note_parts.append("Sidecar: not found.")

            status = ";".join(status_parts)
            note = " | ".join(note_parts)

        out_row = dict(row)
        out_row["Status"] = status
        out_row["ProcessNote"] = note
        out_row["ProcessedUtc"] = now_utc_iso()
        process_rows.append(out_row)

        print_progress("Processing deletes", idx, len(confirmed_rows))

    clear_progress_line()

    report_path = csv_path.with_name(csv_path.stem + f"_delete_process_{now_stamp()}.csv")
    write_csv(report_path, REVIEW_FIELDS, process_rows)

    print("Done.")
    if args.whatif:
        print(f"WHATIF rows previewed: {len(confirmed_rows):,}")
    else:
        print(f"Videos deleted:        {videos_deleted:,}")
        print(f"Sidecars deleted:      {sidecars_deleted:,}")
        print(f"Errors:                {errors:,}")
    print(f"Process report:        {report_path}")

    return 0 if errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find similar/compressed duplicate videos and process confirmed deletes with sidecars.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("-Root", "--root", default=DEFAULT_ROOT, help="Root folder to scan recursively.")
    parser.add_argument("-OutputFolder", "--output-folder", default=str(DEFAULT_OUTPUT_FOLDER), help="Output folder for review/pair/inventory/summary CSV files.")
    parser.add_argument("-CacheDb", "--cache-db", default=str(DEFAULT_CACHE_DB), help="SQLite cache for FFprobe/frame-hash results.")
    parser.add_argument("-FFmpeg", "--ffmpeg", default="", help="Path to ffmpeg.exe. Defaults to ffmpeg in PATH.")
    parser.add_argument("-FFprobe", "--ffprobe", default="", help="Path to ffprobe.exe. Defaults to ffprobe in PATH.")
    parser.add_argument("-Limit", "--limit", type=int, default=0, help="Limit videos for testing. 0 means no limit.")
    parser.add_argument("-RebuildCache", "--rebuild-cache", action="store_true", help="Ignore cached video hash results and rebuild them.")
    parser.add_argument("-SkipDirName", "--skip-dir-name", action="append", default=[], help="Directory name to skip. Can be used multiple times.")

    parser.add_argument("-SamplePoints", "--sample-points", default=DEFAULT_SAMPLE_POINTS, help="Comma-separated video duration fractions to sample, e.g. 0.10,0.25,0.50,0.75,0.90.")
    parser.add_argument("-MinMatchingSamples", "--min-matching-samples", type=int, default=3, help="Minimum sampled frames required for comparison.")

    parser.add_argument("-DurationToleranceSeconds", "--duration-tolerance-seconds", type=float, default=3.0, help="Absolute duration/length tolerance in seconds.")
    parser.add_argument("-DurationTolerancePercent", "--duration-tolerance-percent", type=float, default=1.0, help="Relative duration/length tolerance percent.")
    parser.add_argument("-DateModifiedToleranceDays", "--date-modified-tolerance-days", type=float, default=3.0, help="Date Modified closeness window used as a weak confidence boost/audit field.")

    parser.add_argument("-VeryHighAvgHashDistance", "--very-high-avg-hash-distance", type=float, default=4.0, help="Very High confidence average frame hash distance threshold.")
    parser.add_argument("-VeryHighMaxHashDistance", "--very-high-max-hash-distance", type=int, default=10, help="Very High confidence max frame hash distance threshold.")
    parser.add_argument("-HighAvgHashDistance", "--high-avg-hash-distance", type=float, default=8.0, help="High confidence average frame hash distance threshold.")
    parser.add_argument("-HighMaxHashDistance", "--high-max-hash-distance", type=int, default=16, help="High confidence max frame hash distance threshold.")
    parser.add_argument("-ReviewAvgHashDistance", "--review-avg-hash-distance", type=float, default=12.0, help="Review Carefully average frame hash distance threshold.")
    parser.add_argument("-ReviewMaxHashDistance", "--review-max-hash-distance", type=int, default=22, help="Review Carefully max frame hash distance threshold.")

    parser.add_argument("-FFmpegTimeoutSeconds", "--ffmpeg-timeout-seconds", type=int, default=12, help="Timeout per sampled FFmpeg frame extraction. Slow/bad samples are skipped, not fatal.")
    parser.add_argument("-FFprobeTimeoutSeconds", "--ffprobe-timeout-seconds", type=int, default=30, help="Timeout per FFprobe metadata read.")

    parser.add_argument("-Process", "--process", action="store_true", help="Process confirmed deletes from a reviewed CSV.")
    parser.add_argument("-Csv", "--csv", default="", help="Reviewed CSV to process when -Process is used.")
    parser.add_argument("-WhatIf", "--whatif", action="store_true", help="Preview delete processing without deleting anything.")
    parser.add_argument("-PermanentDelete", "--permanent-delete", action="store_true", help="Permanently delete instead of sending to Recycle Bin. Not recommended.")
    parser.add_argument("-NoSidecars", "--no-sidecars", action="store_true", help="Do not delete sidecar .xmp files when deleting confirmed videos.")
    parser.add_argument("-AllowDeleteWholeGroup", "--allow-delete-whole-group", action="store_true", help="Allow every file in a group to be deleted. Off by default.")

    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.process:
        if not args.csv:
            print("ERROR: -Process requires -Csv <reviewed_csv>.", file=sys.stderr)
            return 2
        return process_mode(args)

    return analyze_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
