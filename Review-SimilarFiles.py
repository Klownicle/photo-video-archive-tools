#!/usr/bin/env python3
r"""
Review-SimilarFiles.py

Local, browser-based reviewer for similar image and similar video review CSVs.

Purpose:
  - Quickly review duplicate/similar image and video groups.
  - Show image thumbnails and playable video previews where the browser supports the video format.
  - Let you confirm suggested DELETE rows.
  - Let you keep one or more images per group.
  - Save a new reviewed CSV.
  - Never delete, move, or rename files.

Install:
  pip install Pillow

Recommended for iPhone HEIC/HEIF:
  pip install pillow-heif

Example:
  python Review-SimilarFiles.py -Csv ".\duplicate_reports\<timestamp>_similar_images_review.csv"

Then process reviewed CSV with the matching processor:
  python Find-SimilarImages-ReviewDelete.py -Process -Csv "<reviewed_image_csv>" -WhatIf
  python Find-SimilarVideos-ReviewDelete.py -Process -Csv "<reviewed_video_csv>" -WhatIf
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


try:
    from PIL import Image, ImageOps, ImageDraw
except ImportError:
    print("ERROR: Pillow is required. Install it with: pip install Pillow", file=sys.stderr)
    raise SystemExit(2)


try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except Exception:
    HEIF_SUPPORT = False


REQUIRED_COLUMNS = {"GroupId", "SuggestedAction", "ConfirmDelete", "FullPath"}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".dng",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".qt",
    ".mpg", ".mpeg", ".mpe",
    ".avi", ".wmv", ".asf",
    ".mkv", ".webm",
    ".3gp", ".3g2",
    ".mts", ".m2ts", ".ts",
    ".mod", ".tod",
}

BROWSER_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".ogg", ".ogv",
}


def media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return "file"


def first_existing(*values: str) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return ""




def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def readable_size_from_bytes(value: str | int | None) -> str:
    try:
        size_bytes = int(value or 0)
    except Exception:
        return ""

    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return str(size_bytes)


def norm_action(value: str) -> str:
    value = (value or "").strip().upper()
    if value == "KEEP":
        return "KEEP"
    if value == "DELETE":
        return "DELETE"
    return value


def norm_confirm(value: str) -> str:
    value = (value or "").strip().upper()
    if value in {"CONFIRM", "CONFIRMED", "YES", "Y"}:
        return "CONFIRM"
    return ""


NEEDS_CATEGORIZED_FOLDER_NAMES = {
    "needs catagorized",   # Legacy/common misspelling supported for compatibility
    "needs categorized",   # Also support corrected spelling
}


def is_needs_categorized_path(path_text: str) -> bool:
    parts = [
        part.strip().lower()
        for part in Path(path_text or "").parts
        if part and part.strip()
    ]
    return any(part in NEEDS_CATEGORIZED_FOLDER_NAMES for part in parts)


def safe_int(value: str | int | None, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return default


def safe_float(value: str | int | float | None, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except Exception:
        return default


def truthy_yes(value: str | None) -> bool:
    return str(value or "").strip().upper() in {"YES", "Y", "TRUE", "1"}


def is_archive_video_row(row: dict[str, str]) -> bool:
    if (row.get("DateFromArchiveName") or "").strip() and (row.get("SequenceFromArchiveName") or "").strip():
        return True
    full_path = row.get("FullPath", "")
    return bool(re.search(r"\d{4}-\d{2}-\d{2}_\d{10}_VID\.[^.\\/]+$", full_path, re.IGNORECASE))


def parse_local_datetime_for_sort(value: str | None) -> float:
    """
    Parse DateModifiedLocal values written by the similar video finder.

    Returns a timestamp where smaller means older. Empty/unparseable values sort
    to the future so they do not win an older-date override.
    """
    text = str(value or "").strip()
    if not text:
        return float("inf")

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m-%d-%Y %H:%M:%S",
        "%m-%d-%Y %H:%M",
        "%m-%d-%Y %I:%M:%S %p",
        "%m-%d-%Y %I:%M %p",
    ]

    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue

    try:
        return dt.datetime.fromisoformat(text.replace("Z", "")).timestamp()
    except Exception:
        return float("inf")


def row_parent_directory(row: dict[str, str]) -> str:
    parent = (row.get("ParentDirectory") or "").strip()
    if parent:
        return os.path.normcase(parent)
    full_path = (row.get("FullPath") or "").strip()
    if full_path:
        return os.path.normcase(str(Path(full_path).parent))
    return ""


def row_size_bytes(row: dict[str, str]) -> int:
    return safe_int(first_existing(row.get("FileSizeBytes", ""), row.get("SizeBytes", "")), 0)


def same_directory_same_size_oldest_modified_candidate(
    row_ids: list[int],
    rows_by_id: list[dict[str, str]],
) -> int | None:
    """
    If every row in a group is in the same directory and has the same non-zero
    file size, prefer the oldest Date Modified file.

    This is useful when the files are effectively the same local duplicate set:
    same directory + same size usually means there is no quality/bitrate reason
    to keep a newer copy over an older/original-timestamp copy.
    """
    if len(row_ids) < 2:
        return None

    rows = [rows_by_id[rid] for rid in row_ids]
    parent_dirs = {row_parent_directory(row) for row in rows if row_parent_directory(row)}
    sizes = {row_size_bytes(row) for row in rows if row_size_bytes(row) > 0}

    if len(parent_dirs) != 1 or len(sizes) != 1:
        return None

    candidates = []
    for rid in row_ids:
        row = rows_by_id[rid]
        modified_ts = parse_local_datetime_for_sort(row.get("DateModifiedLocal", ""))
        if modified_ts != float("inf"):
            candidates.append((modified_ts, rid))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def video_bitrate_kbps_from_row(row: dict[str, str]) -> float:
    explicit = safe_float(row.get("EstimatedBitrateKbps", ""), 0.0)
    if explicit > 0:
        return explicit

    size_bytes = safe_float(first_existing(row.get("FileSizeBytes", ""), row.get("SizeBytes", "")), 0.0)
    duration = safe_float(first_existing(row.get("DurationSeconds", ""), row.get("Duration", "")), 0.0)
    if size_bytes <= 0 or duration <= 0:
        return 0.0

    return (size_bytes * 8.0) / duration / 1000.0


def video_keeper_sort_key(row_id: int, row: dict[str, str]) -> tuple[int, int, float, int, int, str]:
    """
    Preferred video keeper ordering for compressed/similar video groups.

    Lower tuple wins:
      1. archive-renamed video filename
      2. has sidecar
      3. higher estimated bitrate / larger effective file size
      4. higher resolution
      5. shorter path
      6. alphabetical path
    """
    full_path = row.get("FullPath", "")
    archive_rank = 0 if is_archive_video_row(row) else 1
    sidecar_rank = 0 if truthy_yes(row.get("HasSidecar", "")) or bool((row.get("SidecarPath") or "").strip()) else 1

    bitrate_rank = -video_bitrate_kbps_from_row(row)
    width = safe_int(row.get("Width", ""), 0)
    height = safe_int(row.get("Height", ""), 0)
    pixels_rank = -(width * height)

    path_len = len(full_path)
    return (
        archive_rank,
        sidecar_rank,
        bitrate_rank,
        pixels_rank,
        path_len,
        full_path.casefold(),
    )


def is_client_disconnect(ex: BaseException) -> bool:
    """
    Browsers commonly abort media requests when scrolling, changing groups,
    or deciding a video format is not playable. Those are not reviewer errors.
    """
    if isinstance(ex, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
        return True

    text = str(ex)
    return (
        "WinError 10053" in text
        or "WinError 10054" in text
        or "Broken pipe" in text
        or "Connection reset" in text
        or "Connection aborted" in text
    )


class ReviewStore:
    def __init__(
        self,
        csv_path: Path,
        output_csv: Path | None = None,
        *,
        auto_video_keeper_preference: bool = True,
    ) -> None:
        self.csv_path = csv_path
        self.output_csv = output_csv or csv_path.with_name(csv_path.stem + f"_reviewed_{now_stamp()}.csv")
        self.auto_video_keeper_preference = auto_video_keeper_preference
        self.lock = threading.RLock()

        self.fieldnames: list[str] = []
        self.rows: list[dict[str, str]] = []
        self.group_order: list[str] = []
        self.group_to_row_ids: dict[str, list[int]] = {}

        self.load()

    def load(self) -> None:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {self.csv_path}")

        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            missing = sorted(REQUIRED_COLUMNS - set(fieldnames))
            if missing:
                raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")

            self.fieldnames = fieldnames
            self.rows = []

            for row_id, row in enumerate(reader):
                normalized = {name: (row.get(name, "") or "") for name in self.fieldnames}
                normalized["__RowId"] = str(row_id)
                normalized["SuggestedAction"] = norm_action(normalized.get("SuggestedAction", ""))
                normalized["ConfirmDelete"] = norm_confirm(normalized.get("ConfirmDelete", ""))
                self.rows.append(normalized)

        self.rebuild_groups()
        self.apply_video_keeper_preference()
        self.apply_needs_categorized_overrides()

    def rebuild_groups(self) -> None:
        self.group_order = []
        self.group_to_row_ids = {}

        for row_id, row in enumerate(self.rows):
            group_id = (row.get("GroupId") or "").strip() or "NO_GROUP"
            if group_id not in self.group_to_row_ids:
                self.group_order.append(group_id)
                self.group_to_row_ids[group_id] = []
            self.group_to_row_ids[group_id].append(row_id)

    def clear_auto_override_marks(self, group_id: str) -> None:
        for rid in self.group_to_row_ids.get(group_id, []):
            self.rows[rid].pop("__AutoOverrideRole", None)
            self.rows[rid].pop("__AutoOverrideReason", None)

    def group_is_fully_confirmed(self, row_ids: list[int]) -> bool:
        delete_rows = [
            self.rows[rid]
            for rid in row_ids
            if norm_action(self.rows[rid].get("SuggestedAction", "")) == "DELETE"
        ]
        if not delete_rows:
            return False

        return all(norm_confirm(row.get("ConfirmDelete", "")) == "CONFIRM" for row in delete_rows)

    def apply_video_keeper_preference(self) -> None:
        """
        Auto-suggest better keepers for video review CSVs without rerunning the
        expensive video finder.

        This only changes the working review state in the browser. Nothing is
        saved until Save reviewed CSV is clicked.

        Safety rules:
          - only video groups
          - do not touch groups with any confirmed delete rows
          - do not touch fully confirmed groups
          - do not touch groups with zero or multiple current keepers
          - only change the keeper if the preferred row is different
        """
        if not self.auto_video_keeper_preference:
            return

        for group_id in self.group_order:
            row_ids = self.group_to_row_ids.get(group_id, [])
            if len(row_ids) < 2:
                continue

            rows = [self.rows[rid] for rid in row_ids]

            # Only apply to groups where at least one row looks like a video row.
            if not any(media_type_for_path(Path(row.get("FullPath", ""))) == "video" for row in rows):
                continue

            if self.group_is_fully_confirmed(row_ids):
                continue

            if any(norm_confirm(row.get("ConfirmDelete", "")) == "CONFIRM" for row in rows):
                continue

            keep_rids = [
                rid for rid in row_ids
                if norm_action(self.rows[rid].get("SuggestedAction", "")) == "KEEP"
            ]

            if len(keep_rids) != 1:
                continue

            current_keep_rid = keep_rids[0]

            same_dir_size_rid = same_directory_same_size_oldest_modified_candidate(row_ids, self.rows)
            if same_dir_size_rid is not None:
                preferred_rid = same_dir_size_rid
                override_reason_short = (
                    "Auto same-directory/same-size keeper preference: selected the older Date Modified file."
                )
                note_prefix = "AUTO SAME-DIRECTORY SAME-SIZE KEEPER SUGGESTED"
            else:
                preferred_rid = min(row_ids, key=lambda rid: video_keeper_sort_key(rid, self.rows[rid]))
                override_reason_short = (
                    "Auto video keeper preference: selected as keeper because it ranks better by "
                    "archive name, sidecar, estimated bitrate/file size, resolution, and path."
                )
                note_prefix = "AUTO VIDEO KEEPER SUGGESTED"

            if preferred_rid == current_keep_rid:
                continue

            current_keep = self.rows[current_keep_rid]
            preferred = self.rows[preferred_rid]

            current_bitrate = video_bitrate_kbps_from_row(current_keep)
            preferred_bitrate = video_bitrate_kbps_from_row(preferred)
            current_size = row_size_bytes(current_keep)
            preferred_size = row_size_bytes(preferred)
            current_pixels = safe_int(current_keep.get("Width", ""), 0) * safe_int(current_keep.get("Height", ""), 0)
            preferred_pixels = safe_int(preferred.get("Width", ""), 0) * safe_int(preferred.get("Height", ""), 0)
            current_modified = current_keep.get("DateModifiedLocal", "")
            preferred_modified = preferred.get("DateModifiedLocal", "")

            self.clear_auto_override_marks(group_id)

            preferred["SuggestedAction"] = "KEEP"
            preferred["ConfirmDelete"] = ""
            preferred["__AutoOverrideRole"] = "OVERRIDE_KEEP"
            preferred["__AutoOverrideReason"] = override_reason_short
            preferred["Notes"] = (
                f"{note_prefix}: selected as keeper using reviewer-side preference. "
                f"Preferred Date Modified: {preferred_modified}; prior keeper Date Modified: {current_modified}. "
                f"Preferred bitrate/file size: {preferred_bitrate:.2f} kbps / {preferred_size} bytes; "
                f"prior keeper: {current_bitrate:.2f} kbps / {current_size} bytes. "
                f"Preferred pixels: {preferred_pixels}; prior keeper pixels: {current_pixels}."
            )

            current_keep["SuggestedAction"] = "DELETE"
            current_keep["ConfirmDelete"] = ""
            current_keep["__AutoOverrideRole"] = "OVERRIDE_ASSUMED_DELETE"
            current_keep["__AutoOverrideReason"] = (
                "Auto keeper preference: former keeper changed to DELETE suggestion; confirmation still required."
            )
            current_keep["Notes"] = (
                "AUTO ASSUMED DELETE: was the prior keeper, but reviewer-side keeper preference selected another file. "
                "Deletion is not confirmed until you confirm this row/group."
            )

            self.sync_keep_candidate_paths(group_id)


    def apply_needs_categorized_overrides(self) -> None:
        """
        Auto-suggest a keeper override for unconfirmed groups where the current
        single keeper is inside a 'Needs Categorized' folder, but a same-group
        duplicate exists outside that folder.

        This does not confirm deletes. It only changes the working review state:
          - largest non-Needs-Categorized file becomes KEEP
          - the former Needs-Categorized keeper becomes DELETE, unconfirmed
          - rows are visually marked as OVERRIDE in the interface

        Safety rules:
          - Do not touch groups that are already fully confirmed.
          - Do not touch groups with any confirmed delete rows.
          - Do not touch groups with zero or multiple current keepers.
          - Only override if the non-Needs-Categorized candidate is at least as
            large as the current Needs-Categorized keeper.
        """
        for group_id in self.group_order:
            row_ids = self.group_to_row_ids.get(group_id, [])
            if not row_ids:
                continue

            rows = [self.rows[rid] for rid in row_ids]

            if self.group_is_fully_confirmed(row_ids):
                continue

            if any(norm_confirm(row.get("ConfirmDelete", "")) == "CONFIRM" for row in rows):
                continue

            keep_rids = [
                rid for rid in row_ids
                if norm_action(self.rows[rid].get("SuggestedAction", "")) == "KEEP"
            ]

            if len(keep_rids) != 1:
                continue

            current_keep_rid = keep_rids[0]
            current_keep = self.rows[current_keep_rid]
            current_keep_path = current_keep.get("FullPath", "")

            if not is_needs_categorized_path(current_keep_path):
                continue

            outside_candidates = [
                rid for rid in row_ids
                if not is_needs_categorized_path(self.rows[rid].get("FullPath", ""))
            ]

            if not outside_candidates:
                continue

            # Prefer largest file outside Needs Categorized. Tie-break on dimensions if available.
            def candidate_key(rid: int) -> tuple[int, int, int]:
                row = self.rows[rid]
                size = safe_int(row.get("SizeBytes", "0"))
                width = safe_int(row.get("Width", "0"))
                height = safe_int(row.get("Height", "0"))
                return (size, width * height, rid)

            override_rid = max(outside_candidates, key=candidate_key)
            override_row = self.rows[override_rid]

            keep_size = safe_int(current_keep.get("SizeBytes", "0"))
            override_size = safe_int(override_row.get("SizeBytes", "0"))

            # Do not override if the Needs Categorized keeper is larger.
            if keep_size > override_size:
                continue

            self.clear_auto_override_marks(group_id)

            # Make the non-Needs duplicate the suggested keeper.
            override_row["SuggestedAction"] = "KEEP"
            override_row["ConfirmDelete"] = ""
            override_row["__AutoOverrideRole"] = "OVERRIDE_KEEP"
            override_row["__AutoOverrideReason"] = (
                "Auto override: largest duplicate outside Needs Categorized selected as keeper."
            )
            override_row["Notes"] = (
                "AUTO OVERRIDE SUGGESTED: selected as keeper because it is outside "
                "Needs Categorized and is the largest eligible duplicate."
            )

            # The old Needs Categorized keeper is assumed DELETE, but not confirmed.
            current_keep["SuggestedAction"] = "DELETE"
            current_keep["ConfirmDelete"] = ""
            current_keep["__AutoOverrideRole"] = "OVERRIDE_ASSUMED_DELETE"
            current_keep["__AutoOverrideReason"] = (
                "Auto override: former Needs Categorized keeper assumed duplicate/delete; confirmation still required."
            )
            current_keep["Notes"] = (
                "AUTO OVERRIDE ASSUMED DELETE: was the prior keeper in Needs Categorized. "
                "Deletion is not confirmed until the group is confirmed."
            )

            self.sync_keep_candidate_paths(group_id)

    def group_count(self) -> int:
        return len(self.group_order)

    def row_count(self) -> int:
        return len(self.rows)

    def get_row(self, row_id: int) -> dict[str, str]:
        return self.rows[row_id]

    def group_summary(self, group_id: str, index: int) -> dict[str, Any]:
        row_ids = self.group_to_row_ids[group_id]
        rows = [self.rows[i] for i in row_ids]

        keep_count = sum(1 for r in rows if norm_action(r.get("SuggestedAction", "")) == "KEEP")
        delete_count = sum(1 for r in rows if norm_action(r.get("SuggestedAction", "")) == "DELETE")
        confirmed_count = sum(
            1 for r in rows
            if norm_action(r.get("SuggestedAction", "")) == "DELETE"
            and norm_confirm(r.get("ConfirmDelete", "")) == "CONFIRM"
        )
        confidence = next((r.get("MatchConfidence", "") for r in rows if r.get("MatchConfidence", "")), "")
        first_keep = next((r for r in rows if norm_action(r.get("SuggestedAction", "")) == "KEEP"), rows[0])
        has_override = any(r.get("__AutoOverrideRole", "") for r in rows)
        override_keep = next((r for r in rows if r.get("__AutoOverrideRole", "") == "OVERRIDE_KEEP"), None)

        # Used by the sidebar text filter. This intentionally includes every file
        # in the group, not just the keeper, so a filename from WhatIf output can
        # be pasted into the filter to find the associated group.
        searchable_parts = []
        for r in rows:
            full_path = r.get("FullPath", "")
            searchable_parts.extend([
                full_path,
                r.get("FileName", ""),
                r.get("ParentDirectory", ""),
                Path(full_path).name if full_path else "",
                str(Path(full_path).parent) if full_path else "",
            ])
        searchable_text = " ".join(part for part in searchable_parts if part)

        return {
            "index": index,
            "groupId": group_id,
            "rowCount": len(rows),
            "keepCount": keep_count,
            "deleteCount": delete_count,
            "confirmedDeleteCount": confirmed_count,
            "matchConfidence": confidence,
            "keepFileName": first_keep.get("FileName", Path(first_keep.get("FullPath", "")).name),
            "keepFullPath": first_keep.get("FullPath", ""),
            "hasOverride": has_override,
            "overrideKeepFileName": (
                override_keep.get("FileName", Path(override_keep.get("FullPath", "")).name)
                if override_keep else ""
            ),
            "overrideKeepFullPath": override_keep.get("FullPath", "") if override_keep else "",
            "searchableText": searchable_text,
            "isComplete": delete_count > 0 and confirmed_count == delete_count,
            "hasAnyConfirmed": confirmed_count > 0,
        }

    def state(self) -> dict[str, Any]:
        with self.lock:
            groups = [
                self.group_summary(group_id, index)
                for index, group_id in enumerate(self.group_order)
            ]
            total_delete_rows = sum(g["deleteCount"] for g in groups)
            total_confirmed_rows = sum(g["confirmedDeleteCount"] for g in groups)

            return {
                "sourceCsv": str(self.csv_path),
                "outputCsv": str(self.output_csv),
                "groupCount": len(groups),
                "rowCount": len(self.rows),
                "totalDeleteRows": total_delete_rows,
                "totalConfirmedDeleteRows": total_confirmed_rows,
                "heifSupport": HEIF_SUPPORT,
                "supportsVideos": True,
                "autoVideoKeeperPreference": self.auto_video_keeper_preference,
                "groups": groups,
            }

    def group(self, index: int) -> dict[str, Any]:
        with self.lock:
            if not self.group_order:
                return {
                    "index": 0,
                    "groupId": "",
                    "groupCount": 0,
                    "summary": {},
                    "rows": [],
                }

            index = max(0, min(index, len(self.group_order) - 1))
            group_id = self.group_order[index]
            row_ids = self.group_to_row_ids[group_id]
            rows = [self.rows[i] for i in row_ids]

            return {
                "index": index,
                "groupId": group_id,
                "groupCount": len(self.group_order),
                "summary": self.group_summary(group_id, index),
                "rows": [self.public_row(row_id, row) for row_id, row in zip(row_ids, rows)],
            }

    def public_row(self, row_id: int, row: dict[str, str]) -> dict[str, Any]:
        path = Path(row.get("FullPath", ""))
        exists = path.exists()
        media_type = media_type_for_path(path)

        size = row.get("Size", "")
        if not size:
            size = readable_size_from_bytes(first_existing(row.get("SizeBytes", ""), row.get("FileSizeBytes", "")))

        size_bytes = first_existing(row.get("SizeBytes", ""), row.get("FileSizeBytes", ""))

        notes = first_existing(
            row.get("Notes", ""),
            row.get("DuplicateReason", ""),
            row.get("KeepReason", ""),
            row.get("Reason", ""),
        )

        duration = first_existing(row.get("DurationSeconds", ""), row.get("Duration", ""))
        if duration:
            try:
                duration = f"{float(duration):.3f}s"
            except Exception:
                pass

        return {
            "rowId": row_id,
            "groupId": row.get("GroupId", ""),
            "suggestedAction": norm_action(row.get("SuggestedAction", "")),
            "confirmDelete": norm_confirm(row.get("ConfirmDelete", "")),
            "fullPath": row.get("FullPath", ""),
            "parentDirectory": row.get("ParentDirectory", str(path.parent) if str(path) else ""),
            "fileName": row.get("FileName", path.name),
            "extension": first_existing(row.get("Extension", ""), path.suffix.lower()),
            "mediaType": media_type,
            "browserPlayableVideo": "YES" if path.suffix.lower() in BROWSER_VIDEO_EXTENSIONS else "NO",
            "size": size,
            "sizeBytes": size_bytes,
            "width": row.get("Width", ""),
            "height": row.get("Height", ""),
            "megapixels": row.get("Megapixels", ""),
            "duration": duration,
            "dateModifiedLocal": row.get("DateModifiedLocal", ""),
            "estimatedBitrateKbps": f"{video_bitrate_kbps_from_row(row):.2f}" if media_type == "video" else "",
            "durationDeltaSeconds": row.get("DurationDeltaSeconds", ""),
            "dateModifiedDeltaDays": row.get("DateModifiedDeltaDays", ""),
            "dateModifiedClose": row.get("DateModifiedClose", ""),
            "similarityScore": row.get("SimilarityScore", ""),
            "averageFrameHashDistance": row.get("AverageFrameHashDistance", ""),
            "maxFrameHashDistance": row.get("MaxFrameHashDistance", ""),
            "dHashDistanceToKeep": row.get("DHashDistanceToKeep", ""),
            "aHashDistanceToKeep": row.get("AHashDistanceToKeep", ""),
            "aspectRatioDeltaToKeepPercent": row.get("AspectRatioDeltaToKeepPercent", ""),
            "colorDistanceToKeep": row.get("ColorDistanceToKeep", ""),
            "matchConfidence": row.get("MatchConfidence", ""),
            "notes": notes,
            "hasSidecar": row.get("HasSidecar", ""),
            "sidecarPath": row.get("SidecarPath", ""),
            "sidecarSizeBytes": row.get("SidecarSizeBytes", ""),
            "autoOverrideRole": row.get("__AutoOverrideRole", ""),
            "autoOverrideReason": row.get("__AutoOverrideReason", ""),
            "isNeedsCategorized": is_needs_categorized_path(row.get("FullPath", "")),
            "exists": exists,
        }

    def keeper_paths_for_group(self, group_id: str) -> list[str]:
        row_ids = self.group_to_row_ids.get(group_id, [])
        return [
            self.rows[rid].get("FullPath", "")
            for rid in row_ids
            if norm_action(self.rows[rid].get("SuggestedAction", "")) == "KEEP"
            and self.rows[rid].get("FullPath", "")
        ]

    def sync_keep_candidate_paths(self, group_id: str) -> None:
        """
        KeepCandidateFullPath is informational for review. The delete processor only
        requires SuggestedAction=DELETE and ConfirmDelete=CONFIRM, but keeping this
        column useful makes the CSV easier to audit.
        """
        keep_paths = self.keeper_paths_for_group(group_id)
        if not keep_paths:
            keep_text = ""
        elif len(keep_paths) == 1:
            keep_text = keep_paths[0]
        else:
            keep_text = "MULTIPLE_KEEPERS: " + " | ".join(keep_paths)

        for rid in self.group_to_row_ids.get(group_id, []):
            self.rows[rid]["KeepCandidateFullPath"] = keep_text

    def set_row_action(self, row_id: int, action: str, confirm_delete: str | None = None) -> dict[str, Any]:
        with self.lock:
            row = self.rows[row_id]
            action = norm_action(action)
            if action not in {"KEEP", "DELETE"}:
                raise ValueError("SuggestedAction must be KEEP or DELETE.")

            group_id = row.get("GroupId", "")
            self.clear_auto_override_marks(group_id)

            if action == "DELETE":
                existing_keepers = [
                    rid for rid in self.group_to_row_ids.get(group_id, [])
                    if norm_action(self.rows[rid].get("SuggestedAction", "")) == "KEEP"
                ]
                if row_id in existing_keepers and len(existing_keepers) <= 1:
                    raise ValueError("Cannot mark the last KEEP row as DELETE. Select another keeper first.")

            row["SuggestedAction"] = action
            if action == "KEEP":
                row["ConfirmDelete"] = ""
                row["Notes"] = "User marked this row as KEEP in visual reviewer"
            elif confirm_delete is not None:
                row["ConfirmDelete"] = norm_confirm(confirm_delete)
            elif action == "DELETE":
                # Switching from KEEP to DELETE should not auto-confirm deletion.
                row["ConfirmDelete"] = ""
                row["Notes"] = "User marked this row as DELETE in visual reviewer"

            self.sync_keep_candidate_paths(group_id)

            group_index = self.group_order.index(group_id)
            return self.group(group_index)

    def set_keep(self, group_id: str, row_id: int) -> dict[str, Any]:
        """
        Add this row as a keeper without forcing the rest of the group to DELETE.
        This allows multiple keepers per group.
        """
        with self.lock:
            if group_id not in self.group_to_row_ids:
                raise ValueError(f"Group not found: {group_id}")

            if row_id not in self.group_to_row_ids[group_id]:
                raise ValueError("Row is not in the requested group.")

            self.clear_auto_override_marks(group_id)

            row = self.rows[row_id]
            row["SuggestedAction"] = "KEEP"
            row["ConfirmDelete"] = ""
            row["Notes"] = "User added this row as an additional keeper in visual reviewer"

            self.sync_keep_candidate_paths(group_id)

            group_index = self.group_order.index(group_id)
            return self.group(group_index)

    def confirm_group_deletes(self, group_id: str) -> dict[str, Any]:
        with self.lock:
            if group_id not in self.group_to_row_ids:
                raise ValueError(f"Group not found: {group_id}")

            for rid in self.group_to_row_ids[group_id]:
                row = self.rows[rid]
                if norm_action(row.get("SuggestedAction", "")) == "DELETE":
                    row["ConfirmDelete"] = "CONFIRM"
                else:
                    row["ConfirmDelete"] = ""

            group_index = self.group_order.index(group_id)
            return self.group(group_index)

    def confirm_remaining_group_deletes(self, group_indexes: list[int] | None = None) -> dict[str, Any]:
        """
        Confirm all current DELETE rows for the selected remaining groups.
        This applies the current working KEEP/DELETE state, including any
        Needs Categorized override suggestions. It does not save the CSV.
        """
        with self.lock:
            if group_indexes is None:
                target_group_ids = list(self.group_order)
            else:
                target_group_ids = []
                for idx in group_indexes:
                    try:
                        idx_int = int(idx)
                    except Exception:
                        continue
                    if 0 <= idx_int < len(self.group_order):
                        target_group_ids.append(self.group_order[idx_int])

            # Deduplicate while preserving order.
            seen = set()
            target_group_ids = [
                gid for gid in target_group_ids
                if not (gid in seen or seen.add(gid))
            ]

            groups_considered = 0
            groups_changed = 0
            delete_rows_confirmed = 0
            override_groups_confirmed = 0

            for group_id in target_group_ids:
                row_ids = self.group_to_row_ids.get(group_id, [])
                if not row_ids:
                    continue

                delete_row_ids = [
                    rid for rid in row_ids
                    if norm_action(self.rows[rid].get("SuggestedAction", "")) == "DELETE"
                ]

                if not delete_row_ids:
                    continue

                already_complete = all(
                    norm_confirm(self.rows[rid].get("ConfirmDelete", "")) == "CONFIRM"
                    for rid in delete_row_ids
                )

                groups_considered += 1

                if already_complete:
                    continue

                if any(self.rows[rid].get("__AutoOverrideRole", "") for rid in row_ids):
                    override_groups_confirmed += 1

                changed_this_group = False

                for rid in row_ids:
                    row = self.rows[rid]
                    if norm_action(row.get("SuggestedAction", "")) == "DELETE":
                        if norm_confirm(row.get("ConfirmDelete", "")) != "CONFIRM":
                            delete_rows_confirmed += 1
                            changed_this_group = True
                        row["ConfirmDelete"] = "CONFIRM"
                    else:
                        row["ConfirmDelete"] = ""

                if changed_this_group:
                    groups_changed += 1

            return {
                "ok": True,
                "groupsConsidered": groups_considered,
                "groupsChanged": groups_changed,
                "deleteRowsConfirmed": delete_rows_confirmed,
                "overrideGroupsConfirmed": override_groups_confirmed,
            }

    def clear_group_confirms(self, group_id: str) -> dict[str, Any]:
        with self.lock:
            if group_id not in self.group_to_row_ids:
                raise ValueError(f"Group not found: {group_id}")

            for rid in self.group_to_row_ids[group_id]:
                self.rows[rid]["ConfirmDelete"] = ""

            group_index = self.group_order.index(group_id)
            return self.group(group_index)

    def save(self, output_csv: Path | None = None) -> Path:
        with self.lock:
            out = output_csv or self.output_csv
            out.parent.mkdir(parents=True, exist_ok=True)

            fieldnames = [name for name in self.fieldnames if not name.startswith("__")]
            for required in ["SuggestedAction", "ConfirmDelete"]:
                if required not in fieldnames:
                    fieldnames.append(required)

            with out.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for row in self.rows:
                    clean = {name: row.get(name, "") for name in fieldnames}
                    writer.writerow(clean)

            self.output_csv = out
            return out


class Thumbnailer:
    def __init__(
        self,
        cache_dir: Path,
        thumb_size: int = 384,
        preview_size: int = 1600,
        thumb_quality: int = 78,
        max_cache_mb: int = 512,
        cleanup_interval_seconds: int = 60,
    ) -> None:
        self.cache_dir = cache_dir
        self.thumb_size = thumb_size
        self.preview_size = preview_size
        self.thumb_quality = max(40, min(int(thumb_quality), 95))
        self.max_cache_bytes = max(0, int(max_cache_mb)) * 1024 * 1024
        self.cleanup_interval_seconds = max(10, int(cleanup_interval_seconds))
        self.last_cleanup = 0.0
        self.lock = threading.RLock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_key(self, path: Path, max_size: int) -> str:
        try:
            stat = path.stat()
            source = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{max_size}"
        except OSError:
            source = f"{path}|missing|{max_size}"
        return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()

    def placeholder(self, path: Path, max_size: int, message: str) -> Path:
        key = hashlib.sha256(f"placeholder|{path}|{max_size}|{message}".encode("utf-8", errors="replace")).hexdigest()
        out = self.cache_dir / f"{key}.jpg"
        if out.exists():
            return out

        width = max(240, min(max_size, 640))
        height = max(180, int(width * 0.65))
        img = Image.new("RGB", (width, height), (245, 245, 245))
        draw = ImageDraw.Draw(img)
        text = f"Preview unavailable\n\n{path.name}\n\n{message}"
        draw.multiline_text((16, 16), text, fill=(60, 60, 60), spacing=6)
        img.save(out, "JPEG", quality=82)
        return out

    def cleanup_cache_if_needed(self) -> None:
        """
        Keep the thumbnail cache bounded. This preserves speed for recent groups
        while preventing the cache from growing forever across large reviews.
        """
        if self.max_cache_bytes <= 0:
            return

        now = __import__("time").time()
        if now - self.last_cleanup < self.cleanup_interval_seconds:
            return

        self.last_cleanup = now

        try:
            files = []
            total = 0
            for item in self.cache_dir.glob("*.jpg"):
                try:
                    stat = item.stat()
                    total += stat.st_size
                    files.append((stat.st_atime_ns, stat.st_mtime_ns, stat.st_size, item))
                except OSError:
                    continue

            if total <= self.max_cache_bytes:
                return

            # Remove oldest accessed files first until the cache is under 85% of the limit.
            target = int(self.max_cache_bytes * 0.85)
            files.sort(key=lambda x: (x[0], x[1]))

            for _atime, _mtime, size, item in files:
                if total <= target:
                    break
                try:
                    item.unlink()
                    total -= size
                except OSError:
                    pass

        except Exception:
            # Cache cleanup should never break image review.
            return

    def get_image(self, path: Path, max_size: int) -> Path:
        if not path.exists():
            return self.placeholder(path, max_size, "File not found")

        key = self.cache_key(path, max_size)
        out = self.cache_dir / f"{key}.jpg"
        if out.exists():
            return out

        with self.lock:
            if out.exists():
                return out

            try:
                with Image.open(path) as img:
                    try:
                        img.seek(0)
                    except Exception:
                        pass

                    # For JPEGs, draft() can ask Pillow/libjpeg to decode at a lower
                    # resolution, which is much faster for thumbnails.
                    try:
                        img.draft("RGB", (max_size, max_size))
                    except Exception:
                        pass

                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((max_size, max_size), Image.Resampling.BILINEAR)

                    if img.mode in {"RGBA", "LA"}:
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        alpha = img.getchannel("A") if "A" in img.getbands() else None
                        bg.paste(img.convert("RGBA"), mask=alpha)
                        img = bg
                    else:
                        img = img.convert("RGB")

                    img.save(out, "JPEG", quality=self.thumb_quality, optimize=True)
                    self.cleanup_cache_if_needed()
                    return out

            except Exception as ex:
                return self.placeholder(path, max_size, str(ex))


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Similar File Reviewer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #1f2937;
  --muted: #6b7280;
  --border: #d7dce3;
  --keep: #0f766e;
  --delete: #b91c1c;
  --confirm: #7c2d12;
  --button: #111827;
  --buttonText: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}
header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 10px 14px;
}
.topline {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
button, select, input {
  font: inherit;
}
button {
  border: 1px solid #111827;
  background: var(--button);
  color: var(--buttonText);
  padding: 7px 10px;
  border-radius: 8px;
  cursor: pointer;
}
button.secondary {
  background: #fff;
  color: #111827;
  border-color: var(--border);
}
button.danger {
  background: #991b1b;
  border-color: #991b1b;
}
button.good {
  background: #0f766e;
  border-color: #0f766e;
}
button.warn {
  background: #9a3412;
  border-color: #9a3412;
}
button:disabled {
  opacity: .5;
  cursor: not-allowed;
}
input[type="number"], input[type="text"], select {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px 8px;
  background: #fff;
}
main {
  display: grid;
  grid-template-columns: 330px 1fr;
  gap: 14px;
  padding: 14px;
  align-items: start;
}
aside {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  height: calc(100vh - 116px);
  max-height: calc(100vh - 116px);
  position: sticky;
  top: 88px;
  display: flex;
  flex-direction: column;
}
.sidebar-head {
  padding: 10px;
  border-bottom: 1px solid var(--border);
  flex: 0 0 auto;
}
.group-list {
  overflow-y: auto;
  overflow-x: hidden;
  flex: 1 1 auto;
  min-height: 0;
  padding-bottom: 72px;
  scrollbar-gutter: stable;
}
.group-item {
  padding: 9px 10px;
  border-bottom: 1px solid #edf0f3;
  cursor: pointer;
  font-size: 13px;
}
.group-item:hover { background: #f3f4f6; }
.group-item.active { background: #e0f2fe; }
.group-item.complete { border-left: 5px solid var(--keep); }
.group-item.partial { border-left: 5px solid var(--confirm); }
.group-item .gid { font-weight: 700; }
.group-item .small { color: var(--muted); margin-top: 3px; }
.content {
  min-width: 0;
}
.status {
  color: var(--muted);
  font-size: 13px;
  margin-left: auto;
}
.group-title {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  margin-bottom: 14px;
}
.group-title h2 {
  margin: 0 0 8px 0;
}
.meta {
  color: var(--muted);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: 14px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
.card.keep {
  outline: 3px solid rgba(15,118,110,.25);
}
.card.delete.confirmed {
  outline: 3px solid rgba(185,28,28,.30);
}
.thumb-wrap {
  background:
    linear-gradient(90deg, #111827 0%, #1f2937 50%, #111827 100%);
  background-size: 200% 100%;
  animation: shimmer 1.2s ease-in-out infinite;
  height: 270px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.thumb-wrap img {
  max-width: 100%;
  max-height: 270px;
  object-fit: contain;
  cursor: zoom-in;
  opacity: 0;
  transition: opacity .18s ease-in;
}
.thumb-wrap img.loaded {
  opacity: 1;
}

.thumb-wrap video {
  width: 100%;
  max-height: 270px;
  background: #000;
}
.video-unavailable {
  color: #f9fafb;
  font-size: 13px;
  text-align: center;
  padding: 14px;
  overflow-wrap: anywhere;
}
.media-kind {
  font-size: 11px;
  text-transform: uppercase;
  color: #6b7280;
  font-weight: 700;
  letter-spacing: .04em;
}

@keyframes shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
.card-body {
  padding: 10px;
}
.badges {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.badge {
  display: inline-block;
  font-size: 12px;
  border-radius: 999px;
  padding: 3px 8px;
  color: #fff;
}
.badge.keep { background: var(--keep); }
.badge.delete { background: var(--delete); }
.badge.confirm { background: var(--confirm); }
.badge.missing { background: #4b5563; }
.badge.override { background: #7c3aed; }
.badge.assumed { background: #6b7280; }
.badge.folder { background: #0369a1; }
.card.override-keeper {
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .28);
}
.card.override-assumed-delete {
  opacity: .66;
  filter: grayscale(.25);
}
.card.override-assumed-delete .thumb-wrap {
  background: #374151;
}
.override-note {
  font-size: 12px;
  border: 1px solid #ddd6fe;
  background: #f5f3ff;
  color: #4c1d95;
  padding: 6px 8px;
  border-radius: 8px;
  margin: 8px 0;
}
.path {
  font-size: 12px;
  color: #374151;
  overflow-wrap: anywhere;
  max-height: 54px;
  overflow: auto;
  border: 1px solid #edf0f3;
  padding: 6px;
  border-radius: 8px;
  background: #fafafa;
}
.file-name {
  font-weight: 700;
  overflow-wrap: anywhere;
  margin-bottom: 4px;
}
.details {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 3px 8px;
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0;
}
.actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.actions button {
  font-size: 12px;
  padding: 6px 8px;
}
.toast {
  position: fixed;
  right: 16px;
  bottom: 16px;
  background: #111827;
  color: white;
  padding: 10px 12px;
  border-radius: 10px;
  display: none;
  z-index: 100;
  max-width: 720px;
}
.modal {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.80);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal img {
  max-width: 96vw;
  max-height: 92vh;
  object-fit: contain;
  background: #111;
}
.modal .close {
  position: absolute;
  top: 14px;
  right: 14px;
}
kbd {
  background: #111827;
  color: #fff;
  padding: 2px 5px;
  border-radius: 4px;
  font-size: 11px;
}
.help {
  font-size: 12px;
  color: var(--muted);
  margin-top: 8px;
}
@media (max-width: 920px) {
  main { grid-template-columns: 1fr; }
  aside { position: static; height: 260px; max-height: 260px; }
}
</style>
</head>
<body>
<header>
  <div class="topline">
    <button id="prevBtn" class="secondary">← Prev</button>
    <button id="nextBtn" class="secondary">Next →</button>
    <label>Group <input id="groupJump" type="number" min="1" style="width:90px"></label>
    <button id="jumpBtn" class="secondary">Go</button>
    <button id="confirmGroupBtn" class="good">Confirm DELETE rows</button>
    <button id="confirmAllBtn" class="warn">Confirm All Remaining Groups</button>
    <button id="clearGroupBtn" class="warn">Clear confirmations</button>
    <button id="saveBtn">Save reviewed CSV</button>
    <span class="status" id="status">Loading...</span>
  </div>
  <div class="help">
    Shortcuts: <kbd>N</kbd> next, <kbd>P</kbd> previous, <kbd>C</kbd> confirm group, <kbd>U</kbd> clear group, <kbd>S</kbd> save, <kbd>Esc</kbd> close zoom. The text filter searches every filename/path in each group. “Confirm All Remaining Groups” uses the current left-side filters.
  </div>
</header>
<main>
  <aside>
    <div class="sidebar-head">
      <input id="filterBox" type="text" placeholder="Filter group, folder, path, or any filename..." style="width:100%">
      <select id="strengthFilter" title="Filter by match strength / confidence" style="width:100%; margin-top:8px">
        <option value="ALL">All strengths</option>
        <option value="Review Carefully">Review Carefully</option>
        <option value="High">High</option>
        <option value="Very High">Very High</option>
        <option value="UNKNOWN">Unknown / blank</option>
      </select>
      <div class="meta" id="summaryBox" style="margin-top:8px"></div>
    </div>
    <div class="group-list" id="groupList"></div>
  </aside>
  <section class="content">
    <div class="group-title">
      <h2 id="groupTitle">Loading...</h2>
      <div class="meta" id="groupMeta"></div>
    </div>
    <div class="cards" id="cards"></div>
  </section>
</main>

<div class="modal" id="modal">
  <button class="close secondary" id="modalClose">Close</button>
  <img id="modalImg" src="">
</div>
<div class="toast" id="toast"></div>

<script>
let state = null;
let currentIndex = 0;
let currentGroup = null;
let dirty = false;
let filterText = "";
let strengthFilter = "ALL";

function qs(id) { return document.getElementById(id); }

async function api(path, options={}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let text = await res.text();
    throw new Error(text || res.statusText);
  }
  return await res.json();
}

function toast(msg) {
  const el = qs("toast");
  el.textContent = msg;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 3500);
}

function htmlEscape(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[c]));
}

function setDirty(value=true) {
  dirty = value;
  updateStatus();
}

function updateStatus() {
  if (!state) return;
  qs("status").textContent =
    `${dirty ? "Unsaved changes • " : ""}${state.totalConfirmedDeleteRows}/${state.totalDeleteRows} delete rows confirmed`;
}

async function loadState() {
  state = await api("/api/state");
  renderSidebar();
  renderSummary();
  updateStatus();
}

function renderSummary() {
  const visibleCount = getVisibleGroupIndexes().length;
  qs("summaryBox").innerHTML = `
    <b>${visibleCount}</b> / ${state.groupCount} groups shown<br>
    <b>${state.rowCount}</b> rows<br>
    <b>${state.totalConfirmedDeleteRows}</b> / ${state.totalDeleteRows} delete rows confirmed<br>
    Output: <span style="overflow-wrap:anywhere">${htmlEscape(state.outputCsv)}</span>
  `;
}

function normalizedStrength(value) {
  return String(value || "").trim().toLowerCase();
}

function groupPassesFilters(g) {
  const filter = filterText.trim().toLowerCase();
  const hay = `${g.groupId} ${g.keepFileName} ${g.keepFullPath} ${g.matchConfidence} ${g.searchableText || ""}`.toLowerCase();

  if (filter && !hay.includes(filter)) {
    return false;
  }

  if (strengthFilter && strengthFilter !== "ALL") {
    const confidence = normalizedStrength(g.matchConfidence);
    if (strengthFilter === "UNKNOWN") {
      if (confidence) return false;
    } else if (confidence !== normalizedStrength(strengthFilter)) {
      return false;
    }
  }

  return true;
}

function getVisibleGroupIndexes() {
  if (!state || !state.groups) return [];
  return state.groups.filter(groupPassesFilters).map(g => g.index);
}

function renderSidebar() {
  const list = qs("groupList");
  list.innerHTML = "";
  let activeItem = null;
  let shown = 0;

  state.groups.forEach(g => {
    if (!groupPassesFilters(g)) return;
    shown += 1;

    const div = document.createElement("div");
    div.className = "group-item" +
      (g.index === currentIndex ? " active" : "") +
      (g.isComplete ? " complete" : (g.hasAnyConfirmed ? " partial" : ""));
    div.dataset.groupIndex = String(g.index);
    div.onclick = () => loadGroup(g.index);

    if (g.index === currentIndex) {
      activeItem = div;
    }

    div.innerHTML = `
      <div class="gid">${htmlEscape(g.groupId)}</div>
      <div class="small">${g.rowCount} files • ${htmlEscape(g.matchConfidence || "No confidence")} • ${g.confirmedDeleteCount}/${g.deleteCount} confirmed${g.hasOverride ? " • OVERRIDE" : ""}</div>
      <div class="small">${htmlEscape(g.hasOverride ? g.overrideKeepFileName : (g.keepFileName || ""))}</div>
    `;
    list.appendChild(div);
  });

  if (shown === 0) {
    const empty = document.createElement("div");
    empty.className = "group-item";
    empty.innerHTML = `<div class="gid">No matching groups</div><div class="small">Change the text filter or strength filter.</div>`;
    list.appendChild(empty);
  }

  keepActiveSidebarItemInView(activeItem);
}

function keepActiveSidebarItemInView(activeItem) {
  if (!activeItem) return;

  const list = qs("groupList");
  if (!list) return;

  requestAnimationFrame(() => {
    const listRect = list.getBoundingClientRect();
    const itemRect = activeItem.getBoundingClientRect();

    const padding = 22;

    if (itemRect.top < listRect.top + padding) {
      list.scrollTop -= (listRect.top + padding) - itemRect.top;
    } else if (itemRect.bottom > listRect.bottom - padding) {
      list.scrollTop += itemRect.bottom - (listRect.bottom - padding);
    }
  });
}

async function loadGroup(index) {
  if (!state) await loadState();
  if (state.groupCount === 0) {
    qs("groupTitle").textContent = "No groups found";
    qs("groupMeta").textContent = "The CSV does not contain any duplicate groups.";
    qs("cards").innerHTML = "";
    return;
  }

  currentIndex = Math.max(0, Math.min(index, state.groupCount - 1));
  currentGroup = await api(`/api/group?index=${currentIndex}`);
  qs("groupJump").value = currentIndex + 1;

  renderGroup();
  renderSidebar();
}

function renderGroup() {
  const g = currentGroup;
  const s = g.summary;
  qs("groupTitle").textContent = `${g.groupId} — Group ${g.index + 1} of ${g.groupCount}`;
  const overrideMessage = s.hasOverride
    ? `<br><b style="color:#6d28d9">Override suggested:</b> keeping <span style="overflow-wrap:anywhere">${htmlEscape(s.overrideKeepFullPath || "")}</span><br><span class="meta">The prior Needs Categorized keeper is treated as DELETE but still requires confirmation.</span>`
    : "";
  qs("groupMeta").innerHTML = `
    ${s.rowCount} files • Keepers: <b>${s.keepCount}</b> • Confidence: <b>${htmlEscape(s.matchConfidence || "N/A")}</b> •
    Confirmed deletes: <b>${s.confirmedDeleteCount}</b> / ${s.deleteCount}<br>
    First keeper: <span style="overflow-wrap:anywhere">${htmlEscape(s.keepFullPath || "")}</span>${overrideMessage}
  `;

  const visibleIndexes = getVisibleGroupIndexes();
  const visiblePos = visibleIndexes.indexOf(currentIndex);
  qs("prevBtn").disabled = visibleIndexes.length === 0 || visiblePos <= 0;
  qs("nextBtn").disabled = visibleIndexes.length === 0 || visiblePos < 0 || visiblePos >= visibleIndexes.length - 1;

  const cards = qs("cards");
  cards.innerHTML = "";

  g.rows.forEach(row => {
    const action = row.suggestedAction;
    const confirmed = row.confirmDelete === "CONFIRM";
    const exists = !!row.exists;
    const overrideRole = row.autoOverrideRole || "";
    const card = document.createElement("div");
    card.className = `card ${action.toLowerCase()} ${confirmed ? "confirmed" : ""} ${overrideRole === "OVERRIDE_KEEP" ? "override-keeper" : ""} ${overrideRole === "OVERRIDE_ASSUMED_DELETE" ? "override-assumed-delete" : ""}`;

    const badges = [];
    if (action === "KEEP") badges.push(`<span class="badge keep">KEEP</span>`);
    if (action === "DELETE") badges.push(`<span class="badge delete">DELETE</span>`);
    if (overrideRole === "OVERRIDE_KEEP") badges.push(`<span class="badge override">OVERRIDE KEEP</span>`);
    if (overrideRole === "OVERRIDE_ASSUMED_DELETE") badges.push(`<span class="badge assumed">OVERRIDE ASSUMED DELETE</span>`);
    if (row.isNeedsCategorized) badges.push(`<span class="badge folder">Needs Categorized</span>`);
    if (confirmed) badges.push(`<span class="badge confirm">CONFIRMED</span>`);
    if (!exists) badges.push(`<span class="badge missing">MISSING</span>`);

    const mediaHtml = row.mediaType === "video"
      ? `<div class="thumb-wrap">
           <video controls preload="metadata" src="/media?row_id=${row.rowId}"></video>
         </div>`
      : `<div class="thumb-wrap">
           <img loading="lazy" src="/thumb?row_id=${row.rowId}" title="Click to zoom">
         </div>`;

    const detailHtml = row.mediaType === "video"
      ? `
          <div>Size</div><div>${htmlEscape(row.size)}</div>
          <div>Duration</div><div>${htmlEscape(row.duration)}</div>
          <div>Estimated bitrate</div><div>${htmlEscape(row.estimatedBitrateKbps)} kbps</div>
          <div>Dimensions</div><div>${htmlEscape(row.width)} × ${htmlEscape(row.height)}</div>
          <div>Similarity</div><div>${htmlEscape(row.similarityScore)}</div>
          <div>Avg frame hash Δ</div><div>${htmlEscape(row.averageFrameHashDistance)}</div>
          <div>Max frame hash Δ</div><div>${htmlEscape(row.maxFrameHashDistance)}</div>
          <div>Duration Δ</div><div>${htmlEscape(row.durationDeltaSeconds)}s</div>
          <div>Modified Δ</div><div>${htmlEscape(row.dateModifiedDeltaDays)} day(s)</div>
          <div>Modified close</div><div>${htmlEscape(row.dateModifiedClose)}</div>
          <div>Sidecar</div><div>${htmlEscape(row.hasSidecar || "NO")}</div>
        `
      : `
          <div>Size</div><div>${htmlEscape(row.size)}</div>
          <div>Dimensions</div><div>${htmlEscape(row.width)} × ${htmlEscape(row.height)}</div>
          <div>MP</div><div>${htmlEscape(row.megapixels)}</div>
          <div>dHash</div><div>${htmlEscape(row.dHashDistanceToKeep)}</div>
          <div>aHash</div><div>${htmlEscape(row.aHashDistanceToKeep)}</div>
          <div>Aspect Δ</div><div>${htmlEscape(row.aspectRatioDeltaToKeepPercent)}%</div>
          <div>Color Δ</div><div>${htmlEscape(row.colorDistanceToKeep)}</div>
        `;

    card.innerHTML = `
      ${mediaHtml}
      <div class="card-body">
        <div class="media-kind">${htmlEscape(row.mediaType)}</div>
        <div class="badges">${badges.join("")}</div>
        <div class="file-name">${htmlEscape(row.fileName)}</div>
        <div class="details">${detailHtml}</div>
        <div class="path">${htmlEscape(row.fullPath)}</div>
        ${row.sidecarPath ? `<div class="path"><b>Sidecar:</b> ${htmlEscape(row.sidecarPath)}</div>` : ""}
        ${row.notes ? `<div class="override-note">${htmlEscape(row.notes)}</div>` : ""}
        ${row.autoOverrideReason ? `<div class="override-note">${htmlEscape(row.autoOverrideReason)}</div>` : ""}
        <div class="actions">
          <button class="${action === "KEEP" ? "warn" : "secondary"}" data-act="togglekeep">${action === "KEEP" ? "Mark delete" : (overrideRole ? "Change / keep this" : "Keep this too")}</button>
          ${action === "DELETE"
            ? `<button class="${confirmed ? "secondary" : "danger"}" data-act="confirm">${confirmed ? "Unconfirm delete" : "Confirm delete"}</button>`
            : `<button class="secondary" data-act="confirm" disabled>${overrideRole === "OVERRIDE_KEEP" ? "Override keeper" : "Kept"}</button>`}
          <button class="secondary" data-act="openfolder">Open folder</button>
          <button class="secondary" data-act="openfile">Open file</button>
        </div>
      </div>
    `;

    const imgEl = card.querySelector("img");
    if (imgEl) {
      imgEl.onload = () => imgEl.classList.add("loaded");
      if (imgEl.complete) imgEl.classList.add("loaded");
      imgEl.onclick = () => showModal(`/preview?row_id=${row.rowId}`);
    }

    const videoEl = card.querySelector("video");
    if (videoEl) {
      videoEl.onerror = () => {
        const wrap = videoEl.parentElement;
        wrap.innerHTML = `<div class="video-unavailable">Browser preview unavailable for this format.<br>${htmlEscape(row.fileName)}<br><br>Use Open file.</div>`;
      };
    }

    card.querySelector('[data-act="togglekeep"]').onclick = () => toggleKeep(row);
    card.querySelector('[data-act="confirm"]').onclick = () => toggleConfirm(row);
    card.querySelector('[data-act="openfolder"]').onclick = () => openFolder(row);
    card.querySelector('[data-act="openfile"]').onclick = () => openFile(row);

    cards.appendChild(card);
  });

  prefetchUpcomingGroups(currentIndex + 1, 3);
}

async function prefetchUpcomingGroups(startIndex, count) {
  if (!state || state.groupCount === 0) return;

  for (let i = startIndex; i < Math.min(state.groupCount, startIndex + count); i++) {
    try {
      const g = await api(`/api/group?index=${i}`);
      for (const row of g.rows) {
        if (row.mediaType !== "image") continue;
        const img = new Image();
        img.src = `/thumb?row_id=${row.rowId}&prefetch=1`;
      }
    } catch (_err) {
      return;
    }
  }
}

function showModal(src) {
  qs("modalImg").src = src;
  qs("modal").style.display = "flex";
}

function closeModal() {
  qs("modal").style.display = "none";
  qs("modalImg").src = "";
}

async function refreshAfterChange(groupData) {
  currentGroup = groupData;
  await loadState();
  currentIndex = currentGroup.index;
  renderGroup();
  renderSidebar();
  renderSummary();
  setDirty(true);
}

async function toggleKeep(row) {
  if (row.suggestedAction === "KEEP") {
    const data = await api("/api/update_row", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({row_id: row.rowId, suggested_action: "DELETE", confirm_delete: ""})
    });
    toast("Marked row as DELETE. It was not confirmed for deletion.");
    await refreshAfterChange(data);
    return;
  }

  const data = await api("/api/set_keep", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({group_id: currentGroup.groupId, row_id: row.rowId})
  });
  toast("Added this row as another KEEP. Existing keepers were preserved.");
  await refreshAfterChange(data);
}

async function toggleConfirm(row) {
  if (row.suggestedAction === "KEEP") {
    toast("KEEP rows cannot be confirmed for deletion.");
    return;
  }
  const newConfirm = row.confirmDelete === "CONFIRM" ? "" : "CONFIRM";
  const data = await api("/api/update_row", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({row_id: row.rowId, suggested_action: "DELETE", confirm_delete: newConfirm})
  });
  await refreshAfterChange(data);
}

async function confirmCurrentGroup() {
  const data = await api("/api/confirm_group", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({group_id: currentGroup.groupId})
  });
  toast("Confirmed current DELETE rows. Any override suggestion is now applied through the confirmed KEEP/DELETE choices.");
  await refreshAfterChange(data);
}

async function confirmAllRemainingGroups() {
  if (!state || !state.groups) return;

  const visibleIndexes = getVisibleGroupIndexes();
  const visibleGroups = state.groups.filter(g => visibleIndexes.includes(g.index));
  const remaining = visibleGroups.filter(g => g.deleteCount > 0 && g.confirmedDeleteCount < g.deleteCount);
  const overrideRemaining = remaining.filter(g => g.hasOverride).length;

  if (remaining.length === 0) {
    toast("No remaining unconfirmed groups in the current filter.");
    return;
  }

  const filterDescription =
    (strengthFilter && strengthFilter !== "ALL" ? `Strength: ${strengthFilter}` : "All strengths") +
    (filterText.trim() ? `, Text filter: "${filterText.trim()}"` : "");

  const ok = window.confirm(
    `Confirm DELETE rows for ${remaining.length} remaining group(s) currently shown?\n\n` +
    `${filterDescription}\n` +
    `${overrideRemaining} group(s) include an override suggestion.\n\n` +
    `This does NOT delete files yet. It marks DELETE rows as CONFIRM in the reviewed CSV state. You still need to save the CSV and run the processor.`
  );

  if (!ok) return;

  const data = await api("/api/confirm_all_remaining", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({group_indexes: visibleIndexes})
  });

  toast(
    `Confirmed ${data.deleteRowsConfirmed} delete row(s) across ${data.groupsChanged} group(s). ` +
    `${data.overrideGroupsConfirmed} override group(s) included.`
  );

  await loadState();
  await loadGroup(currentIndex);
  setDirty(true);
}

async function clearCurrentGroup() {
  const data = await api("/api/clear_group", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({group_id: currentGroup.groupId})
  });
  toast("Cleared confirmations in this group.");
  await refreshAfterChange(data);
}

async function saveCsv() {
  const data = await api("/api/save", {method: "POST"});
  toast(`Saved reviewed CSV: ${data.outputCsv}`);
  await loadState();
  renderSummary();
  setDirty(false);
}

async function openFolder(row) {
  const data = await api(`/api/open_folder?row_id=${row.rowId}`);
  toast(data.message || "Open folder requested.");
}

async function openFile(row) {
  const data = await api(`/api/open_file?row_id=${row.rowId}`);
  toast(data.message || "Open file requested.");
}

function nextGroup() {
  if (!state || state.groupCount === 0) return;

  const visible = getVisibleGroupIndexes();
  if (visible.length === 0) return;

  const pos = visible.indexOf(currentIndex);
  if (pos >= 0 && pos < visible.length - 1) {
    loadGroup(visible[pos + 1]);
    return;
  }

  const nextVisible = visible.find(i => i > currentIndex);
  if (nextVisible !== undefined) {
    loadGroup(nextVisible);
  }
}

function prevGroup() {
  if (!state || state.groupCount === 0) return;

  const visible = getVisibleGroupIndexes();
  if (visible.length === 0) return;

  const pos = visible.indexOf(currentIndex);
  if (pos > 0) {
    loadGroup(visible[pos - 1]);
    return;
  }

  const previousVisible = [...visible].reverse().find(i => i < currentIndex);
  if (previousVisible !== undefined) {
    loadGroup(previousVisible);
  }
}

function applySidebarFilters() {
  renderSidebar();
  renderSummary();

  const visible = getVisibleGroupIndexes();
  if (visible.length > 0 && !visible.includes(currentIndex)) {
    loadGroup(visible[0]);
  }
}

qs("nextBtn").onclick = nextGroup;
qs("prevBtn").onclick = prevGroup;
qs("jumpBtn").onclick = () => {
  const value = parseInt(qs("groupJump").value || "1", 10);
  loadGroup(value - 1);
};
qs("confirmGroupBtn").onclick = confirmCurrentGroup;
qs("confirmAllBtn").onclick = confirmAllRemainingGroups;
qs("clearGroupBtn").onclick = clearCurrentGroup;
qs("saveBtn").onclick = saveCsv;
qs("modalClose").onclick = closeModal;
qs("modal").onclick = (ev) => { if (ev.target.id === "modal") closeModal(); };
qs("filterBox").oninput = (ev) => {
  filterText = ev.target.value || "";
  applySidebarFilters();
};

qs("strengthFilter").onchange = (ev) => {
  strengthFilter = ev.target.value || "ALL";
  applySidebarFilters();
};

document.addEventListener("keydown", (ev) => {
  const tag = (ev.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea") return;

  if (ev.key === "n" || ev.key === "N" || ev.key === "ArrowRight") nextGroup();
  if (ev.key === "p" || ev.key === "P" || ev.key === "ArrowLeft") prevGroup();
  if (ev.key === "c" || ev.key === "C") confirmCurrentGroup();
  if (ev.key === "u" || ev.key === "U") clearCurrentGroup();
  if (ev.key === "s" || ev.key === "S") saveCsv();
  if (ev.key === "Escape") closeModal();
});

window.addEventListener("beforeunload", (ev) => {
  if (dirty) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

(async function init() {
  try {
    await loadState();
    await loadGroup(0);
  } catch (err) {
    qs("status").textContent = "Error";
    qs("groupTitle").textContent = "Error loading reviewer";
    qs("groupMeta").textContent = err.message || String(err);
    console.error(err);
  }
})();
</script>
</body>
</html>
"""


