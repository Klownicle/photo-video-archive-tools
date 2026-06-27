#!/usr/bin/env python3
r"""
Find-SimilarImages-ReviewDelete.py

Find visually similar duplicate images by looking at image content, not metadata.

Designed for:
  - Renamed copies
  - Recompressed copies
  - Resized copies
  - Same image saved in a different folder

Not designed for:
  - Cropped/cut-apart images
  - Heavily edited images
  - Screenshots that merely look vaguely similar

Workflow:
  1. Run analysis mode. It is read-only and creates a CSV.
  2. Review/edit the CSV.
  3. Run process mode. It deletes only rows explicitly marked CONFIRM.

Default root:
  D:\MediaArchive\Photos and Videos

Install:
  pip install Pillow send2trash

Recommended for iPhone HEIC/HEIF:
  pip install pillow-heif

Examples:
  # Read-only analysis
  python Find-SimilarImages-ReviewDelete.py

  # Use more CPU threads
  python Find-SimilarImages-ReviewDelete.py -Workers 12

  # Stricter or more aggressive visual matching
  python Find-SimilarImages-ReviewDelete.py -Preset Strict
  python Find-SimilarImages-ReviewDelete.py -Preset Balanced
  python Find-SimilarImages-ReviewDelete.py -Preset Aggressive

  # Test on first 500 images
  python Find-SimilarImages-ReviewDelete.py -Limit 500

  # Preview delete processing from edited CSV
  python Find-SimilarImages-ReviewDelete.py -Process -Csv ".\duplicate_reports\20260626_120000_similar_images_review.csv" -WhatIf

  # Send confirmed deletes to Recycle Bin
  python Find-SimilarImages-ReviewDelete.py -Process -Csv ".\duplicate_reports\20260626_120000_similar_images_review.csv"

CSV rules:
  - SuggestedAction = KEEP or DELETE.
  - ConfirmDelete must be CONFIRM before a DELETE row is processed.
  - KEEP rows are never deleted.
  - The script refuses to delete every file in a group unless -AllowDeleteWholeGroup is used.
  - Default delete behavior uses Recycle Bin through send2trash.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ROOT = r"D:\MediaArchive\Photos and Videos"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_FOLDER = SCRIPT_DIR / "duplicate_reports"
DEFAULT_CACHE_DB = SCRIPT_DIR / "similar_image_hash_cache.sqlite"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".gif", ".bmp",
    ".tif", ".tiff", ".webp",
}

OPTIONAL_IMAGE_EXTENSIONS = {
    ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".dng",
}

ALL_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS | OPTIONAL_IMAGE_EXTENSIONS


try:
    from PIL import Image, ImageOps
except ImportError:
    print("ERROR: Pillow is required. Install it with: pip install Pillow", file=sys.stderr)
    raise SystemExit(2)


try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except Exception:
    HEIF_SUPPORT = False


try:
    from send2trash import send2trash  # type: ignore

    SEND2TRASH_SUPPORT = True
except Exception:
    send2trash = None  # type: ignore
    SEND2TRASH_SUPPORT = False


@dataclass
class ImageRecord:
    idx: int
    full_path: Path
    parent_directory: str
    file_name: str
    extension: str
    size_bytes: int
    size_readable: str
    mtime_ns: int
    width: int
    height: int
    megapixels: float
    exact_sha256: str
    dhash_hex: str
    dhash_int: int
    ahash_hex: str
    ahash_int: int
    avg_r: int
    avg_g: int
    avg_b: int


@dataclass
class ErrorRecord:
    full_path: Path
    parent_directory: str
    file_name: str
    extension: str
    size_bytes: int
    size_readable: str
    mtime_ns: int
    error: str


@dataclass
class HashResult:
    ok: bool
    full_path: str
    parent_directory: str
    file_name: str
    extension: str
    size_bytes: int
    size_readable: str
    mtime_ns: int
    width: int = 0
    height: int = 0
    megapixels: float = 0.0
    exact_sha256: str = ""
    dhash_hex: str = ""
    ahash_hex: str = ""
    avg_r: int = 0
    avg_g: int = 0
    avg_b: int = 0
    error: str = ""


class DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


class BKTreeNode:
    def __init__(self, value: int, record_index: int) -> None:
        self.value = value
        self.record_indices = [record_index]
        self.children: dict[int, "BKTreeNode"] = {}


class BKTree:
    def __init__(self) -> None:
        self.root: BKTreeNode | None = None

    @staticmethod
    def distance(a: int, b: int) -> int:
        return (a ^ b).bit_count()

    def add(self, value: int, record_index: int) -> None:
        if self.root is None:
            self.root = BKTreeNode(value, record_index)
            return

        node = self.root
        while True:
            dist = self.distance(value, node.value)
            if dist == 0:
                node.record_indices.append(record_index)
                return

            child = node.children.get(dist)
            if child is None:
                node.children[dist] = BKTreeNode(value, record_index)
                return
            node = child

    def query(self, value: int, threshold: int) -> list[tuple[int, int]]:
        if self.root is None:
            return []

        matches: list[tuple[int, int]] = []
        stack = [self.root]

        while stack:
            node = stack.pop()
            dist = self.distance(value, node.value)
            if dist <= threshold:
                for idx in node.record_indices:
                    matches.append((idx, dist))

            low = dist - threshold
            high = dist + threshold
            for child_dist, child in node.children.items():
                if low <= child_dist <= high:
                    stack.append(child)

        return matches


def readable_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def normalize_path_for_compare(path: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except Exception:
        return os.path.normcase(str(path))


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def format_seconds(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    return f"{minutes}m {sec}s"


def clear_progress_line() -> None:
    print("\r" + " " * 180 + "\r", end="", flush=True)


def print_progress(label: str, done: int, total: int, *, extra: str = "") -> None:
    if total <= 0:
        return
    width = 28
    pct = done / total
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    msg = f"\r{label} [{bar}] {done:,}/{total:,} {pct * 100:6.2f}%"
    if extra:
        msg += f" | {extra}"
    print(msg, end="", flush=True)


def scan_images(root: Path) -> list[Path]:
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
                        elif entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            if path.suffix.lower() in ALL_IMAGE_EXTENSIONS:
                                results.append(path)
                    except OSError as ex:
                        print(f"WARNING: Could not inspect {entry.path}: {ex}", file=sys.stderr)
        except OSError as ex:
            print(f"WARNING: Could not scan {current}: {ex}", file=sys.stderr)

    results.sort(key=normalize_path_for_compare)
    return results


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def dhash_256(image: Image.Image) -> int:
    grayscale = image.convert("L")
    small = grayscale.resize((17, 16), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())

    value = 0
    for row in range(16):
        offset = row * 17
        for col in range(16):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def ahash_256(image: Image.Image) -> int:
    grayscale = image.convert("L")
    small = grayscale.resize((16, 16), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)

    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= avg else 0)
    return value


def average_rgb(image: Image.Image) -> tuple[int, int, int]:
    small = image.convert("RGB").resize((1, 1), Image.Resampling.BOX)
    r, g, b = small.getpixel((0, 0))
    return int(r), int(g), int(b)


def hash_one_image(path_text: str, include_exact_hash: bool) -> HashResult:
    path = Path(path_text)
    size_bytes = 0
    mtime_ns = 0

    try:
        stat = path.stat()
        size_bytes = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)

        exact = sha256_file(path) if include_exact_hash else ""

        with Image.open(path) as img:
            try:
                img.seek(0)
            except Exception:
                pass

            img = ImageOps.exif_transpose(img)
            width, height = img.size
            dh = dhash_256(img)
            ah = ahash_256(img)
            r, g, b = average_rgb(img)

        return HashResult(
            ok=True,
            full_path=str(path),
            parent_directory=str(path.parent),
            file_name=path.name,
            extension=path.suffix.lower(),
            size_bytes=size_bytes,
            size_readable=readable_size(size_bytes),
            mtime_ns=mtime_ns,
            width=width,
            height=height,
            megapixels=round((width * height) / 1_000_000, 3),
            exact_sha256=exact,
            dhash_hex=f"{dh:064x}",
            ahash_hex=f"{ah:064x}",
            avg_r=r,
            avg_g=g,
            avg_b=b,
        )

    except Exception as ex:
        error = str(ex)
        if path.suffix.lower() in {".heic", ".heif"} and not HEIF_SUPPORT:
            error = "HEIC/HEIF support not available. Install with: pip install pillow-heif"

        return HashResult(
            ok=False,
            full_path=str(path),
            parent_directory=str(path.parent),
            file_name=path.name,
            extension=path.suffix.lower(),
            size_bytes=size_bytes,
            size_readable=readable_size(size_bytes),
            mtime_ns=mtime_ns,
            error=error,
        )


def connect_cache(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_cache (
            full_path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            parent_directory TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            size_readable TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            megapixels REAL NOT NULL,
            exact_sha256 TEXT NOT NULL,
            dhash_hex TEXT NOT NULL,
            ahash_hex TEXT NOT NULL,
            avg_r INTEGER NOT NULL,
            avg_g INTEGER NOT NULL,
            avg_b INTEGER NOT NULL,
            error TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def cache_lookup(conn: sqlite3.Connection, path: Path, include_exact_hash: bool) -> HashResult | None:
    try:
        stat = path.stat()
    except OSError:
        return None

    row = conn.execute(
        """
        SELECT full_path, parent_directory, file_name, extension, size_bytes, size_readable,
               mtime_ns, width, height, megapixels, exact_sha256, dhash_hex, ahash_hex,
               avg_r, avg_g, avg_b, error
        FROM image_cache
        WHERE full_path = ? AND size_bytes = ? AND mtime_ns = ?
        """,
        (str(path), int(stat.st_size), int(stat.st_mtime_ns)),
    ).fetchone()

    if not row:
        return None

    exact_sha = row[10] or ""
    if include_exact_hash and not exact_sha and not (row[16] or ""):
        return None

    error = row[16] or ""

    return HashResult(
        ok=(error == ""),
        full_path=row[0],
        parent_directory=row[1],
        file_name=row[2],
        extension=row[3],
        size_bytes=int(row[4]),
        size_readable=row[5],
        mtime_ns=int(row[6]),
        width=int(row[7]),
        height=int(row[8]),
        megapixels=float(row[9]),
        exact_sha256=exact_sha,
        dhash_hex=row[11] or "",
        ahash_hex=row[12] or "",
        avg_r=int(row[13]),
        avg_g=int(row[14]),
        avg_b=int(row[15]),
        error=error,
    )


def cache_store_many(conn: sqlite3.Connection, results: list[HashResult]) -> None:
    if not results:
        return

    rows = [
        (
            r.full_path,
            r.size_bytes,
            r.mtime_ns,
            r.parent_directory,
            r.file_name,
            r.extension,
            r.size_readable,
            r.width,
            r.height,
            r.megapixels,
            r.exact_sha256,
            r.dhash_hex,
            r.ahash_hex,
            r.avg_r,
            r.avg_g,
            r.avg_b,
            r.error,
            utc_now_text(),
        )
        for r in results
    ]

    conn.executemany(
        """
        INSERT OR REPLACE INTO image_cache (
            full_path, size_bytes, mtime_ns, parent_directory, file_name, extension,
            size_readable, width, height, megapixels, exact_sha256, dhash_hex, ahash_hex,
            avg_r, avg_g, avg_b, error, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def result_to_record(result: HashResult, idx: int) -> ImageRecord:
    return ImageRecord(
        idx=idx,
        full_path=Path(result.full_path),
        parent_directory=result.parent_directory,
        file_name=result.file_name,
        extension=result.extension,
        size_bytes=result.size_bytes,
        size_readable=result.size_readable,
        mtime_ns=result.mtime_ns,
        width=result.width,
        height=result.height,
        megapixels=result.megapixels,
        exact_sha256=result.exact_sha256,
        dhash_hex=result.dhash_hex,
        dhash_int=int(result.dhash_hex, 16),
        ahash_hex=result.ahash_hex,
        ahash_int=int(result.ahash_hex, 16),
        avg_r=result.avg_r,
        avg_g=result.avg_g,
        avg_b=result.avg_b,
    )


def result_to_error(result: HashResult) -> ErrorRecord:
    return ErrorRecord(
        full_path=Path(result.full_path),
        parent_directory=result.parent_directory,
        file_name=result.file_name,
        extension=result.extension,
        size_bytes=result.size_bytes,
        size_readable=result.size_readable,
        mtime_ns=result.mtime_ns,
        error=result.error,
    )


def hash_images_with_cache(
    files: list[Path],
    cache_db: Path,
    workers: int,
    include_exact_hash: bool,
    rebuild_cache: bool,
) -> tuple[list[ImageRecord], list[ErrorRecord], int, int]:
    conn = connect_cache(cache_db)
    cache_hits: list[HashResult] = []
    to_hash: list[Path] = []

    if rebuild_cache:
        to_hash = files
    else:
        for i, path in enumerate(files, start=1):
            cached = cache_lookup(conn, path, include_exact_hash)
            if cached:
                cache_hits.append(cached)
            else:
                to_hash.append(path)

            if i == 1 or i % 1000 == 0 or i == len(files):
                print_progress(
                    "Checking cache",
                    i,
                    len(files),
                    extra=f"Hits: {len(cache_hits):,} | To hash: {len(to_hash):,}",
                )

        clear_progress_line()

    results: list[HashResult] = list(cache_hits)
    pending_store: list[HashResult] = []

    if to_hash:
        start = time.time()
        done = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(hash_one_image, str(path), include_exact_hash): path
                for path in to_hash
            }

            for future in concurrent.futures.as_completed(future_map):
                result = future.result()
                pending_store.append(result)
                done += 1

                if done == 1 or done % 50 == 0 or done == len(to_hash):
                    elapsed = time.time() - start
                    rate = done / elapsed if elapsed > 0 else 0
                    errs = sum(1 for r in pending_store if not r.ok)
                    print_progress(
                        "Hashing images",
                        done,
                        len(to_hash),
                        extra=f"{rate:.1f}/sec | Recent errors: {errs:,}",
                    )

                if len(pending_store) >= 250:
                    cache_store_many(conn, pending_store)
                    results.extend(pending_store)
                    pending_store.clear()

        clear_progress_line()

        if pending_store:
            cache_store_many(conn, pending_store)
            results.extend(pending_store)
            pending_store.clear()

    conn.close()

    records: list[ImageRecord] = []
    errors: list[ErrorRecord] = []

    for result in results:
        if result.ok:
            try:
                records.append(result_to_record(result, idx=len(records)))
            except Exception as ex:
                result.error = f"Bad cached/hash result: {ex}"
                errors.append(result_to_error(result))
        else:
            errors.append(result_to_error(result))

    records.sort(key=lambda r: normalize_path_for_compare(r.full_path))
    for i, record in enumerate(records):
        record.idx = i

    errors.sort(key=lambda e: normalize_path_for_compare(e.full_path))

    return records, errors, len(cache_hits), len(to_hash)


