#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Correct-ImageVideoMetadataFromFilename.py

Corrective metadata script for a renamed photo/video archive.

What this does:
  1. Recursively scans a path for renamed files:
       YYYY-MM-DD_0000000001_IMG.ext
       YYYY-MM-DD_0000000001_VID.ext

  2. Images:
       - If Date Taken / DateTimeOriginal is missing, writes it from the filename date.
       - Adds the immediate parent folder name as a tag/keyword.
       - Optional -ResetExistingTags clears existing tag fields and resets
         them to the current parent-folder tag only.
       - Does NOT add the parent-folder tag if the immediate parent is:
           Needs Catagorized
           Needs Categorized
           Needs Catagory
           Needs Category

  3. Videos:
       - Creates an XMP sidecar beside the video.
       - Uses the filename date for date-taken/creation-date style XMP fields.
       - Uses the immediate parent folder name as the sidecar tag, unless parent
         is one of the Needs Catagorized / Categorized / Catagory / Category names.
       - Optional -ResetExistingTags rewrites existing sidecars so stale tags are removed.
       - Sidecar naming:
           video_file.ext.xmp

Always run -WhatIf first.

Recent additions:
  - -ResetExistingTags clears stale folder tags and resets to the current parent folder.
  - -SetWindowsTags writes Windows Explorer Tags / System.Keywords for videos where supported.
  - -VideosOnly and -ImagesOnly allow targeted corrective runs.
  - -SkipExifTool skips image ExifTool reads/writes so video-only sidecar/Windows-tag passes can run faster.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import xml.sax.saxutils as xml_escape
from pathlib import Path
from typing import Any


DEFAULT_EXIFTOOL = r"C:\Tools\ExifTool\exiftool.exe"
DEFAULT_ROOT = r"D:\MediaArchive\Photos and Videos"

RENAMED_FILE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<seq>\d{10})_(?P<kind>IMG|VID)\.(?P<ext>[^.]+)$",
    re.IGNORECASE,
)

SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg",
    ".tif", ".tiff",
    ".heic", ".heif",
    ".png",
}

EXIF_DATE_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg",
    ".tif", ".tiff",
    ".heic", ".heif",
}

XPKEYWORDS_EXTENSIONS = {
    ".jpg", ".jpeg",
    ".tif", ".tiff",
}

SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".qt",
    ".mpg", ".mpeg", ".mpe",
    ".avi", ".wmv", ".asf",
    ".mkv", ".webm",
    ".3gp", ".3g2",
    ".mts", ".m2ts", ".ts",
    ".mod", ".tod",
}

NEEDS_CATEGORIZED_NAMES = {
    "needs catagorized",
    "needs categorized",
    "needs catagory",
    "needs category",
}

DEFAULT_SKIP_DIR_NAMES = {
    "duplicate_reports",
    "thumbnail_cache",
    ".git",
    "__pycache__",
}

REPORT_FIELDS = [
    "FullPath",
    "ParentDirectory",
    "ParentFolderName",
    "FileName",
    "Extension",
    "Kind",
    "FilenameDate",
    "SupportedImage",
    "SupportedVideo",
    "SkippedNeedsCategorizedTag",
    "ExistingDateTimeOriginal",
    "ExistingTags",
    "NeedsDateTakenUpdate",
    "NeedsParentTagUpdate",
    "ResetExistingTags",
    "NeedsWindowsTagUpdate",
    "ExistingWindowsTags",
    "WindowsTagsAction",
    "WindowsTagsStatus",
    "WindowsTagsMessage",
    "NeedsVideoSidecar",
    "SidecarPath",
    "Action",
    "Status",
    "Message",
    "ExifToolOutput",
]


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def is_needs_categorized_name(name: str) -> bool:
    return normalize_name(name) in NEEDS_CATEGORIZED_NAMES


def parse_filename_date(file_name: str) -> tuple[dt.datetime | None, str, str]:
    match = RENAMED_FILE_RE.match(file_name)
    if not match:
        return None, "", ""

    try:
        parsed = dt.datetime.strptime(match.group("date"), "%Y-%m-%d")
    except ValueError:
        return None, match.group("kind").upper(), "." + match.group("ext").lower()

    return parsed, match.group("kind").upper(), "." + match.group("ext").lower()


def format_exif_datetime(value: dt.datetime) -> str:
    return value.strftime("%Y:%m:%d 00:00:00")


