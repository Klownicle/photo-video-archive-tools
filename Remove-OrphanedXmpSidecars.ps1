<#
.SYNOPSIS
Finds orphaned media sidecar .xmp files and optionally removes them.

.DESCRIPTION
This script is designed for the archive sidecar convention used by the photo/video tools:

  media_file.ext.xmp

Example:

  2023-08-20_0000000001_IMG.jpg.xmp

The expected media file is the same path with only the final .xmp removed:

  2023-08-20_0000000001_IMG.jpg

By default, the script only processes XMP files whose pre-.xmp suffix is a known image/video extension.
That avoids accidentally deleting generic Adobe-style sidecars such as file.xmp unless -IncludeGenericXmp is used.

EXAMPLES
Preview only:
  .\Remove-OrphanedXmpSidecars.ps1 -Root "E:\DATA\Blake\Photo&Video" -WhatIf

Live cleanup to Recycle Bin:
  .\Remove-OrphanedXmpSidecars.ps1 -Root "E:\DATA\Blake\Photo&Video"

Permanent delete instead of Recycle Bin:
  .\Remove-OrphanedXmpSidecars.ps1 -Root "E:\DATA\Blake\Photo&Video" -PermanentDelete
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [string]$ReportPath,

    [switch]$PermanentDelete,

    [switch]$IncludeGenericXmp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$SupportedMediaExtensions = @(
    '.jpg', '.jpeg', '.jpe', '.png', '.heic', '.heif', '.gif', '.bmp', '.tif', '.tiff', '.webp',
    '.arw', '.cr2', '.cr3', '.nef', '.nrw', '.orf', '.raf', '.rw2', '.dng',
    '.mp4', '.m4v', '.mov', '.qt', '.mpg', '.mpeg', '.mpe', '.avi', '.wmv', '.asf',
    '.mkv', '.webm', '.3gp', '.3g2', '.mts', '.m2ts', '.ts', '.mod', '.tod'
)

function Get-ReadableSize {
    param([long]$Bytes)
    if ($Bytes -lt 1KB) { return "$Bytes B" }
    if ($Bytes -lt 1MB) { return ('{0:N2} KB' -f ($Bytes / 1KB)) }
    if ($Bytes -lt 1GB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    return ('{0:N2} GB' -f ($Bytes / 1GB))
}

function Send-FileToRecycleBin {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
        $LiteralPath,
        [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
        [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
    )
}

$rootItem = Get-Item -LiteralPath $Root
if (-not $rootItem.PSIsContainer) {
    throw "Root is not a folder: $Root"
}
$rootPath = $rootItem.FullName

if (-not $ReportPath) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $reportFolder = Join-Path -Path (Split-Path -Parent $PSCommandPath) -ChildPath 'duplicate_reports'
    New-Item -ItemType Directory -Path $reportFolder -Force | Out-Null
    $ReportPath = Join-Path -Path $reportFolder -ChildPath "${stamp}_orphaned_xmp_cleanup.csv"
}

Write-Host "Find Orphaned XMP Sidecars"
Write-Host "Root:             $rootPath"
Write-Host "Mode:             $(if ($WhatIfPreference) { 'WHATIF / dry run' } else { 'LIVE' })"
Write-Host "Delete method:    $(if ($PermanentDelete) { 'Permanent delete' } else { 'Recycle Bin' })"
Write-Host "Generic .xmp:     $(if ($IncludeGenericXmp) { 'Included' } else { 'Skipped unless media_file.ext.xmp' })"
Write-Host "Report:           $ReportPath"
Write-Host ""

$xmpFiles = Get-ChildItem -LiteralPath $rootPath -Filter '*.xmp' -File -Recurse -Force
$total = $xmpFiles.Count
$checked = 0
$skippedGeneric = 0
$orphans = New-Object System.Collections.Generic.List[object]

foreach ($xmp in $xmpFiles) {
    $checked++
    if ($total -gt 0 -and (($checked -eq 1) -or ($checked % 1000 -eq 0) -or ($checked -eq $total))) {
        $pct = [Math]::Round(($checked / [Math]::Max($total, 1)) * 100, 2)
        Write-Progress -Activity 'Scanning XMP sidecars' -Status "$checked / $total ($pct%)" -PercentComplete $pct
    }

    $expectedMediaPath = $xmp.FullName.Substring(0, $xmp.FullName.Length - 4)
    $expectedExt = [System.IO.Path]::GetExtension($expectedMediaPath).ToLowerInvariant()

    if (-not $IncludeGenericXmp -and $SupportedMediaExtensions -notcontains $expectedExt) {
        $skippedGeneric++
        continue
    }

    if (-not (Test-Path -LiteralPath $expectedMediaPath -PathType Leaf)) {
        $orphans.Add([pscustomobject]@{
            XmpPath = $xmp.FullName
            ExpectedMediaPath = $expectedMediaPath
            XmpSizeBytes = $xmp.Length
            XmpSize = Get-ReadableSize -Bytes $xmp.Length
            LastWriteTime = $xmp.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
            Status = 'PENDING'
            Error = ''
            ProcessedUtc = ''
        })
    }
}

Write-Progress -Activity 'Scanning XMP sidecars' -Completed

Write-Host "XMP files found:        $total"
Write-Host "Media-style checked:   $($total - $skippedGeneric)"
Write-Host "Generic skipped:       $skippedGeneric"
Write-Host "Orphaned XMP found:    $($orphans.Count)"
Write-Host ""

$deleted = 0
$errors = 0
$index = 0
foreach ($row in $orphans) {
    $index++
    if ($orphans.Count -gt 0 -and (($index -eq 1) -or ($index % 100 -eq 0) -or ($index -eq $orphans.Count))) {
        $pct = [Math]::Round(($index / [Math]::Max($orphans.Count, 1)) * 100, 2)
        Write-Progress -Activity 'Processing orphaned XMP sidecars' -Status "$index / $($orphans.Count) ($pct%)" -PercentComplete $pct
    }

    try {
        if ($PSCmdlet.ShouldProcess($row.XmpPath, 'Delete orphaned XMP sidecar')) {
            if ($PermanentDelete) {
                Remove-Item -LiteralPath $row.XmpPath -Force
            }
            else {
                Send-FileToRecycleBin -LiteralPath $row.XmpPath
            }
            $row.Status = if ($PermanentDelete) { 'PERMANENTLY_DELETED' } else { 'SENT_TO_RECYCLE_BIN' }
            $deleted++
        }
        else {
            $row.Status = if ($WhatIfPreference) { 'WHATIF' } else { 'SKIPPED' }
        }
    }
    catch {
        $row.Status = 'ERROR'
        $row.Error = $_.Exception.Message
        $errors++
    }
    $row.ProcessedUtc = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
}

Write-Progress -Activity 'Processing orphaned XMP sidecars' -Completed

$orphans | Export-Csv -LiteralPath $ReportPath -NoTypeInformation -Encoding UTF8

Write-Host "Done."
if ($WhatIfPreference) {
    Write-Host "Would delete:          $($orphans.Count)"
}
else {
    Write-Host "Deleted:               $deleted"
}
Write-Host "Errors:                $errors"
Write-Host "Report CSV:            $ReportPath"