def aspect_ratio(record: ImageRecord) -> float:
    if record.height == 0:
        return 0.0
    return record.width / record.height


def aspect_ratio_delta(a: ImageRecord, b: ImageRecord) -> float:
    ar_a = aspect_ratio(a)
    ar_b = aspect_ratio(b)
    if ar_a == 0 or ar_b == 0:
        return 999.0
    return abs(ar_a - ar_b) / max(ar_a, ar_b)


def color_distance(a: ImageRecord, b: ImageRecord) -> float:
    return ((a.avg_r - b.avg_r) ** 2 + (a.avg_g - b.avg_g) ** 2 + (a.avg_b - b.avg_b) ** 2) ** 0.5


def get_preset_values(preset: str) -> dict[str, float | int]:
    presets = {
        "STRICT": {
            "dhash_threshold": 8,
            "ahash_threshold": 18,
            "aspect_tolerance": 0.015,
            "color_threshold": 45,
        },
        "BALANCED": {
            "dhash_threshold": 12,
            "ahash_threshold": 28,
            "aspect_tolerance": 0.025,
            "color_threshold": 60,
        },
        "AGGRESSIVE": {
            "dhash_threshold": 18,
            "ahash_threshold": 40,
            "aspect_tolerance": 0.040,
            "color_threshold": 85,
        },
    }
    return presets[preset.upper()]


