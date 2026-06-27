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

## Requirements

Install Python requirements:

```powershell
python -m pip install -r requirements.txt
```

Additional tools:

- ExifTool for rename/metadata operations.
- FFmpeg/FFprobe for video duplicate detection.
- Windows is assumed for PowerShell examples, Recycle Bin deletes, and Windows Explorer tag support.

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

## Full guide

Open:

[`docs/Photo_Video_Archive_Tools_Help.html`](docs/Photo_Video_Archive_Tools_Help.html)

## License

See `LICENSE`.

## No support

See `NO_SUPPORT.md`.