def format_xmp_datetime(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT00:00:00")


def format_photoshop_date(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d")


def normalize_tag(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def flatten_tag_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(flatten_tag_values(item))
        return output

    text = str(value).strip()
    if not text:
        return []

    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]

    return [text]


def get_existing_datetime_original(metadata: dict[str, Any]) -> str:
    for key, value in metadata.items():
        key_l = key.lower()
        if key_l.endswith(":datetimeoriginal") or key_l == "datetimeoriginal":
            text = str(value or "").strip()
            if text:
                return text
    return ""


def collect_existing_tags(metadata: dict[str, Any]) -> list[str]:
    tag_keys = {
        "subject",
        "keywords",
        "hierarchicalsubject",
        "xpkeywords",
    }

    values: list[str] = []
    for key, value in metadata.items():
        short_key = key.split(":")[-1].strip().lower()
        if short_key in tag_keys:
            values.extend(flatten_tag_values(value))

    seen = set()
    output: list[str] = []
    for item in values:
        norm = normalize_tag(item)
        if norm and norm not in seen:
            seen.add(norm)
            output.append(item)
    return output


def build_xpkeywords(existing_tags: list[str], new_tag: str) -> str:
    output: list[str] = []
    seen = set()

    for tag in existing_tags + [new_tag]:
        tag = (tag or "").strip()
        norm = normalize_tag(tag)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        output.append(tag)

    return ";".join(output)


def path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def sidecar_path_for_video(video_path: Path) -> Path:
    # Preferred Immich-style sidecar naming:
    #   video_file.ext.xmp
    return video_path.with_name(video_path.name + ".xmp")


def scan_files(root: Path, skip_dir_names: set[str], limit: int | None = None) -> list[Path]:
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
                        filename_date, kind, ext = parse_filename_date(path.name)
                        if not filename_date:
                            continue

                        if kind == "IMG" and ext in SUPPORTED_IMAGE_EXTENSIONS:
                            found.append(path)
                        elif kind == "VID" and ext in SUPPORTED_VIDEO_EXTENSIONS:
                            found.append(path)

                        if limit and len(found) >= limit:
                            return found

                    except OSError:
                        continue

                stack.extend(reversed(dirs))
        except OSError:
            continue

    return found


def run_exiftool_json(exiftool: Path, files: list[Path]) -> list[dict[str, Any]]:
    if not files:
        return []

    args = [
        str(exiftool),
        "-j",
        "-a",
        "-G1",
        "-s",
        "-q",
        "-q",
        "-DateTimeOriginal",
        "-Subject",
        "-Keywords",
        "-HierarchicalSubject",
        "-XPKeywords",
    ] + [str(p) for p in files]

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout = completed.stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def build_image_metadata_write_args(
    path: Path,
    filename_date: dt.datetime,
    date_needed: bool,
    tag_needed: bool,
    parent_tag: str,
    existing_tags: list[str],
    overwrite_original: bool,
    preserve_file_times: bool,
    reset_existing_tags: bool,
) -> list[str]:
    args: list[str] = []

    if overwrite_original:
        args.append("-overwrite_original")

    if preserve_file_times:
        args.append("-P")

    args.append("-m")

    ext = path.suffix.lower()

    if date_needed:
        exif_dt = format_exif_datetime(filename_date)
        xmp_dt = format_xmp_datetime(filename_date)

        if ext in EXIF_DATE_IMAGE_EXTENSIONS:
            args.append(f"-EXIF:DateTimeOriginal={exif_dt}")

        args.append(f"-XMP-exif:DateTimeOriginal={xmp_dt}")
        args.append(f"-XMP-xmp:CreateDate={xmp_dt}")

    if reset_existing_tags:
        # Clear stale folder/album tags first.
        # This fixes cases where the folder was renamed and the old parent tag remains.
        args.append("-XMP-dc:Subject=")
        args.append("-XMP-lr:HierarchicalSubject=")
        args.append("-IPTC:Keywords=")
        args.append("-XPKeywords=")

        # If parent_tag is blank or the folder is a Needs Category folder, this clears tags only.
        if parent_tag:
            args.append(f"-XMP-dc:Subject={parent_tag}")
            args.append(f"-XMP-lr:HierarchicalSubject={parent_tag}")

            if ext != ".png":
                args.append(f"-IPTC:Keywords={parent_tag}")

            if ext in XPKEYWORDS_EXTENSIONS:
                args.append(f"-XPKeywords={parent_tag}")

    elif tag_needed and parent_tag:
        args.append(f"-XMP-dc:Subject+={parent_tag}")
        args.append(f"-XMP-lr:HierarchicalSubject+={parent_tag}")

        if ext != ".png":
            args.append(f"-IPTC:Keywords+={parent_tag}")

        if ext in XPKEYWORDS_EXTENSIONS:
            xp_keywords = build_xpkeywords(existing_tags, parent_tag)
            args.append(f"-XPKeywords={xp_keywords}")

    return args


def run_exiftool_write(exiftool: Path, file_path: Path, write_args: list[str]) -> tuple[str, str, str]:
    args = [str(exiftool)] + write_args + [str(file_path)]

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    combined = ((completed.stdout or "") + " " + (completed.stderr or "")).strip()
    combined_single = re.sub(r"\s+", " ", combined)

    updated = any(
        phrase in combined
        for phrase in [
            "1 image files updated",
            "1 image files unchanged",
            "1 files updated",
            "1 file updated",
            "1 files unchanged",
            "1 file unchanged",
        ]
    )

    if completed.returncode == 0 or updated:
        return "UPDATED", "ExifTool update completed.", combined_single

    return "ERROR", f"ExifTool failed with exit code {completed.returncode}.", combined_single


def make_xmp_bag_xml(tag_name: str, values: list[str], indent: str = "      ") -> str:
    if not values:
        return ""

    lines = [
        f"{indent}<{tag_name}>",
        f"{indent}  <rdf:Bag>",
    ]
    for value in values:
        lines.append(f"{indent}    <rdf:li>{xml_escape.escape(value)}</rdf:li>")
    lines.extend([
        f"{indent}  </rdf:Bag>",
        f"{indent}</{tag_name}>",
    ])
    return "\n".join(lines)


def make_xmp_seq_xml(tag_name: str, values: list[str], indent: str = "      ") -> str:
    if not values:
        return ""

    lines = [
        f"{indent}<{tag_name}>",
        f"{indent}  <rdf:Seq>",
    ]
    for value in values:
        lines.append(f"{indent}    <rdf:li>{xml_escape.escape(value)}</rdf:li>")
    lines.extend([
        f"{indent}  </rdf:Seq>",
        f"{indent}</{tag_name}>",
    ])
    return "\n".join(lines)


def build_video_sidecar_xmp(
    video_path: Path,
    filename_date: dt.datetime,
    parent_tag: str,
    include_tag: bool,
    include_date: bool,
) -> str:
    xmp_dt = format_xmp_datetime(filename_date)
    exif_dt = format_exif_datetime(filename_date)
    photoshop_date = format_photoshop_date(filename_date)

    if include_date:
        date_attrs = (
            f'\n      xmp:CreateDate="{xmp_dt}"'
            f'\n      xmp:MetadataDate="{xmp_dt}"'
            f'\n      exif:DateTimeOriginal="{exif_dt}"'
            f'\n      photoshop:DateCreated="{photoshop_date}"'
            f'\n      xmpDM:shotDate="{xmp_dt}"'
        )
    else:
        date_attrs = ""

    tags = [parent_tag] if include_tag and parent_tag else []
    tag_blocks = [
        make_xmp_bag_xml("dc:subject", tags),
        make_xmp_bag_xml("lr:hierarchicalSubject", tags),
        make_xmp_seq_xml("digiKam:TagsList", tags),
    ]
    tag_xml = "\n".join(block for block in tag_blocks if block)
    if tag_xml:
        tag_xml = "\n" + tag_xml + "\n    "

    original_file = xml_escape.escape(video_path.name)

    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:exif="http://ns.adobe.com/exif/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
      xmlns:digiKam="http://www.digikam.org/ns/1.0/"
      xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
      xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"{date_attrs}>
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{original_file}</rdf:li>
        </rdf:Alt>
      </dc:title>{tag_xml}</rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_video_sidecar(
    video_path: Path,
    filename_date: dt.datetime,
    parent_tag: str,
    include_tag: bool,
    include_date: bool,
    update_existing: bool,
    whatif: bool,
) -> tuple[str, str, str]:
    sidecar_path = sidecar_path_for_video(video_path)
    exists = sidecar_path.exists()

    if exists and not update_existing:
        return "SKIPPED_EXISTING_SIDECAR", "Sidecar already exists. Use -UpdateExistingSidecars to rewrite it.", str(sidecar_path)

    if whatif:
        if exists and update_existing:
            return "WHATIF_UPDATE_SIDECAR", "Would update existing XMP sidecar.", str(sidecar_path)
        return "WHATIF_CREATE_SIDECAR", "Would create XMP sidecar.", str(sidecar_path)

    xmp = build_video_sidecar_xmp(
        video_path=video_path,
        filename_date=filename_date,
        parent_tag=parent_tag,
        include_tag=include_tag,
        include_date=include_date,
    )

    try:
        sidecar_path.write_text(xmp, encoding="utf-8", newline="\n")
        if exists:
            return "UPDATED_SIDECAR", "Updated existing XMP sidecar.", str(sidecar_path)
        return "CREATED_SIDECAR", "Created XMP sidecar.", str(sidecar_path)
    except OSError as exc:
        return "ERROR_SIDECAR", f"Failed to write XMP sidecar: {exc}", str(sidecar_path)


def flatten_windows_tag_values(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple)):
        output: list[str] = []
        for item in value:
            output.extend(flatten_windows_tag_values(item))
        return output

    text = str(value).strip()
    if not text:
        return []

    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]

    return [text]