def is_visual_match(
    a: ImageRecord,
    b: ImageRecord,
    dhash_distance: int,
    *,
    ahash_threshold: int,
    aspect_tolerance: float,
    color_threshold: float,
) -> tuple[bool, int, float, float, str]:
    ahash_distance = BKTree.distance(a.ahash_int, b.ahash_int)
    ar_delta = aspect_ratio_delta(a, b)
    color_delta = color_distance(a, b)

    if ar_delta > aspect_tolerance:
        return False, ahash_distance, ar_delta, color_delta, "Aspect ratio differs"

    if ahash_distance > ahash_threshold:
        return False, ahash_distance, ar_delta, color_delta, "Secondary visual hash differs"

    if color_delta > color_threshold:
        return False, ahash_distance, ar_delta, color_delta, "Average color differs"

    return True, ahash_distance, ar_delta, color_delta, "Visual hash match"


def choose_keep_candidate(indices: list[int], records: list[ImageRecord]) -> int:
    extension_rank = {
        ".heic": 0,
        ".heif": 0,
        ".jpg": 1,
        ".jpeg": 1,
        ".png": 2,
        ".tif": 3,
        ".tiff": 3,
        ".webp": 4,
    }

    return sorted(
        indices,
        key=lambda i: (
            -(records[i].width * records[i].height),
            -records[i].size_bytes,
            extension_rank.get(records[i].extension.lower(), 99),
            len(str(records[i].full_path)),
            normalize_path_for_compare(records[i].full_path),
        ),
    )[0]


