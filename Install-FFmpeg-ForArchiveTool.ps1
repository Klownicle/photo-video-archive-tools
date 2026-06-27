<#
Install-FFmpeg-ForArchiveTool.ps1

FFmpeg installer for the Photo & Video Archive Tool.

This downloads/extracts FFmpeg release essentials from gyan.dev, finds the extracted
bin folder containing ffmpeg.exe and ffprobe.exe, copies that bin folder into:

  C:\MediaArchiveTools\ffmpeg\bin

Then it verifies:

  .\ffmpeg\bin\ffmpeg.exe -version
  .\ffmpeg\bin\ffprobe.exe -version

The video duplicate script can auto-detect this local install.
#>

[CmdletBinding()]
param(
    [string]$ToolRoot = "C:\MediaArchiveTools",

    [string]$DownloadUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",

    [switch]$Force,

    [switch]$KeepZip
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Executable {
    param([string]$PathToExe)

    if (-not (Test-Path -LiteralPath $PathToExe)) {
        return $false
    }

    try {
        & $PathToExe -version | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Find-FirstFile {
    param(
        [string]$Root,
        [string]$Filter
    )

    if (-not (Test-Path -LiteralPath $Root)) {
        return $null
    }

    return Get-ChildItem -LiteralPath $Root -Filter $Filter -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

$ToolRootPath = [System.IO.Path]::GetFullPath($ToolRoot)
$InstallRoot = Join-Path $ToolRootPath "ffmpeg"
$InstallBin = Join-Path $InstallRoot "bin"
$ZipPath = Join-Path $ToolRootPath "ffmpeg-release-essentials.zip"
$TempExtract = Join-Path $ToolRootPath "_ffmpeg_extract_temp"

Write-Host "Install FFmpeg For Photo & Video Archive Tool - Fixed"
Write-Host "Tool root:    $ToolRootPath"
Write-Host "Install root: $InstallRoot"
Write-Host "Install bin:  $InstallBin"
Write-Host "Download URL: $DownloadUrl"

if (-not (Test-Path -LiteralPath $ToolRootPath)) {
    throw "ToolRoot does not exist: $ToolRootPath"
}

$ExistingFfmpeg = Find-FirstFile -Root $InstallRoot -Filter "ffmpeg.exe"
$ExistingFfprobe = Find-FirstFile -Root $InstallRoot -Filter "ffprobe.exe"

if ($ExistingFfmpeg -and $ExistingFfprobe -and -not $Force) {
    Write-Step "Existing FFmpeg install found"
    Write-Host "ffmpeg:  $($ExistingFfmpeg.FullName)"
    Write-Host "ffprobe: $($ExistingFfprobe.FullName)"
    Write-Host ""
    Write-Host "Verifying existing install..."
    if ((Test-Executable -PathToExe $ExistingFfmpeg.FullName) -and (Test-Executable -PathToExe $ExistingFfprobe.FullName)) {
        Write-Host "Existing install verified." -ForegroundColor Green
        exit 0
    }

    Write-Host "Existing files were found but did not verify. Rerun with -Force." -ForegroundColor Yellow
    exit 1
}

if ($Force) {
    Write-Step "Force requested; removing existing install/temp files"
    if (Test-Path -LiteralPath $InstallRoot) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $TempExtract) {
        Remove-Item -LiteralPath $TempExtract -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $ZipPath)) {
    Write-Step "Downloading FFmpeg"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath
}
else {
    Write-Step "Using existing downloaded ZIP"
    Write-Host $ZipPath
}

Write-Step "Extracting FFmpeg"
if (Test-Path -LiteralPath $TempExtract) {
    Remove-Item -LiteralPath $TempExtract -Recurse -Force
}
New-Item -ItemType Directory -Path $TempExtract | Out-Null

Expand-Archive -LiteralPath $ZipPath -DestinationPath $TempExtract -Force

$ExtractedFfmpeg = Find-FirstFile -Root $TempExtract -Filter "ffmpeg.exe"
$ExtractedFfprobe = Find-FirstFile -Root $TempExtract -Filter "ffprobe.exe"

if (-not $ExtractedFfmpeg) {
    throw "Could not find ffmpeg.exe after extraction under: $TempExtract"
}
if (-not $ExtractedFfprobe) {
    throw "Could not find ffprobe.exe after extraction under: $TempExtract"
}

$SourceBin = $ExtractedFfmpeg.Directory.FullName
if ((Split-Path -Leaf $SourceBin) -ine "bin") {
    # Fallback: use the directory containing ffmpeg.exe even if not named bin.
    Write-Host "WARNING: ffmpeg.exe was not inside a folder named bin. Using: $SourceBin" -ForegroundColor Yellow
}

Write-Host "Source bin: $SourceBin"

Write-Step "Installing bin folder"
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $InstallBin -Force | Out-Null

Copy-Item -Path (Join-Path $SourceBin "*") -Destination $InstallBin -Recurse -Force

$InstalledFfmpeg = Join-Path $InstallBin "ffmpeg.exe"
$InstalledFfprobe = Join-Path $InstallBin "ffprobe.exe"

if (-not (Test-Path -LiteralPath $InstalledFfmpeg)) {
    throw "Install failed: ffmpeg.exe was not copied to $InstalledFfmpeg"
}
if (-not (Test-Path -LiteralPath $InstalledFfprobe)) {
    throw "Install failed: ffprobe.exe was not copied to $InstalledFfprobe"
}

Write-Step "Verifying executables"
if (-not (Test-Executable -PathToExe $InstalledFfmpeg)) {
    throw "ffmpeg.exe exists but failed to run: $InstalledFfmpeg"
}
if (-not (Test-Executable -PathToExe $InstalledFfprobe)) {
    throw "ffprobe.exe exists but failed to run: $InstalledFfprobe"
}

$PathFile = Join-Path $ToolRootPath "ffmpeg_paths.ps1"
@"
`$FFmpegPath = "$InstalledFfmpeg"
`$FFprobePath = "$InstalledFfprobe"
"@ | Set-Content -LiteralPath $PathFile -Encoding UTF8

Write-Step "Cleaning up temp extraction"
if (Test-Path -LiteralPath $TempExtract) {
    Remove-Item -LiteralPath $TempExtract -Recurse -Force
}

if (-not $KeepZip -and (Test-Path -LiteralPath $ZipPath)) {
    Write-Step "Removing downloaded ZIP"
    Remove-Item -LiteralPath $ZipPath -Force
}

Write-Host ""
Write-Host "FFmpeg installed successfully." -ForegroundColor Green
Write-Host "ffmpeg:  $InstalledFfmpeg"
Write-Host "ffprobe: $InstalledFfprobe"
Write-Host "Path helper written: $PathFile"
Write-Host ""
Write-Host "Manual verification commands:"
Write-Host "  .\ffmpeg\bin\ffmpeg.exe -version"
Write-Host "  .\ffmpeg\bin\ffprobe.exe -version"
Write-Host ""
Write-Host "Video duplicate test command:"
Write-Host "  python .\Find-SimilarVideos-ReviewDelete.py -Root `"D:\MediaArchive\Photos and Videos`" -OutputFolder `".\duplicate_reports`" -Limit 10"
