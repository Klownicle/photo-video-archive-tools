# Photo & Video Archive Tools

Windows-focused Python/PowerShell utilities for cleaning up large photo and video archives.

This project was created for a practical one-time archive cleanup and is shared publicly because it may be useful as a starting point for someone else.

![Video duplicate review example](assets/video-review-example.png)

## Maintenance status

This repository is shared **as-is**.

I do not plan to actively maintain this project, provide support, or fix issues reported by others. The tools were built for a specific cleanup workflow and may need changes for your archive, codecs, folder structure, or tooling versions.

Recommended use:

1. Fork the repository.
2. Test on a small copy of your files.
3. Adapt the scripts for your own archive.
4. Use your own debugging and tools, including generative AI, to modify it.

Issues and pull requests may not be reviewed.

## Included tools

- Rename tool: `Rename-PhotosVideos-ExifTool.py`
- Corrective metadata/tag/sidecar tool: `Correct-ImageVideoMetadataFromFilename.py`
- Video duplicate finder/processor: `Find-SimilarVideos-ReviewDelete.py`
- Image duplicate finder/processor: `Find-SimilarImages-ReviewDelete.py`
- Local browser review UI: `Review-SimilarFiles.py`
- FFmpeg local installer: `Install-FFmpeg-ForArchiveTool.ps1`
- Full help guide: `docs/Photo_Video_Archive_Tools_Help.html`

## What makes the de-dupe workflow different

The de-dupe process is the main reason this project exists. The goal was not just to find exact byte-for-byte duplicates. I needed something that could help review real-world duplicates created by phone exports, cloud downloads, resized images, recompressed videos, edited copies, repeated camera filenames, and files moved through multiple folder structures.

The de-dupe workflow is intentionally split into three parts:

1. **Find candidate duplicate groups.**
2. **Review the groups visually in a local browser UI.**
3. **Process only rows that were explicitly confirmed for deletion.**

That separation is important. The finder scripts make suggestions, but the delete processors only act when a row has both:

```text
SuggestedAction = DELETE
ConfirmDelete = CONFIRM
```

The reviewer is the safety layer. It lets you inspect the candidates, change the keeper, keep multiple files from the same group, clear confirmations, and save a reviewed CSV before anything is deleted.

### Image de-dupe logic

The image de-dupe script compares image content instead of only comparing filenames, folders, timestamps, or exact file hashes.

It is designed to catch cases such as:

- the same image renamed into different folders
- resized copies
- recompressed copies
- images exported from phones or cloud services
- visually identical or near-identical copies with different file sizes

The image workflow uses:

- **Exact SHA-256** for true byte-for-byte duplicates.
- **Perceptual dHash** to compare visual structure.
- **Average hash / aHash** as a second visual similarity signal.
- **Aspect-ratio comparison** to reduce false matches.
- **Average color distance** to reduce matches that have similar structure but different visual content.
- **BK-tree indexing** so hash-distance searches are much faster than comparing every image to every other image.
- **SQLite caching** so future runs do not need to re-hash unchanged images.

The script then groups related matches together and suggests a keeper. The image keeper preference generally favors:

1. higher resolution / pixel count
2. larger file size
3. preferred extension order
4. shorter path
5. alphabetical path as a final tie-breaker

The confidence labels are intentionally conservative:

- **Very High** means the visual hash distances are very close.
- **High** means likely duplicate, but still review.
- **Review Carefully** means the group needs human judgment.

### Video de-dupe logic

Video de-dupe is harder than image de-dupe because the same video may have different file sizes, codecs, bitrates, containers, resolutions, and modified dates.

The video finder does not rely on filenames or file hashes alone. It uses FFprobe and FFmpeg to compare the actual video content.

The video workflow uses:

- **FFprobe** to read duration, width, height, and basic stream information.
- **FFmpeg** to sample frames from multiple points in the video.
- **64-bit perceptual frame hashes** to compare sampled frames visually.
- **Duration tolerance** because two duplicate videos should usually be nearly the same length.
- **Frame-hash distance thresholds** to score visual similarity.
- **Date Modified delta** as a weak audit/helper signal, not as the deciding factor.
- **Estimated bitrate** as a preservation signal.
- **SQLite caching** so unchanged videos do not need to be re-sampled every run.

This makes the video de-dupe useful for cases where:

- a video was re-encoded
- a cloud service compressed or exported a different copy
- one copy has a different resolution
- one copy has a much smaller or larger file size
- the modified date is not trustworthy
- the filename changed but the visible content is the same

### Video keeper logic

For videos, the keeper logic is not simply “highest resolution wins.” That was intentional.

A tiny 1080p video can be worse than a larger 720p or 1440x1080 video if the 1080p copy is heavily compressed. So the video logic favors preservation quality signals before resolution.

The current video keeper preference is:

1. if all files in the group are in the same directory and have the same non-zero file size, prefer the older Date Modified file
2. otherwise prefer an already archive-renamed video filename
3. prefer a file that already has a matching XMP sidecar
4. prefer higher estimated bitrate / larger effective file size
5. prefer higher resolution
6. use shorter path and alphabetical path as final tie-breakers

The reviewer can also apply keeper overrides without rerunning expensive video analysis. For example, if a prior keeper is inside a temporary `Needs Categorized` folder and an equal-or-better duplicate exists outside that folder, the reviewer can suggest the outside file as the keeper instead.

### Why this was useful for my archive

My archive had tens of thousands of files, repeated camera names, partially sorted folders, a large `Needs Categorized` holding area, missing dates, old videos, sidecars, and many duplicate-looking exports. The value of this workflow was that it gave me a way to:

- normalize filenames first
- preserve folder context
- find likely duplicates by content
- visually review the results
- avoid one-click blind deletion
- keep multiple files when needed
- process only confirmed deletes
- clean up metadata and sidecars after the structure was stable

That combination is what made this more useful to me than a simple exact duplicate finder or a basic image-hash-only pass.

## Requirements

Install Python requirements:

```powershell
python -m pip install -r requirements.txt
```

### ExifTool

ExifTool is required for rename and corrective metadata operations.

Get ExifTool from the official ExifTool site:

```text
https://exiftool.org/
```

The official ExifTool site may point Windows downloads to SourceForge. Use the Windows executable package from the official download path.

Recommended Windows placement for these scripts:

```text
C:\Tools\ExifTool\exiftool.exe
```

Typical manual setup:

1. Download the Windows executable package from the official ExifTool site.
2. Extract it.
3. Rename `exiftool(-k).exe` to `exiftool.exe` if needed.
4. Place it here:

```text
C:\Tools\ExifTool\exiftool.exe
```

5. Verify it from PowerShell:

```powershell
& "C:\Tools\ExifTool\exiftool.exe" -ver
```

If you put ExifTool somewhere else, pass it explicitly:

```powershell
python .\Rename-PhotosVideos-ExifTool.py `
  -Root "D:\MediaArchive\Photos and Videos" `
  -ExifTool "C:\Path\To\exiftool.exe" `
  -WhatIf
```

### FFmpeg / FFprobe

FFmpeg and FFprobe are required for video duplicate detection.

This repository includes a helper installer:

```powershell
.\Install-FFmpeg-ForArchiveTool.ps1 -Force
```

That installer downloads and places FFmpeg locally under the tool folder, usually here:

```text
.\ffmpeg\bin\ffmpeg.exe
.\ffmpeg\bin\ffprobe.exe
```

The video duplicate finder can auto-detect that local `ffmpeg\bin` folder.

Verify after installation:

```powershell
.\ffmpeg\bin\ffmpeg.exe -version
.\ffmpeg\bin\ffprobe.exe -version
```

### Platform note

Windows is assumed for PowerShell examples, Recycle Bin delete behavior, and optional Windows Explorer tag support.


## Basic workflow

1. Rename media into stable archive-safe names.
2. Review unresolved missing-date items.
3. Use CatchUp only after undated files are in meaningful folders.
4. Find and review image/video duplicates.
5. Process confirmed deletes with `-WhatIf` first.
6. Run the corrective metadata/tag/sidecar pass after the folder structure is stable.

## Safety rule

The duplicate processors only delete rows where:

```text
SuggestedAction = DELETE
ConfirmDelete = CONFIRM
```

Always run rename, delete, and metadata operations with `-WhatIf` first.

## Viewing the HTML help guide

GitHub shows HTML files in the repository as source code when you click them in the normal `github.com` file browser. That is expected.

To view the help guide as an actual rendered web page, enable GitHub Pages for the repository and publish from the `/docs` folder.

Recommended setup:

1. Go to the repository on GitHub.
2. Open `Settings`.
3. Open `Pages`.
4. Under `Build and deployment`, choose `Deploy from a branch`.
5. Set the branch to `main`.
6. Set the folder to `/docs`.
7. Save.

After GitHub Pages finishes publishing, the rendered help guide should be available at a URL like:

```text
https://YOUR-GITHUB-USERNAME.github.io/photo-video-archive-tools/
```

This package includes:

```text
docs/index.html
docs/Photo_Video_Archive_Tools_Help.html
```

The `docs/index.html` file redirects the GitHub Pages root to the full help guide.

If you are only browsing the repository on `github.com`, clicking the HTML file will show code. Use the GitHub Pages URL for the rendered version.


## Full guide

Repository source file:

[`docs/Photo_Video_Archive_Tools_Help.html`](docs/Photo_Video_Archive_Tools_Help.html)

Rendered version after GitHub Pages is enabled:

```text
https://YOUR-GITHUB-USERNAME.github.io/photo-video-archive-tools/
```


## License

See `LICENSE`.

## No support

See `NO_SUPPORT.md`.