def build_groups(
    records: list[ImageRecord],
    *,
    dhash_threshold: int,
    ahash_threshold: int,
    aspect_tolerance: float,
    color_threshold: float,
    max_pair_rows: int,
) -> tuple[dict[int, list[int]], list[dict[str, Any]], bool]:
    dsu = DisjointSet(len(records))
    pair_rows: list[dict[str, Any]] = []
    pairs_truncated = False

    exact_map: dict[str, list[int]] = {}
    for i, record in enumerate(records):
        if record.exact_sha256:
            exact_map.setdefault(record.exact_sha256, []).append(i)

    for indices in exact_map.values():
        if len(indices) > 1:
            first = indices[0]
            for other in indices[1:]:
                dsu.union(first, other)

    tree = BKTree()

    for i, record in enumerate(records):
        matches = tree.query(record.dhash_int, dhash_threshold)

        for other_idx, dh_dist in matches:
            other = records[other_idx]

            exact = bool(record.exact_sha256 and record.exact_sha256 == other.exact_sha256)
            if exact:
                dsu.union(i, other_idx)
                ah_dist = BKTree.distance(record.ahash_int, other.ahash_int)
                ar_delta = aspect_ratio_delta(record, other)
                c_delta = color_distance(record, other)
                reason = "Exact SHA-256 duplicate"
                matched = True
            else:
                matched, ah_dist, ar_delta, c_delta, reason = is_visual_match(
                    record,
                    other,
                    dh_dist,
                    ahash_threshold=ahash_threshold,
                    aspect_tolerance=aspect_tolerance,
                    color_threshold=color_threshold,
                )
                if matched:
                    dsu.union(i, other_idx)

            if matched:
                if len(pair_rows) < max_pair_rows:
                    pair_rows.append(
                        {
                            "MatchReason": reason,
                            "DHashDistance": dh_dist,
                            "AHashDistance": ah_dist,
                            "AspectRatioDeltaPercent": round(ar_delta * 100, 4),
                            "ColorDistance": round(c_delta, 2),
                            "A_FullPath": str(other.full_path),
                            "A_FileName": other.file_name,
                            "A_SizeReadable": other.size_readable,
                            "A_SizeBytes": other.size_bytes,
                            "A_Width": other.width,
                            "A_Height": other.height,
                            "B_FullPath": str(record.full_path),
                            "B_FileName": record.file_name,
                            "B_SizeReadable": record.size_readable,
                            "B_SizeBytes": record.size_bytes,
                            "B_Width": record.width,
                            "B_Height": record.height,
                        }
                    )
                else:
                    pairs_truncated = True

        tree.add(record.dhash_int, i)

        if i == 0 or (i + 1) % 250 == 0 or (i + 1) == len(records):
            print_progress(
                "Matching images",
                i + 1,
                len(records),
                extra=f"Pair rows: {len(pair_rows):,}{'+' if pairs_truncated else ''}",
            )

    clear_progress_line()

    root_map: dict[int, list[int]] = {}
    for i in range(len(records)):
        root = dsu.find(i)
        root_map.setdefault(root, []).append(i)

    return {
        root: indices
        for root, indices in root_map.items()
        if len(indices) > 1
    }, pair_rows, pairs_truncated


