# Photo & Video Archive Tools

Windows-focused Python/PowerShell tools for cleaning up a large photo/video archive.

I made this because I had about 40k photos/videos to work through and did not find a public tool that felt modern enough for what I needed. I found older options like ImageHash-based scripts and VisiPics, but they were not the right fit for my archive. Basic ImageHash settings also missed a lot of what I needed to catch.

So I vibe-coded this with generative AI help and used it for my own cleanup.

Free to use. Fork it. Change it. Break it. Fix it with AI. I do not plan to support it.

Also, use this at your own choice and risk. I take no responsibility if it deletes the wrong file, corrupts metadata, breaks your workflow, or somehow sets your PC on fire.

![Video duplicate review example](assets/video-review-example.png)

## Help guide

[Open the help guide](https://klownicle.github.io/photo-video-archive-tools/)

GitHub shows HTML files as source code when opened from the normal repo file browser. The link above is the rendered GitHub Pages version.

Source files:

- [`docs/index.html`](docs/index.html)
- [`docs/Photo_Video_Archive_Tools_Help.html`](docs/Photo_Video_Archive_Tools_Help.html)

## What this does

The tools are meant to help with:

- renaming photos/videos into stable archive-safe filenames
- reviewing files with missing dates
- using folder context to deal with leftover undated files
- finding image/video duplicates and near-duplicates
- visually reviewing duplicate groups before deleting anything
- processing only explicitly confirmed delete rows
- creating/updating video XMP sidecars
- correcting image metadata and folder-based tags
- optionally writing Windows Explorer video tags where supported

This was built around a real cleanup workflow, not as a general-purpose commercial app.

## The de-dupe part

The de-dupe workflow is the main reason this project exists.

I was not just trying to find exact duplicate files. Exact hash matching is easy, but it misses a lot of real archive junk. My archive had renamed files, resized images, recompressed videos, cloud exports, phone exports, old camera folders, repeated filenames, and random copies spread across folders.

The workflow is intentionally split into three steps:

1. Find likely duplicate groups.
2. Review them visually in a local browser UI.
3. Delete only rows that were explicitly confirmed.

The delete processors only act on rows where both values are set:

```text
SuggestedAction = DELETE
ConfirmDelete = CONFIRM
```

So the scripts can suggest deletes, but they do not blindly delete them. You review, confirm, save the CSV, then process the confirmed rows.

## Image duplicate logic

The image duplicate finder compares image content, not just filenames or exact file hashes.

It can help catch things like:

- the same image renamed into different folders
- resized copies
- recompressed copies
- phone/cloud export duplicates
- visually identical or near-identical files with different sizes

The image workflow uses:

- SHA-256 for exact byte-for-byte duplicates
- perceptual dHash for visual similarity
- average hash / aHash as another visual signal
- aspect-ratio checks
- average color distance checks
- BK-tree indexing so searches are not just a giant slow compare-everything-to-everything loop
- SQLite caching so future runs do not re-hash unchanged images

The suggested image keeper generally favors:

1. higher resolution / pixel count
2. larger file size
3. preferred extension order
4. shorter path
5. alphabetical path as a final tie-breaker

It still needs review. That is the point of the browser reviewer.

## Video duplicate logic

Video de-dupe is harder than image de-dupe.

The same video can have a different file size, codec, bitrate, container, resolution, and modified date. A basic file hash is useless for that.

The video finder uses FFprobe and FFmpeg to compare the actual video content:

- FFprobe reads duration, width, height, and stream info
- FFmpeg samples frames from multiple points in the video
- each sampled frame gets a 64-bit perceptual hash
- frame hash distances are compared
- duration has to be close enough
- Date Modified is used only as a weak audit/helper signal
- estimated bitrate is used as a quality/preservation signal
- SQLite caching avoids re-sampling unchanged videos every run

This was useful for videos that were exported, compressed, re-encoded, resized, or renamed but were still basically the same clip.

## Video keeper logic

For videos, the keeper is not simply “highest resolution wins.”

That mattered because a tiny heavily compressed 1080p file can be worse than a larger lower-resolution copy. So the video logic tries to prefer preservation quality, not just dimensions.

The current video keeper preference is roughly:

1. if everything is in the same folder and the files are the same size, prefer the older Date Modified file
2. prefer an already archive-renamed video filename
3. prefer a file with a matching XMP sidecar
4. prefer higher estimated bitrate / larger effective file size
5. prefer higher resolution
6. use shorter path and alphabetical path as final tie-breakers

The reviewer can also override the suggested keeper without rerunning the expensive video analysis. For example, if the suggested keeper is still sitting in a temporary `Needs Categorized` folder, but an equal-or-better copy exists in a real album folder, the reviewer can suggest keeping the better placed file instead.

## Included files

- `Rename-PhotosVideos-ExifTool.py`
- `Correct-ImageVideoMetadataFromFilename.py`
- `Find-SimilarImages-ReviewDelete.py`
- `Find-SimilarVideos-ReviewDelete.py`
- `Review-SimilarFiles.py`
- `Install-FFmpeg-ForArchiveTool.ps1`
- `requirements.txt`
- `docs/Photo_Video_Archive_Tools_Help.html`

## Requirements

Install Python requirements:

```powershell
python -m pip install -r requirements.txt
```

### ExifTool

ExifTool is required for rename and metadata operations.

Get it from the official ExifTool site:

```text
https://exiftool.org/
```

Recommended Windows placement for these scripts:

```text
C:\Tools\ExifTool\exiftool.exe
```

Typical setup:

1. Download the Windows executable package from ExifTool.
2. Extract it.
3. Rename `exiftool(-k).exe` to `exiftool.exe` if needed.
4. Place it at `C:\Tools\ExifTool\exiftool.exe`.
5. Verify it:

```powershell
& "C:\Tools\ExifTool\exiftool.exe" -ver
```

If you put it somewhere else, pass the path with `-ExifTool`.

### FFmpeg / FFprobe

FFmpeg and FFprobe are required for video duplicate detection.

This repo includes a helper installer:

```powershell
.\Install-FFmpeg-ForArchiveTool.ps1 -Force
```

That should place FFmpeg locally under the tool folder:

```text
.\ffmpeg\bin\ffmpeg.exe
.\ffmpeg\bin\ffprobe.exe
```

The video duplicate finder can auto-detect that local folder.

Verify it:

```powershell
.\ffmpeg\bin\ffmpeg.exe -version
.\ffmpeg\bin\ffprobe.exe -version
```

## Basic workflow

1. Rename media into stable archive-safe names.
2. Review unresolved missing-date items.
3. Use CatchUp only after undated files are in meaningful folders.
4. Find duplicate images/videos.
5. Review duplicate groups in `Review-SimilarFiles.py`.
6. Process confirmed deletes with `-WhatIf` first.
7. Run the corrective metadata/tag/sidecar pass after the folder structure is stable.

## Safety rule

The duplicate processors only delete rows where:

```text
SuggestedAction = DELETE
ConfirmDelete = CONFIRM
```

Always run rename, delete, and metadata operations with `-WhatIf` first.

Also: do not run this against your only copy of anything important. Test on a copied folder first.

## Maintenance status

This repository is shared as-is.

I do not plan to actively maintain this project, provide support, or fix reported issues. If it helps you, great. If it does not, fork it and change it.

Issues and pull requests may not be reviewed.

## License

See `LICENSE`.

## No support

See `NO_SUPPORT.md`.