def unique_tag_list(tags: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    for tag in tags:
        tag = str(tag or "").strip()
        norm = normalize_tag(tag)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        output.append(tag)

    return output


def windows_tag_lists_equal(a: list[str], b: list[str]) -> bool:
    return [normalize_tag(x) for x in unique_tag_list(a)] == [normalize_tag(x) for x in unique_tag_list(b)]


def get_windows_keywords(file_path: Path) -> tuple[list[str], str]:
    """
    Read Windows Explorer Tags / System.Keywords through the Windows Property System.

    Requires:
      python -m pip install pywin32
    """
    try:
        import pythoncom  # type: ignore
        from win32com.propsys import propsys, pscon  # type: ignore
        from win32com.shell import shellcon  # type: ignore
    except Exception as exc:
        return [], f"pywin32 unavailable: {exc}"

    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        store = propsys.SHGetPropertyStoreFromParsingName(
            str(file_path),
            None,
            shellcon.GPS_DEFAULT,
            propsys.IID_IPropertyStore,
        )
        value = store.GetValue(pscon.PKEY_Keywords).GetValue()
        return unique_tag_list(flatten_windows_tag_values(value)), "OK"
    except Exception as exc:
        return [], f"Failed to read Windows tags: {exc}"


def set_windows_keywords(
    file_path: Path,
    tags: list[str],
) -> tuple[str, str, list[str]]:
    """
    Write Windows Explorer Tags / System.Keywords through the Windows Property System.

    This depends on Windows having a writable property handler for the file type.
    If Explorer can manually write Tags for the file type, this usually can too,
    but some containers/codecs may still reject writes.
    """
    try:
        import pythoncom  # type: ignore
        from win32com.propsys import propsys, pscon  # type: ignore
        from win32com.shell import shellcon  # type: ignore
    except Exception as exc:
        return (
            "ERROR_WINDOWS_TAGS",
            "pywin32 is required for Windows tag writes. Install with: "
            "python -m pip install pywin32. "
            f"Original error: {exc}",
            [],
        )

    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    final_tags = unique_tag_list(tags)

    try:
        store = propsys.SHGetPropertyStoreFromParsingName(
            str(file_path),
            None,
            shellcon.GPS_READWRITE,
            propsys.IID_IPropertyStore,
        )

        prop_value = propsys.PROPVARIANTType(
            final_tags,
            pythoncom.VT_VECTOR | pythoncom.VT_LPWSTR,
        )
        store.SetValue(pscon.PKEY_Keywords, prop_value)
        store.Commit()

        if final_tags:
            return "UPDATED_WINDOWS_TAGS", "Updated Windows Explorer Tags / System.Keywords.", final_tags

        return "CLEARED_WINDOWS_TAGS", "Cleared Windows Explorer Tags / System.Keywords.", final_tags

    except Exception as exc:
        return "ERROR_WINDOWS_TAGS", f"Failed to write Windows tags: {exc}", final_tags



def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def write_report(report_path: Path, rows: list[dict[str, Any]]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_safe(row.get(field, "")) for field in REPORT_FIELDS})