def group_confidence(indices: list[int], records: list[ImageRecord], keep_idx: int) -> str:
    keep = records[keep_idx]
    max_dh = 0
    max_ah = 0

    for idx in indices:
        if idx == keep_idx:
            continue
        rec = records[idx]
        max_dh = max(max_dh, BKTree.distance(keep.dhash_int, rec.dhash_int))
        max_ah = max(max_ah, BKTree.distance(keep.ahash_int, rec.ahash_int))

    if max_dh <= 4 and max_ah <= 12:
        return "Very High"
    if max_dh <= 10 and max_ah <= 25:
        return "High"
    return "Review Carefully"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_review_csv(
    path: Path,
    groups: dict[int, list[int]],
    records: list[ImageRecord],
) -> tuple[int, int]:
    rows: list[dict[str, Any]] = []
    sorted_groups = sorted(
        groups.values(),
        key=lambda g: (
            -len(g),
            normalize_path_for_compare(records[choose_keep_candidate(g, records)].full_path),
        ),
    )

    delete_suggestions = 0

    for group_number, indices in enumerate(sorted_groups, start=1):
        keep_idx = choose_keep_candidate(indices, records)
        keep = records[keep_idx]
        confidence = group_confidence(indices, records, keep_idx)
        group_id = f"SIMILAR_{group_number:06d}"

        for idx in sorted(
            indices,
            key=lambda i: (
                0 if i == keep_idx else 1,
                -(records[i].width * records[i].height),
                -records[i].size_bytes,
                normalize_path_for_compare(records[i].full_path),
            ),
        ):
            rec = records[idx]
            action = "KEEP" if idx == keep_idx else "DELETE"
            if action == "DELETE":
                delete_suggestions += 1

            rows.append(
                {
                    "GroupId": group_id,
                    "GroupCount": len(indices),
                    "MatchConfidence": confidence,
                    "SuggestedAction": action,
                    "ConfirmDelete": "",
                    "KeepCandidateFullPath": str(keep.full_path),
                    "FullPath": str(rec.full_path),
                    "ParentDirectory": rec.parent_directory,
                    "FileName": rec.file_name,
                    "Extension": rec.extension,
                    "Size": rec.size_readable,
                    "SizeBytes": rec.size_bytes,
                    "Width": rec.width,
                    "Height": rec.height,
                    "Megapixels": rec.megapixels,
                    "DHashDistanceToKeep": BKTree.distance(keep.dhash_int, rec.dhash_int),
                    "AHashDistanceToKeep": BKTree.distance(keep.ahash_int, rec.ahash_int),
                    "AspectRatioDeltaToKeepPercent": round(aspect_ratio_delta(keep, rec) * 100, 4),
                    "ColorDistanceToKeep": round(color_distance(keep, rec), 2),
                    "ExactSHA256": rec.exact_sha256,
                    "DHash256": rec.dhash_hex,
                    "AHash256": rec.ahash_hex,
                    "Notes": "Auto-selected largest resolution/file size as keeper" if idx == keep_idx else "Review visually before confirming delete",
                }
            )

    write_csv(
        path,
        [
            "GroupId",
            "GroupCount",
            "MatchConfidence",
            "SuggestedAction",
            "ConfirmDelete",
            "KeepCandidateFullPath",
            "FullPath",
            "ParentDirectory",
            "FileName",
            "Extension",
            "Size",
            "SizeBytes",
            "Width",
            "Height",
            "Megapixels",
            "DHashDistanceToKeep",
            "AHashDistanceToKeep",
            "AspectRatioDeltaToKeepPercent",
            "ColorDistanceToKeep",
            "ExactSHA256",
            "DHash256",
            "AHash256",
            "Notes",
        ],
        rows,
    )

    return len(sorted_groups), delete_suggestions


def write_pair_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    write_csv(
        path,
        [
            "MatchReason",
            "DHashDistance",
            "AHashDistance",
            "AspectRatioDeltaPercent",
            "ColorDistance",
            "A_FullPath",
            "A_FileName",
            "A_SizeReadable",
            "A_SizeBytes",
            "A_Width",
            "A_Height",
            "B_FullPath",
            "B_FileName",
            "B_SizeReadable",
            "B_SizeBytes",
            "B_Width",
            "B_Height",
        ],
        rows,
    )


def write_inventory_csv(path: Path, records: list[ImageRecord]) -> None:
    rows = [
        {
            "FullPath": str(r.full_path),
            "ParentDirectory": r.parent_directory,
            "FileName": r.file_name,
            "Extension": r.extension,
            "Size": r.size_readable,
            "SizeBytes": r.size_bytes,
            "Width": r.width,
            "Height": r.height,
            "Megapixels": r.megapixels,
            "ExactSHA256": r.exact_sha256,
            "DHash256": r.dhash_hex,
            "AHash256": r.ahash_hex,
            "AvgR": r.avg_r,
            "AvgG": r.avg_g,
            "AvgB": r.avg_b,
        }
        for r in records
    ]

    write_csv(
        path,
        [
            "FullPath",
            "ParentDirectory",
            "FileName",
            "Extension",
            "Size",
            "SizeBytes",
            "Width",
            "Height",
            "Megapixels",
            "ExactSHA256",
            "DHash256",
            "AHash256",
            "AvgR",
            "AvgG",
            "AvgB",
        ],
        rows,
    )