class ReviewHandler(BaseHTTPRequestHandler):
    store: ReviewStore
    thumbnailer: Thumbnailer

    server_version = "SimilarFileReview/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except OSError as ex:
            if is_client_disconnect(ex):
                return
            raise

    def send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, indent=2).encode("utf-8")
        self.send_bytes(data, "application/json; charset=utf-8", status=status)

    def send_text(self, text: str, status: int = 200) -> None:
        self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def parse_query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def get_int_query(self, query: dict[str, list[str]], name: str, default: int = 0) -> int:
        try:
            return int((query.get(name) or [str(default)])[0])
        except Exception:
            return default

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        return json.loads(data.decode("utf-8"))

    def send_file_with_range(self, source: Path) -> None:
        if not source.exists() or not source.is_file():
            self.send_text("File not found", status=404)
            return

        file_size = source.stat().st_size
        content_type = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
        range_header = self.headers.get("Range", "")

        try:
            if range_header.startswith("bytes="):
                try:
                    range_spec = range_header.split("=", 1)[1]
                    start_text, end_text = (range_spec.split("-", 1) + [""])[:2]
                    start = int(start_text) if start_text else 0
                    end = int(end_text) if end_text else file_size - 1
                    start = max(0, min(start, file_size - 1))
                    end = max(start, min(end, file_size - 1))
                    length = end - start + 1

                    self.send_response(206)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()

                    with source.open("rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                except ValueError:
                    pass

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            with source.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except OSError as ex:
            if is_client_disconnect(ex):
                return
            raise

    def do_GET(self) -> None:
        try:
            path, query = self.parse_query()

            if path == "/":
                self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == "/api/state":
                self.send_json(self.store.state())
                return

            if path == "/api/group":
                index = self.get_int_query(query, "index", 0)
                self.send_json(self.store.group(index))
                return

            if path in {"/thumb", "/preview"}:
                row_id = self.get_int_query(query, "row_id", -1)
                if row_id < 0 or row_id >= self.store.row_count():
                    self.send_text("Invalid row_id", status=404)
                    return

                row = self.store.get_row(row_id)
                source = Path(row.get("FullPath", ""))
                if media_type_for_path(source) != "image":
                    placeholder = self.thumbnailer.placeholder(source, self.thumbnailer.thumb_size, "Video preview uses the video player.")
                    data = placeholder.read_bytes()
                    self.send_bytes(data, "image/jpeg")
                    return

                max_size = self.thumbnailer.thumb_size if path == "/thumb" else self.thumbnailer.preview_size
                image_path = self.thumbnailer.get_image(source, max_size)
                data = image_path.read_bytes()
                self.send_bytes(data, "image/jpeg")
                return

            if path == "/media":
                row_id = self.get_int_query(query, "row_id", -1)
                if row_id < 0 or row_id >= self.store.row_count():
                    self.send_text("Invalid row_id", status=404)
                    return

                row = self.store.get_row(row_id)
                source = Path(row.get("FullPath", ""))
                self.send_file_with_range(source)
                return

            if path in {"/api/open_folder", "/open_folder"}:
                row_id = self.get_int_query(query, "row_id", -1)
                self.send_json(self.open_folder(row_id))
                return

            if path in {"/api/open_file", "/open_file"}:
                row_id = self.get_int_query(query, "row_id", -1)
                self.send_json(self.open_file(row_id))
                return

            self.send_text("Not found", status=404)

        except Exception as ex:
            if is_client_disconnect(ex):
                return
            self.send_text(f"ERROR: {ex}", status=500)

    def do_POST(self) -> None:
        try:
            path, _query = self.parse_query()
            body = self.read_json_body()

            if path == "/api/update_row":
                row_id = int(body.get("row_id"))
                action = str(body.get("suggested_action", ""))
                confirm = body.get("confirm_delete")
                self.send_json(self.store.set_row_action(row_id, action, confirm))
                return

            if path == "/api/set_keep":
                group_id = str(body.get("group_id", ""))
                row_id = int(body.get("row_id"))
                self.send_json(self.store.set_keep(group_id, row_id))
                return

            if path == "/api/confirm_group":
                group_id = str(body.get("group_id", ""))
                self.send_json(self.store.confirm_group_deletes(group_id))
                return

            if path == "/api/confirm_all_remaining":
                raw_indexes = body.get("group_indexes")
                indexes = raw_indexes if isinstance(raw_indexes, list) else None
                self.send_json(self.store.confirm_remaining_group_deletes(indexes))
                return

            if path == "/api/clear_group":
                group_id = str(body.get("group_id", ""))
                self.send_json(self.store.clear_group_confirms(group_id))
                return

            if path == "/api/save":
                output_csv_text = str(body.get("output_csv", "") or "").strip()
                output_csv = Path(output_csv_text) if output_csv_text else None
                out = self.store.save(output_csv)
                self.send_json({"ok": True, "outputCsv": str(out)})
                return

            self.send_text("Not found", status=404)

        except Exception as ex:
            if is_client_disconnect(ex):
                return
            self.send_text(f"ERROR: {ex}", status=500)

    def open_folder(self, row_id: int) -> dict[str, Any]:
        if row_id < 0 or row_id >= self.store.row_count():
            return {"ok": False, "message": "Invalid row_id"}

        row = self.store.get_row(row_id)
        path = Path(row.get("FullPath", ""))

        if not path.exists():
            folder = path.parent if str(path.parent) else None
            if folder and folder.exists() and os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
                return {"ok": True, "message": f"Opened folder: {folder}"}
            return {"ok": False, "message": "File/folder not found"}

        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return {"ok": True, "message": f"Selected in Explorer: {path}"}

        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return {"ok": True, "message": f"Revealed in Finder: {path}"}

        subprocess.Popen(["xdg-open", str(path.parent)])
        return {"ok": True, "message": f"Opened folder: {path.parent}"}

    def open_file(self, row_id: int) -> dict[str, Any]:
        if row_id < 0 or row_id >= self.store.row_count():
            return {"ok": False, "message": "Invalid row_id"}

        row = self.store.get_row(row_id)
        path = Path(row.get("FullPath", ""))

        if not path.exists():
            return {"ok": False, "message": "File not found"}

        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

        return {"ok": True, "message": f"Opened file: {path}"}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local browser viewer for reviewing similar-image duplicate CSVs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-Csv", "--csv", required=True, help="Review CSV from Find-SimilarImages-ReviewDelete.py.")
    parser.add_argument("-OutputCsv", "--output-csv", default="", help="Reviewed CSV output path.")
    parser.add_argument("-ThumbCache", "--thumb-cache", default="", help="Thumbnail cache folder. Defaults beside the CSV.")
    parser.add_argument("-ThumbSize", "--thumb-size", type=int, default=384, help="Thumbnail max dimension.")
    parser.add_argument("-PreviewSize", "--preview-size", type=int, default=1600, help="Zoom preview max dimension.")
    parser.add_argument("-ThumbQuality", "--thumb-quality", type=int, default=78, help="JPEG quality for generated thumbnails/previews.")
    parser.add_argument("-MaxThumbCacheMB", "--max-thumb-cache-mb", type=int, default=512, help="Maximum thumbnail cache size in MB. Use 0 for unlimited.")
    parser.add_argument("-Host", "--host", default="127.0.0.1", help="Bind address. Keep 127.0.0.1 for local-only use.")
    parser.add_argument("-Port", "--port", type=int, default=8765, help="Local web server port.")
    parser.add_argument("-NoBrowser", "--no-browser", action="store_true", help="Do not automatically open browser.")
    parser.add_argument("-NoAutoVideoKeeperPreference", "--no-auto-video-keeper-preference", action="store_true", help="Do not auto-adjust video KEEP suggestions in the reviewer based on bitrate/file size preference.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    csv_path = Path(args.csv)
    output_csv = Path(args.output_csv) if args.output_csv else None
    thumb_cache = Path(args.thumb_cache) if args.thumb_cache else csv_path.parent / "thumbnail_cache"

    try:
        store = ReviewStore(
            csv_path,
            output_csv=output_csv,
            auto_video_keeper_preference=not bool(args.no_auto_video_keeper_preference),
        )
    except Exception as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 2

    thumbnailer = Thumbnailer(
        thumb_cache,
        thumb_size=max(128, int(args.thumb_size)),
        preview_size=max(512, int(args.preview_size)),
        thumb_quality=int(args.thumb_quality),
        max_cache_mb=int(args.max_thumb_cache_mb),
    )

    ReviewHandler.store = store
    ReviewHandler.thumbnailer = thumbnailer

    server = ThreadingHTTPServer((args.host, int(args.port)), ReviewHandler)
    url = f"http://{args.host}:{args.port}/"

    print("Similar File Visual Reviewer")
    print(f"CSV:             {csv_path}")
    print(f"Output CSV:      {store.output_csv}")
    print(f"Groups:          {store.group_count():,}")
    print(f"Rows:            {store.row_count():,}")
    print(f"Thumbnail cache: {thumb_cache}")
    print(f"Thumb size:      {max(128, int(args.thumb_size))} px")
    print(f"Preview size:    {max(512, int(args.preview_size))} px")
    print(f"Cache limit:     {'Unlimited' if int(args.max_thumb_cache_mb) <= 0 else str(int(args.max_thumb_cache_mb)) + ' MB'}")
    print(f"HEIC/HEIF:       {'Yes' if HEIF_SUPPORT else 'No'}")
    print(f"Video keeper pref:{'Yes' if not bool(args.no_auto_video_keeper_preference) else 'No'}")
    print(f"URL:             {url}")
    print("")
    print("This viewer does not delete, move, or rename files.")
    print("Press Ctrl+C here when finished.")
    print("")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping reviewer...")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