def base_report_row(
    file_path: Path,
    filename_date: dt.datetime,
    kind: str,
    ext: str,
    supported_image: bool,
    supported_video: bool,
) -> dict[str, Any]:
    parent_name = file_path.parent.name
    return {
        "FullPath": str(file_path),
        "ParentDirectory": str(file_path.parent),
        "ParentFolderName": parent_name,
        "FileName": file_path.name,
        "Extension": ext,
        "Kind": kind,
        "FilenameDate": filename_date.strftime("%Y-%m-%d") if filename_date else "",
        "SupportedImage": "YES" if supported_image else "NO",
        "SupportedVideo": "YES" if supported_video else "NO",
        "SkippedNeedsCategorizedTag": "YES" if is_needs_categorized_name(parent_name) else "NO",
        "NeedsWindowsTagUpdate": "NO",
        "ExistingWindowsTags": "",
        "WindowsTagsAction": "",
        "WindowsTagsStatus": "",
        "WindowsTagsMessage": "",
    }


def process_image_file(
    args: argparse.Namespace,
    exiftool: Path,
    file_path: Path,
    filename_date: dt.datetime,
    kind: str,
    ext: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    parent_name = file_path.parent.name
    parent_tag = parent_name.strip()
    skip_parent_tag = is_needs_categorized_name(parent_name)

    existing_date = get_existing_datetime_original(metadata)
    existing_tags = collect_existing_tags(metadata)
    existing_tag_norms = {normalize_tag(t) for t in existing_tags}

    date_needed = (
        not args.no_date_taken
        and filename_date is not None
        and (args.overwrite_existing_date_taken or not existing_date)
    )

    reset_image_tags = bool(args.reset_existing_tags)
    tag_allowed = not args.no_parent_tag and bool(parent_tag) and not skip_parent_tag

    tag_needed = (
        tag_allowed
        and (
            reset_image_tags
            or normalize_tag(parent_tag) not in existing_tag_norms
        )
    )

    action_parts: list[str] = []
    if date_needed:
        action_parts.append("SET_IMAGE_DATE_TAKEN_FROM_FILENAME")
    if reset_image_tags:
        if tag_allowed:
            action_parts.append("RESET_IMAGE_TAGS_TO_PARENT_FOLDER")
        else:
            action_parts.append("CLEAR_IMAGE_TAGS")
    elif tag_needed:
        action_parts.append("ADD_IMAGE_PARENT_FOLDER_TAG")

    row = base_report_row(file_path, filename_date, kind, ext, True, False)
    row.update({
        "ExistingDateTimeOriginal": existing_date,
        "ExistingTags": "; ".join(existing_tags),
        "NeedsDateTakenUpdate": "YES" if date_needed else "NO",
        "NeedsParentTagUpdate": "YES" if tag_needed else "NO",
        "ResetExistingTags": "YES" if reset_image_tags else "NO",
        "NeedsVideoSidecar": "NO",
        "SidecarPath": "",
        "Action": "; ".join(action_parts) if action_parts else "NO_CHANGE",
    })

    if not date_needed and not tag_needed and not reset_image_tags:
        row.update({"Status": "SKIPPED", "Message": "No image corrective action needed.", "ExifToolOutput": ""})
        return row, "no_change"

    if args.whatif:
        row.update({"Status": "WHATIF", "Message": "Would apply image corrective metadata action(s).", "ExifToolOutput": ""})
        return row, "would_update"

    write_args = build_image_metadata_write_args(
        path=file_path,
        filename_date=filename_date,
        date_needed=date_needed,
        tag_needed=tag_needed,
        parent_tag=parent_tag,
        existing_tags=existing_tags,
        overwrite_original=not args.keep_exiftool_backups,
        preserve_file_times=not args.update_file_modified_time,
        reset_existing_tags=reset_image_tags,
    )

    status, message, output = run_exiftool_write(exiftool, file_path, write_args)
    row.update({"Status": status, "Message": message, "ExifToolOutput": output})

    return row, "updated" if status == "UPDATED" else "error"


def process_video_file(
    args: argparse.Namespace,
    file_path: Path,
    filename_date: dt.datetime,
    kind: str,
    ext: str,
) -> tuple[dict[str, Any], str]:
    parent_name = file_path.parent.name
    parent_tag = parent_name.strip()
    skip_parent_tag = is_needs_categorized_name(parent_name)
    sidecar_path = sidecar_path_for_video(file_path)

    include_tag = not args.no_parent_tag and bool(parent_tag) and not skip_parent_tag
    include_date = not args.no_date_taken
    reset_video_sidecar = bool(args.reset_existing_tags)
    should_have_sidecar = include_tag or include_date or reset_video_sidecar
    rewrite_existing_sidecar = args.update_existing_sidecars or reset_video_sidecar
    needs_sidecar = (
        not args.no_video_sidecars
        and should_have_sidecar
        and (rewrite_existing_sidecar or not sidecar_path.exists())
    )

    existing_windows_tags: list[str] = []
    windows_read_message = ""
    windows_tag_needed = False
    windows_tag_action = "NO_CHANGE"
    desired_windows_tags: list[str] = []

    if args.set_windows_tags:
        existing_windows_tags, windows_read_message = get_windows_keywords(file_path)

        if not args.no_parent_tag and include_tag:
            if args.append_windows_tags:
                desired_windows_tags = unique_tag_list(existing_windows_tags + [parent_tag])
                windows_tag_action = "APPEND_WINDOWS_PARENT_FOLDER_TAG"
            else:
                desired_windows_tags = unique_tag_list([parent_tag])
                windows_tag_action = "SET_WINDOWS_TAGS_TO_PARENT_FOLDER"

            windows_tag_needed = (
                bool(args.reset_existing_tags)
                or not windows_tag_lists_equal(existing_windows_tags, desired_windows_tags)
            )

        elif not args.no_parent_tag and args.reset_existing_tags and existing_windows_tags:
            # Needs Categorized folders intentionally do not get the parent-folder tag.
            desired_windows_tags = []
            windows_tag_action = "CLEAR_WINDOWS_TAGS"
            windows_tag_needed = True

    action_parts: list[str] = []
    if not args.no_video_sidecars and should_have_sidecar:
        if sidecar_path.exists() and reset_video_sidecar:
            action_parts.append("RESET_VIDEO_XMP_SIDECAR")
        elif sidecar_path.exists() and not rewrite_existing_sidecar:
            action_parts.append("VIDEO_SIDECAR_EXISTS")
        elif sidecar_path.exists() and rewrite_existing_sidecar:
            action_parts.append("UPDATE_VIDEO_XMP_SIDECAR")
        else:
            action_parts.append("CREATE_VIDEO_XMP_SIDECAR")
    if include_tag:
        action_parts.append("ADD_VIDEO_PARENT_FOLDER_TAG_TO_SIDECAR")
    if include_date:
        action_parts.append("ADD_VIDEO_DATE_FROM_FILENAME_TO_SIDECAR")
    if windows_tag_needed:
        action_parts.append(windows_tag_action)

    row = base_report_row(file_path, filename_date, kind, ext, False, True)
    row.update({
        "ExistingDateTimeOriginal": "",
        "ExistingTags": "",
        "NeedsDateTakenUpdate": "YES" if include_date else "NO",
        "NeedsParentTagUpdate": "YES" if include_tag else "NO",
        "ResetExistingTags": "YES" if reset_video_sidecar else "NO",
        "NeedsWindowsTagUpdate": "YES" if windows_tag_needed else "NO",
        "ExistingWindowsTags": "; ".join(existing_windows_tags),
        "WindowsTagsAction": windows_tag_action,
        "WindowsTagsStatus": "WHATIF" if args.whatif and windows_tag_needed else "",
        "WindowsTagsMessage": "" if windows_read_message == "OK" else windows_read_message,
        "NeedsVideoSidecar": "YES" if needs_sidecar else "NO",
        "SidecarPath": str(sidecar_path),
        "Action": "; ".join(action_parts) if action_parts else "NO_CHANGE",
        "ExifToolOutput": "",
    })

    if args.whatif:
        messages: list[str] = []
        if needs_sidecar:
            messages.append("Would create/update video XMP sidecar.")
        elif args.no_video_sidecars:
            messages.append("Video sidecar creation/update disabled by -NoVideoSidecars.")
        elif should_have_sidecar and sidecar_path.exists() and not rewrite_existing_sidecar:
            messages.append("Video sidecar already exists. Use -UpdateExistingSidecars or -ResetExistingTags to rewrite it.")

        if windows_tag_needed:
            messages.append("Would update Windows Explorer Tags / System.Keywords.")

        if needs_sidecar or windows_tag_needed:
            row.update({"Status": "WHATIF", "Message": " ".join(messages).strip()})
            return row, "would_update"

        row.update({"Status": "SKIPPED", "Message": "No video corrective action needed."})
        return row, "no_change"

    status_parts: list[str] = []
    message_parts: list[str] = []
    errors = False
    sidecar_written = False
    windows_written = False

    if args.no_video_sidecars:
        status_parts.append("SKIPPED_VIDEO_SIDECARS_DISABLED")
        message_parts.append("Video sidecar creation/update disabled by -NoVideoSidecars.")
    elif not should_have_sidecar:
        status_parts.append("SKIPPED_SIDECAR")
        message_parts.append("No video sidecar action needed because date and parent tag actions are disabled.")
    else:
        sidecar_status, sidecar_message, sidecar_text = write_video_sidecar(
            video_path=file_path,
            filename_date=filename_date,
            parent_tag=parent_tag,
            include_tag=include_tag,
            include_date=include_date,
            update_existing=rewrite_existing_sidecar,
            whatif=False,
        )
        row["SidecarPath"] = sidecar_text
        status_parts.append(sidecar_status)
        message_parts.append(sidecar_message)
        if sidecar_status in {"CREATED_SIDECAR", "UPDATED_SIDECAR"}:
            sidecar_written = True
        elif sidecar_status == "ERROR_SIDECAR":
            errors = True

    if windows_tag_needed:
        windows_status, windows_message, final_windows_tags = set_windows_keywords(
            file_path,
            desired_windows_tags,
        )
        row["WindowsTagsStatus"] = windows_status
        row["WindowsTagsMessage"] = windows_message
        if final_windows_tags:
            row["ExistingWindowsTags"] = "; ".join(final_windows_tags)
        status_parts.append(windows_status)
        message_parts.append(windows_message)

        if windows_status in {"UPDATED_WINDOWS_TAGS", "CLEARED_WINDOWS_TAGS"}:
            windows_written = True
        elif windows_status == "ERROR_WINDOWS_TAGS":
            errors = True

    if not status_parts:
        status_parts.append("SKIPPED")
        message_parts.append("No video corrective action needed.")

    row.update({
        "Status": "; ".join(status_parts),
        "Message": " ".join(message_parts).strip(),
    })

    if errors:
        return row, "error"
    if sidecar_written:
        return row, "sidecar_written"
    if windows_written:
        return row, "windows_tags_written"
    return row, "no_change"


def process(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    exiftool = Path(args.exiftool).expanduser()

    if not root.exists():
        print(f"ERROR: Root folder does not exist: {root}")
        return 2

    needs_exiftool = not bool(args.videos_only) and not bool(args.skip_exiftool)
    if needs_exiftool and not exiftool.exists():
        print(f"ERROR: ExifTool not found: {exiftool}")
        return 2

    output_folder = Path(args.output_folder).expanduser().resolve() if args.output_folder else root
    report_path = Path(args.report_csv).expanduser().resolve() if args.report_csv else output_folder / f"{now_stamp()}_corrective_image_video_metadata_report.csv"

    skip_dir_names = set(DEFAULT_SKIP_DIR_NAMES)
    for item in args.skip_dir_name or []:
        if item.strip():
            skip_dir_names.add(normalize_name(item))

    print("Correct Image/Video Metadata From Filename")
    print(f"Mode:                       {'WHATIF / dry run' if args.whatif else 'LIVE'}")
    print(f"Root:                       {root}")
    if args.skip_exiftool:
        exiftool_display = "Skipped by -SkipExifTool"
    elif needs_exiftool:
        exiftool_display = str(exiftool)
    else:
        exiftool_display = "Not used in -VideosOnly mode"
    print(f"ExifTool:                   {exiftool_display}")
    print(f"Report CSV:                 {report_path}")
    print(f"Batch size:                 {args.batch_size}")
    print(f"Overwrite image date:       {'Yes' if args.overwrite_existing_date_taken else 'No'}")
    print(f"Image Date Taken updates:   {'No' if args.no_date_taken else 'Yes'}")
    print(f"Parent folder tags:         {'No' if args.no_parent_tag else 'Yes'}")
    print(f"Target:                     {'Images only' if args.images_only else 'Videos only' if args.videos_only else 'Images and videos'}")
    print(f"Skip ExifTool:              {'Yes' if args.skip_exiftool else 'No'}")
    print(f"Reset existing tags:        {'Yes' if args.reset_existing_tags else 'No'}")
    print(f"Windows Explorer tags:      {'Yes' if args.set_windows_tags else 'No'}")
    print(f"Windows tag mode:           {'Append' if args.append_windows_tags else 'Replace'}")
    print(f"Video sidecars:             {'No' if args.no_video_sidecars else 'Yes'}")
    print(f"Update existing sidecars:   {'Yes' if args.update_existing_sidecars else 'No'}")
    print()

    print("Scanning for renamed image/video files...")
    limit = int(args.limit) if args.limit else None
    files = scan_files(root, skip_dir_names=skip_dir_names, limit=limit)

    if args.images_only:
        files = [p for p in files if parse_filename_date(p.name)[1] == "IMG"]
    elif args.videos_only:
        files = [p for p in files if parse_filename_date(p.name)[1] == "VID"]

    image_files: list[Path] = []
    video_files: list[Path] = []

    for file_path in files:
        filename_date, kind, ext = parse_filename_date(file_path.name)
        if kind == "IMG" and ext in SUPPORTED_IMAGE_EXTENSIONS:
            image_files.append(file_path)
        elif kind == "VID" and ext in SUPPORTED_VIDEO_EXTENSIONS:
            video_files.append(file_path)

    print(f"Renamed supported image files found: {len(image_files)}")
    print(f"Renamed supported video files found: {len(video_files)}")

    metadata_by_source: dict[str, dict[str, Any]] = {}

    if image_files and not args.skip_exiftool:
        print("Reading existing image metadata with ExifTool...")
        batch_size = max(1, int(args.batch_size))
        for start in range(0, len(image_files), batch_size):
            batch = image_files[start:start + batch_size]
            batch_data = run_exiftool_json(exiftool, batch)

            for item in batch_data:
                source = item.get("SourceFile", "")
                if source:
                    metadata_by_source[path_key(source)] = item

            print(f"  Read image metadata: {min(start + len(batch), len(image_files))}/{len(image_files)}")
    elif image_files and args.skip_exiftool:
        print("Skipping image ExifTool metadata read because -SkipExifTool was used.")
        print("  Image Date Taken and embedded image tag updates will be skipped.")

    report_rows: list[dict[str, Any]] = []

    stats = {
        "files_scanned": len(files),
        "image_files": len(image_files),
        "video_files": len(video_files),
        "would_update": 0,
        "image_updated": 0,
        "video_sidecar_written": 0,
        "windows_tags_written": 0,
        "no_change": 0,
        "errors": 0,
        "tag_skipped_needs": 0,
        "image_skipped_exiftool": 0,
    }

    print("Evaluating and applying corrective actions...")
    for index, file_path in enumerate(files, start=1):
        filename_date, kind, ext = parse_filename_date(file_path.name)

        if not filename_date:
            continue

        if is_needs_categorized_name(file_path.parent.name):
            stats["tag_skipped_needs"] += 1

        if kind == "IMG" and ext in SUPPORTED_IMAGE_EXTENSIONS:
            if args.skip_exiftool:
                row = base_report_row(file_path, filename_date, kind, ext, True, False)
                row.update({
                    "ExistingDateTimeOriginal": "",
                    "ExistingTags": "",
                    "NeedsDateTakenUpdate": "SKIPPED",
                    "NeedsParentTagUpdate": "SKIPPED",
                    "ResetExistingTags": "YES" if args.reset_existing_tags else "NO",
                    "NeedsVideoSidecar": "NO",
                    "SidecarPath": "",
                    "Action": "SKIPPED_IMAGE_EXIFTOOL_DISABLED",
                    "Status": "SKIPPED",
                    "Message": "Skipped image Date Taken and embedded image tag processing because -SkipExifTool was used.",
                    "ExifToolOutput": "",
                })
                outcome = "image_skipped_exiftool"
            else:
                row, outcome = process_image_file(
                    args=args,
                    exiftool=exiftool,
                    file_path=file_path,
                    filename_date=filename_date,
                    kind=kind,
                    ext=ext,
                    metadata=metadata_by_source.get(path_key(file_path), {}),
                )
        elif kind == "VID" and ext in SUPPORTED_VIDEO_EXTENSIONS:
            row, outcome = process_video_file(
                args=args,
                file_path=file_path,
                filename_date=filename_date,
                kind=kind,
                ext=ext,
            )
        else:
            continue

        report_rows.append(row)

        if outcome in {"would_update", "would_sidecar"}:
            stats["would_update"] += 1
        elif outcome == "updated":
            stats["image_updated"] += 1
        elif outcome == "sidecar_written":
            stats["video_sidecar_written"] += 1
        elif outcome == "windows_tags_written":
            stats["windows_tags_written"] += 1
        elif outcome == "no_change":
            stats["no_change"] += 1
        elif outcome == "image_skipped_exiftool":
            stats["image_skipped_exiftool"] += 1
        elif outcome == "error":
            stats["errors"] += 1

        if index % 250 == 0 or index == len(files):
            print(f"  Processed: {index}/{len(files)}")

    write_report(report_path, report_rows)

    print()
    print("Done.")
    print(f"Files scanned:                  {stats['files_scanned']}")
    print(f"Image files:                    {stats['image_files']}")
    print(f"Video files:                    {stats['video_files']}")
    print(f"Parent tags skipped - Needs:    {stats['tag_skipped_needs']}")
    if args.whatif:
        print(f"Files that would be updated:    {stats['would_update']}")
    else:
        print(f"Image files updated:            {stats['image_updated']}")
        print(f"Video sidecars written:         {stats['video_sidecar_written']}")
        print(f"Windows tags written:           {stats['windows_tags_written']}")
        print(f"Errors:                         {stats['errors']}")
    if args.skip_exiftool:
        print(f"Images skipped - ExifTool:      {stats['image_skipped_exiftool']}")
    print(f"No change needed:               {stats['no_change']}")
    print(f"Report written:                 {report_path}")

    return 1 if stats["errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Correct image metadata and create video sidecars from renamed filename dates and parent folder names.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("-Root", "--root", default=DEFAULT_ROOT, help="Root folder to scan recursively.")
    parser.add_argument("-ExifTool", "--exiftool", default=DEFAULT_EXIFTOOL, help="Path to exiftool.exe.")
    parser.add_argument("-SkipExifTool", "--skip-exiftool", action="store_true", help="Skip ExifTool entirely. Images are not read or written in this mode; video sidecars and Windows video tags can still be processed.")
    parser.add_argument("-OutputFolder", "--output-folder", default="", help="Folder for the report CSV. Defaults to root.")
    parser.add_argument("-ReportCsv", "--report-csv", default="", help="Explicit report CSV path.")
    parser.add_argument("-BatchSize", "--batch-size", type=int, default=100, help="Image metadata read batch size.")
    parser.add_argument("-Limit", "--limit", type=int, default=0, help="Limit number of matching files for testing. 0 means no limit.")
    parser.add_argument("-ImagesOnly", "--images-only", action="store_true", help="Process only renamed image files.")
    parser.add_argument("-VideosOnly", "--videos-only", action="store_true", help="Process only renamed video files. ExifTool is not required in this mode unless image processing is also enabled.")
    parser.add_argument("-WhatIf", "--whatif", action="store_true", help="Dry run. Do not write image metadata, video sidecars, or Windows Explorer tags.")

    parser.add_argument("-OverwriteExistingDateTaken", "--overwrite-existing-date-taken", action="store_true", help="Overwrite image DateTimeOriginal even when it already exists.")
    parser.add_argument("-NoDateTaken", "--no-date-taken", action="store_true", help="Do not update image Date Taken or video sidecar date fields.")
    parser.add_argument("-NoParentTag", "--no-parent-tag", action="store_true", help="Do not add the immediate parent folder name as a tag.")
    parser.add_argument("-ResetExistingTags", "--reset-existing-tags", action="store_true", help="Clear existing image tag fields and reset them to the current parent-folder tag. Also rewrites existing video sidecars with current tag/date values.")
    parser.add_argument("-SetWindowsTags", "--set-windows-tags", action="store_true", help="For videos, set Windows Explorer Tags / System.Keywords to the current parent-folder tag. Requires pywin32. Use -VideosOnly to target only videos.")
    parser.add_argument("-AppendWindowsTags", "--append-windows-tags", action="store_true", help="Append the parent-folder tag to existing Windows Explorer tags instead of replacing them. Used with -SetWindowsTags.")
    parser.add_argument("-NoVideoSidecars", "--no-video-sidecars", action="store_true", help="Do not create/update video XMP sidecars.")
    parser.add_argument("-UpdateExistingSidecars", "--update-existing-sidecars", action="store_true", help="Rewrite existing video .xmp sidecars.")
    parser.add_argument("-KeepExifToolBackups", "--keep-exiftool-backups", action="store_true", help="Allow ExifTool to create *_original backups for image writes.")
    parser.add_argument("-UpdateFileModifiedTime", "--update-file-modified-time", action="store_true", help="Do not preserve image filesystem modified time when writing embedded metadata.")
    parser.add_argument("-SkipDirName", "--skip-dir-name", action="append", default=[], help="Directory name to skip. Can be used multiple times.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.limit and args.limit < 0:
        args.limit = 0

    if args.images_only and args.videos_only:
        print("ERROR: Use either -ImagesOnly or -VideosOnly, not both.")
        return 2

    if args.images_only and args.skip_exiftool:
        print("ERROR: -ImagesOnly requires ExifTool. Do not use -SkipExifTool with -ImagesOnly.")
        return 2

    return process(args)


if __name__ == "__main__":
    raise SystemExit(main())
