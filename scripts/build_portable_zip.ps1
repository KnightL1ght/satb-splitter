# Build the all-in-one portable ("travelling software") Windows ZIP release
param(
    [string]$Version = "0.3.0",
    [string]$AudiverisDir = (Join-Path $env:USERPROFILE "Audiveris-local-dev3l"),
    [string]$TessdataDir = (Join-Path $env:APPDATA "AudiverisLtd\audiveris\config\tessdata"),
    [string]$AudiverisSourceRepo = (Join-Path $env:USERPROFILE "Audiveris\audiveris-dev")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$icoPath = Join-Path $root "assets\satb_splitter.ico"
$entryPy = Join-Path $root "scripts\pyinstaller_entry.py"

$stagingRoot = Join-Path $root "release_staging"
$bundleName = "SATB-Splitter-v$Version-Windows-Portable"
$stageDir = Join-Path $stagingRoot $bundleName
$pyiDist = Join-Path $root "build\pyinstaller_dist"
$pyiWork = Join-Path $root "build\pyinstaller_work"
$pyiSpec = Join-Path $root "build"

$portableZip = Join-Path $stagingRoot "$bundleName.zip"
$patchesZip = Join-Path $stagingRoot "audiveris-dev3l-source-patches.zip"
$patchTempDir = Join-Path $stagingRoot "_audiveris_patches_tmp"

if (-not (Test-Path $python)) {
    throw "Virtualenv python not found at $python"
}
if (-not (Test-Path (Join-Path $AudiverisDir "Audiveris.exe"))) {
    throw "Audiveris.exe not found in $AudiverisDir"
}
if (-not (Test-Path (Join-Path $AudiverisSourceRepo "LICENSE"))) {
    throw "Audiveris LICENSE not found in $AudiverisSourceRepo"
}

Write-Host "1. Refreshing icon ($icoPath)..."
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONPATH = Join-Path $root "src"
& $python -m satb_splitter.gui.icon
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $icoPath)) {
    throw "Icon generation failed."
}

Write-Host "2. Building PyInstaller --onedir bundle..."
if (Test-Path $pyiDist) { Remove-Item -Recurse -Force $pyiDist }
if (Test-Path $pyiWork) { Remove-Item -Recurse -Force $pyiWork }

& $python -m PyInstaller `
    --onedir `
    --windowed `
    --noconfirm `
    --clean `
    --name "SATB Splitter" `
    --icon "$icoPath" `
    --paths "$root\src" `
    --distpath "$pyiDist" `
    --workpath "$pyiWork" `
    --specpath "$pyiSpec" `
    --add-data "$root\templates;templates" `
    --add-data "$root\assets;assets" `
    --hidden-import PIL `
    --hidden-import PIL.ImageOps `
    --hidden-import PIL.ImageFilter `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtWebChannel `
    --exclude-module PySide6.Qt3DCore `
    --exclude-module PySide6.Qt3DRender `
    --exclude-module PySide6.QtMultimedia `
    --exclude-module PySide6.QtMultimediaWidgets `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtQuickWidgets `
    --exclude-module PySide6.QtDesigner `
    --exclude-module PySide6.QtSql `
    --exclude-module PySide6.QtCharts `
    --exclude-module PySide6.QtPdf `
    --exclude-module PySide6.QtPdfWidgets `
    --exclude-module PySide6.QtBluetooth `
    --exclude-module PySide6.QtNfc `
    --exclude-module PySide6.QtSensors `
    --exclude-module PySide6.QtPositioning `
    --exclude-module PySide6.QtWebSockets `
    --exclude-module PySide6.QtTest `
    --exclude-module matplotlib `
    --exclude-module scipy `
    --exclude-module IPython `
    --exclude-module tkinter `
    --exclude-module pytest `
    "$entryPy"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$pyiAppOut = Join-Path $pyiDist "SATB Splitter"
if (-not (Test-Path (Join-Path $pyiAppOut "SATB Splitter.exe"))) {
    throw "PyInstaller output missing SATB Splitter.exe at $pyiAppOut"
}

Write-Host "3. Staging portable directory ($stageDir)..."
New-Item -ItemType Directory -Force -Path $stagingRoot | Out-Null
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

Copy-Item -Recurse -Force "$pyiAppOut\*" $stageDir

$stageAudiveris = Join-Path $stageDir "Audiveris"
New-Item -ItemType Directory -Force -Path $stageAudiveris | Out-Null
Copy-Item -Recurse -Force "$AudiverisDir\*" $stageAudiveris

$stageTess = Join-Path $stageAudiveris "tessdata"
New-Item -ItemType Directory -Force -Path $stageTess | Out-Null
foreach ($lang in @("eng.traineddata", "ron.traineddata")) {
    $srcTess = Join-Path $TessdataDir $lang
    if (-not (Test-Path $srcTess)) {
        throw "Required tessdata file not found: $srcTess"
    }
    Copy-Item -Force $srcTess (Join-Path $stageTess $lang)
}