def write_errors_csv(path: Path, errors: list[ErrorRecord]) -> None:
    rows = [
        {
            "FullPath": str(e.full_path),
            "ParentDirectory": e.parent_directory,
            "FileName": e.file_name,
            "Extension": e.extension,
            "Size": e.size_readable,
            "SizeBytes": e.size_bytes,
            "Error": e.error,
        }
        for e in errors
    ]
    write_csv(path, ["FullPath", "ParentDirectory", "FileName", "Extension", "Size", "SizeBytes", "Error"], rows)


def write_summary(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze(args: argparse.Namespace) -> int:
    root = Path(args.root)
    output_folder = Path(args.output_folder)
    cache_db = Path(args.cache_db)
    workers = max(1, int(args.workers or (os.cpu_count() or 4)))
    preset_values = get_preset_values(args.preset)

    dhash_threshold = int(args.dhash_threshold if args.dhash_threshold is not None else preset_values["dhash_threshold"])
    ahash_threshold = int(args.ahash_threshold if args.ahash_threshold is not None else preset_values["ahash_threshold"])
    aspect_tolerance = float(args.aspect_tolerance if args.aspect_tolerance is not None else preset_values["aspect_tolerance"])
    color_threshold = float(args.color_threshold if args.color_threshold is not None else preset_values["color_threshold"])

    stamp = now_stamp()
    review_csv = output_folder / f"{stamp}_similar_images_review.csv"
    pairs_csv = output_folder / f"{stamp}_similar_images_pairs.csv"
    inventory_csv = output_folder / f"{stamp}_image_inventory.csv"
    errors_csv = output_folder / f"{stamp}_read_errors.csv"
    summary_txt = output_folder / f"{stamp}_summary.txt"

    started = dt.datetime.now()
    wall_start = time.time()

    print("Similar Image Duplicate Finder")
    print(f"Mode:                  READ-ONLY analysis")
    print(f"Root folder:           {root}")
    print(f"Output folder:         {output_folder}")
    print(f"Cache DB:              {cache_db}")
    print(f"Workers:               {workers}")
    print(f"Preset:                {args.preset}")
    print(f"DHash threshold:       {dhash_threshold}")
    print(f"AHash threshold:       {ahash_threshold}")
    print(f"Aspect tolerance:      {aspect_tolerance * 100:.3f}%")
    print(f"Color threshold:       {color_threshold}")
    print(f"HEIC/HEIF support:     {'Yes' if HEIF_SUPPORT else 'No'}")
    print("GPU acceleration:      No - CPU/multithreaded hashing is used")
    print("")

    if not root.exists():
        print(f"ERROR: Root folder not found: {root}", file=sys.stderr)
        return 2

    output_folder.mkdir(parents=True, exist_ok=True)

    print("Scanning for image files...")
    files = scan_images(root)
    if args.limit and args.limit > 0:
        files = files[: int(args.limit)]
        print(f"Scan complete. Processing first {len(files):,} files due to -Limit.")
    else:
        print(f"Scan complete. Image files found: {len(files):,}")

    if not files:
        print("No supported images found.")
        return 0

    records, errors, cache_hits, hashed_count = hash_images_with_cache(
        files,
        cache_db=cache_db,
        workers=workers,
        include_exact_hash=not args.skip_exact_hash,
        rebuild_cache=args.rebuild_cache,
    )

    print(f"Images readable:        {len(records):,}")
    print(f"Read errors:            {len(errors):,}")
    print(f"Cache hits:             {cache_hits:,}")
    print(f"Files hashed this run:  {hashed_count:,}")

    if not records:
        print("No images could be read.")
        write_errors_csv(errors_csv, errors)
        return 1

    groups, pair_rows, pairs_truncated = build_groups(
        records,
        dhash_threshold=dhash_threshold,
        ahash_threshold=ahash_threshold,
        aspect_tolerance=aspect_tolerance,
        color_threshold=color_threshold,
        max_pair_rows=int(args.max_pair_rows),
    )

    group_count, delete_suggestions = write_review_csv(review_csv, groups, records)
    write_pair_csv(pairs_csv, pair_rows)
    write_inventory_csv(inventory_csv, records)
    write_errors_csv(errors_csv, errors)

    elapsed = time.time() - wall_start
    ended = dt.datetime.now()

    summary_lines = [
        "Similar Image Duplicate Finder Summary",
        "",
        f"Mode:                    READ-ONLY analysis",
        f"Root folder:             {root}",
        f"Started:                 {started.isoformat(timespec='seconds')}",
        f"Ended:                   {ended.isoformat(timespec='seconds')}",
        f"Elapsed:                 {format_seconds(elapsed)}",
        f"Workers:                 {workers}",
        f"Preset:                  {args.preset}",
        f"DHash threshold:         {dhash_threshold}",
        f"AHash threshold:         {ahash_threshold}",
        f"Aspect tolerance:        {aspect_tolerance * 100:.3f}%",
        f"Color threshold:         {color_threshold}",
        f"HEIC/HEIF support:       {HEIF_SUPPORT}",
        f"GPU acceleration:        No",
        "",
        f"Image files scanned:     {len(files):,}",
        f"Images readable:         {len(records):,}",
        f"Read errors:             {len(errors):,}",
        f"Cache hits:              {cache_hits:,}",
        f"Files hashed this run:   {hashed_count:,}",
        "",
        f"Similar groups found:    {group_count:,}",
        f"Suggested delete rows:   {delete_suggestions:,}",
        f"Pair rows written:       {len(pair_rows):,}{'+' if pairs_truncated else ''}",
        "",
        "Important:",
        "  Analysis mode is read-only.",
        "  No files were deleted, moved, or renamed.",
        "  Edit the review CSV and set ConfirmDelete=CONFIRM only for rows you want deleted.",
        "  Process with: python Find-SimilarImages-ReviewDelete.py -Process -Csv \"<review csv>\" -WhatIf",
        "",
        f"Review CSV:              {review_csv}",
        f"Pair details CSV:        {pairs_csv}",
        f"Inventory CSV:           {inventory_csv}",
        f"Read errors CSV:         {errors_csv}",
        f"Summary TXT:             {summary_txt}",
    ]
    write_summary(summary_txt, summary_lines)

    print("")
    print("Done. No files were deleted, moved, or renamed.")
    print(f"Review CSV:       {review_csv}")
    print(f"Pair details CSV: {pairs_csv}")
    print(f"Inventory CSV:    {inventory_csv}")
    print(f"Read errors CSV:  {errors_csv}")
    print(f"Summary TXT:      {summary_txt}")
    print("")
    print("Next step after review:")
    print(f'python "{Path(__file__).name}" -Process -Csv "{review_csv}" -WhatIf')

    return 0


def read_process_csv(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(row) for row in reader]
        fieldnames = reader.fieldnames or []

    required = {"GroupId", "SuggestedAction", "ConfirmDelete", "FullPath"}
    headers = set(fieldnames)
    missing = sorted(required - headers)
    if missing:
        raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")

    return rows


def process_csv(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    if not args.permanent_delete and not SEND2TRASH_SUPPORT:
        print("ERROR: send2trash is required for Recycle Bin delete. Install with: pip install send2trash", file=sys.stderr)
        print("Or use -PermanentDelete if you intentionally want permanent deletion.", file=sys.stderr)
        return 2

    rows = read_process_csv(csv_path)

    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault((row.get("GroupId") or "").strip(), []).append(row)

    candidates: list[dict[str, str]] = []
    skipped_keep = 0
    skipped_unconfirmed = 0
    skipped_missing = 0
    refused_whole_group = 0

    for group_id, group_rows in groups.items():
        delete_rows = [
            r for r in group_rows
            if (r.get("SuggestedAction") or "").strip().upper() == "DELETE"
            and (r.get("ConfirmDelete") or "").strip().upper() == "CONFIRM"
        ]

        keep_rows = [
            r for r in group_rows
            if (r.get("SuggestedAction") or "").strip().upper() == "KEEP"
        ]

        skipped_unconfirmed += sum(
            1 for r in group_rows
            if (r.get("SuggestedAction") or "").strip().upper() == "DELETE"
            and (r.get("ConfirmDelete") or "").strip().upper() != "CONFIRM"
        )

        skipped_keep += len(keep_rows)

        if not delete_rows:
            continue

        group_file_paths = [
            (r.get("FullPath") or "").strip()
            for r in group_rows
            if (r.get("FullPath") or "").strip()
        ]
        delete_file_paths = [
            (r.get("FullPath") or "").strip()
            for r in delete_rows
            if (r.get("FullPath") or "").strip()
        ]

        if not args.allow_delete_whole_group and set(group_file_paths) and set(group_file_paths) <= set(delete_file_paths):
            refused_whole_group += len(delete_rows)
            print(f"SAFETY SKIP | Group {group_id}: would delete every file in group. Use -AllowDeleteWholeGroup to override.")
            continue

        candidates.extend(delete_rows)

    print("Similar Image Duplicate Processor")
    print(f"Mode:              {'WHATIF / dry run' if args.whatif else 'LIVE delete'}")
    print(f"CSV:               {csv_path}")
    print(f"Delete method:     {'Permanent delete' if args.permanent_delete else 'Recycle Bin'}")
    print(f"Rows in CSV:        {len(rows):,}")
    print(f"Delete candidates:  {len(candidates):,}")
    print(f"Unconfirmed skips:  {skipped_unconfirmed:,}")
    print(f"KEEP row skips:     {skipped_keep:,}")
    print(f"Whole-group skips:  {refused_whole_group:,}")
    print("")

    if not candidates:
        print("No confirmed DELETE rows to process.")
        return 0

    audit_path = csv_path.with_name(csv_path.stem + f"_process_audit_{now_stamp()}.csv")
    audit_rows: list[dict[str, Any]] = []

    deleted = 0
    errors = 0

    for i, row in enumerate(candidates, start=1):
        full_path = (row.get("FullPath") or "").strip()
        path = Path(full_path)

        status = ""
        error = ""

        try:
            if not full_path:
                status = "SKIPPED"
                error = "Missing FullPath"
                skipped_missing += 1
            elif not path.exists():
                status = "SKIPPED"
                error = "File no longer exists"
                skipped_missing += 1
            elif (row.get("SuggestedAction") or "").strip().upper() != "DELETE":
                status = "SKIPPED"
                error = "SuggestedAction is not DELETE"
            elif (row.get("ConfirmDelete") or "").strip().upper() != "CONFIRM":
                status = "SKIPPED"
                error = "ConfirmDelete is not CONFIRM"
            else:
                action = "WHATIF DELETE" if args.whatif else "DELETE"
                clear_progress_line()
                print(f"{action} | {path}")

                if not args.whatif:
                    if args.permanent_delete:
                        path.unlink()
                    else:
                        send2trash(str(path))  # type: ignore[misc]

                status = "WOULD_DELETE" if args.whatif else "DELETED"
                deleted += 1

        except Exception as ex:
            status = "ERROR"
            error = str(ex)
            errors += 1
            clear_progress_line()
            print(f"ERROR | {path}: {ex}")

        audit_rows.append(
            {
                "TimestampUtc": utc_now_text(),
                "Status": status,
                "Error": error,
                "GroupId": row.get("GroupId", ""),
                "FullPath": full_path,
                "FileName": row.get("FileName", ""),
                "Size": row.get("Size", ""),
                "SizeBytes": row.get("SizeBytes", ""),
                "SuggestedAction": row.get("SuggestedAction", ""),
                "ConfirmDelete": row.get("ConfirmDelete", ""),
                "KeepCandidateFullPath": row.get("KeepCandidateFullPath", ""),
            }
        )

        print_progress("Processing deletes", i, len(candidates), extra=f"Processed: {deleted:,} | Errors: {errors:,}")

    clear_progress_line()

    write_csv(
        audit_path,
        [
            "TimestampUtc",
            "Status",
            "Error",
            "GroupId",
            "FullPath",
            "FileName",
            "Size",
            "SizeBytes",
            "SuggestedAction",
            "ConfirmDelete",
            "KeepCandidateFullPath",
        ],
        audit_rows,
    )

    print("")
    print("Summary")
    print(f"{'Would delete' if args.whatif else 'Deleted'}: {deleted:,}")
    print(f"Missing/skipped: {skipped_missing:,}")
    print(f"Errors:          {errors:,}")
    print(f"Audit CSV:       {audit_path}")

    return 0 if errors == 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find visually similar duplicate images and optionally process confirmed deletes from a review CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("-Root", "--root", default=DEFAULT_ROOT, help="Root folder to scan recursively.")
    parser.add_argument("-OutputFolder", "--output-folder", default=str(DEFAULT_OUTPUT_FOLDER), help="Folder for analysis reports.")
    parser.add_argument("-CacheDb", "--cache-db", default=str(DEFAULT_CACHE_DB), help="SQLite cache DB for image hashes.")
    parser.add_argument("-Workers", "--workers", type=int, default=max(1, os.cpu_count() or 4), help="Worker threads for image decoding/hashing.")
    parser.add_argument("-Preset", "--preset", choices=["Strict", "Balanced", "Aggressive"], default="Balanced", help="Matching preset.")
    parser.add_argument("-DHashThreshold", "--dhash-threshold", type=int, default=None, help="Override dHash threshold.")
    parser.add_argument("-AHashThreshold", "--ahash-threshold", type=int, default=None, help="Override aHash threshold.")
    parser.add_argument("-AspectTolerance", "--aspect-tolerance", type=float, default=None, help="Override aspect-ratio tolerance, e.g. 0.025 means 2.5%.")
    parser.add_argument("-ColorThreshold", "--color-threshold", type=float, default=None, help="Override average RGB color distance threshold.")
    parser.add_argument("-Limit", "--limit", type=int, default=0, help="Process only first N scanned images for testing.")
    parser.add_argument("-MaxPairRows", "--max-pair-rows", type=int, default=500000, help="Maximum pair-detail rows to write.")
    parser.add_argument("-RebuildCache", "--rebuild-cache", action="store_true", help="Ignore existing cache and recalculate all hashes.")
    parser.add_argument("-SkipExactHash", "--skip-exact-hash", action="store_true", help="Skip SHA-256 exact duplicate check to reduce disk reads.")

    parser.add_argument("-Process", "--process", action="store_true", help="Process confirmed deletes from a review CSV.")
    parser.add_argument("-Csv", "--csv", default="", help="Review CSV to process when using -Process.")
    parser.add_argument("-WhatIf", "--whatif", "--dry-run", action="store_true", help="Preview delete processing without deleting files.")
    parser.add_argument("-PermanentDelete", "--permanent-delete", action="store_true", help="Permanently delete instead of sending to Recycle Bin.")
    parser.add_argument("-AllowDeleteWholeGroup", "--allow-delete-whole-group", action="store_true", help="Allow deleting every file in a duplicate group.")

    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    if args.process:
        if not args.csv:
            print("ERROR: -Process requires -Csv <review csv path>.", file=sys.stderr)
            return 2
        return process_csv(args)

    return analyze(args)


if __name__ == "__main__":
    raise SystemExit(main())