Copy-Item -Force (Join-Path $AudiverisSourceRepo "LICENSE") (Join-Path $stageAudiveris "LICENSE_AGPL3.txt")

$modificationsMd = @"
# Bundled Audiveris Modifications (`dev3l`)

This portable release bundles a modified build of **Audiveris** licensed under the
**GNU Affero General Public License v3.0 (AGPL-3.0)** (see ``LICENSE_AGPL3.txt``).

- **Upstream Base Branch**: ``origin/development`` (Audiveris 5.x development tree)
- **Bundled Build Commit**: ``6d65a4f06`` (``Cycle 7 dev3l``)
- **Corresponding Source**: Distributed alongside this release as ``audiveris-dev3l-source-patches.zip`` (exported via ``git format-patch origin/development..HEAD``).

## Summary of Changes Over Upstream
- **Cycle 7 (`dev3l`)**: fixed-point combo peak merge, notehead beam peak guard, and residual 16th-hook recovery.
- **Cycle 6 (`dev3k`)**: mixed void/black chord duration resolution and cross-staff ``StackRhythm`` sibling ``timeOffset`` recovery.
- **Caiet (`dev3j`)**: ``GridBuilder`` ``smallInterline`` resource key fix and ``PartwiseBuilder`` ``ChordName`` null guards.
- **Cycles 3–5B (`dev3f`–`dev3i`) & Prior Choir OMR Improvements**: Taguchi-tuned thin-beam and void/dot recovery, partial-system guards, OCR time-signature & multi-bar rest count detection, Romanian/lyrics/fermata separation, and system-break continuation measure alignment.
"@
Set-Content -Path (Join-Path $stageAudiveris "MODIFICATIONS.md") -Value $modificationsMd -Encoding UTF8

Copy-Item -Force (Join-Path $root "demo_score.musicxml") (Join-Path $stageDir "demo_score.musicxml")
Copy-Item -Force (Join-Path $root "LICENSE") (Join-Path $stageDir "LICENSE.txt")

$readmePortable = @"
SATB Splitter v$Version — Portable Windows Edition
==================================================

Quick Start
-----------
1. Extract the entire folder anywhere on your PC or USB drive.
2. Double-click "SATB Splitter.exe".
3. Drag and drop choral PDF scores or MusicXML (.musicxml / .mxl / .xml)
   files onto the window, choose an output folder, and click "Convert & Split".

What Is Included
----------------
- SATB Splitter.exe         Desktop Studio app (Light & Dark themes)
- _internal\                Self-contained Python + PySide6 + music21 + PyMuPDF runtime
- Audiveris\                Bundled Audiveris (dev3l) for offline PDF OMR
- Audiveris\tessdata\       English (eng) & Romanian (ron) Tesseract OCR data
- demo_score.musicxml       Sample 2-staff SATB score for quick testing
- LICENSE.txt               MIT License (SATB Splitter) + third-party notice

Optional MuseScore 4 Support
----------------------------
If MuseScore 4 is installed on your Windows PC, SATB Splitter will
automatically generate *_SATB.mscz files next to *_SATB.musicxml and
allow opening parts directly in MuseScore.

Licensing
---------
SATB Splitter is licensed under the MIT License (LICENSE.txt).
The bundled Audiveris OMR engine in Audiveris\ is licensed under the
GNU AGPL v3.0 (Audiveris\LICENSE_AGPL3.txt). Details of modifications
and the corresponding patch archive (audiveris-dev3l-source-patches.zip)
are documented in Audiveris\MODIFICATIONS.md.
"@
Set-Content -Path (Join-Path $stageDir "README_PORTABLE.txt") -Value $readmePortable -Encoding UTF8

Write-Host "4. Exporting Audiveris source patches (read-only from $AudiverisSourceRepo)..."
if (Test-Path $patchTempDir) { Remove-Item -Recurse -Force $patchTempDir }
New-Item -ItemType Directory -Force -Path $patchTempDir | Out-Null
& git -C "$AudiverisSourceRepo" format-patch "origin/development..HEAD" -o "$patchTempDir" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "git format-patch failed on $AudiverisSourceRepo"
}

if (Test-Path $patchesZip) { Remove-Item -Force $patchesZip }
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $patchTempDir,
    $patchesZip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)
Remove-Item -Recurse -Force $patchTempDir

Write-Host "5. Creating portable ZIP ($portableZip)..."
if (Test-Path $portableZip) { Remove-Item -Force $portableZip }
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stageDir,
    $portableZip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true
)

$portableSizeMB = [math]::Round((Get-Item $portableZip).Length / 1MB, 2)
$patchesSizeMB = [math]::Round((Get-Item $patchesZip).Length / 1MB, 2)
Write-Host "Done:"
Write-Host "  Portable ZIP: $portableZip ($portableSizeMB MB)"
Write-Host "  Patches ZIP:  $patchesZip ($patchesSizeMB MB)"
