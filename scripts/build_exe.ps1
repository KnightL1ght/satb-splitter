# Build the multi-resolution icon and compile the standalone Windows launcher (SATB Splitter.exe)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$icoPath = Join-Path $root "assets\satb_splitter.ico"
$csPath = Join-Path $root "scripts\SatbSplitterLauncher.cs"
$exePath = Join-Path $root "SATB Splitter.exe"
$distDir = Join-Path $root "dist"
$distExePath = Join-Path $distDir "SATB Splitter.exe"

Write-Host "1. Generating multi-resolution icon ($icoPath)..."
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONPATH = Join-Path $root "src"
& $python -m satb_splitter.gui.icon
if (-not (Test-Path $icoPath)) {
    throw "Icon generation failed: $icoPath not found."
}

$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = "C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) {
    throw "Could not find .NET Framework csc.exe compiler."
}

Write-Host "2. Compiling native Windows executable ($exePath)..."
& $csc /nologo /target:winexe /optimize+ "/win32icon:$icoPath" "/out:$exePath" "$csPath"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exePath)) {
    throw "csc.exe failed to build $exePath"
}

New-Item -ItemType Directory -Force -Path $distDir | Out-Null
Copy-Item -Force $exePath $distExePath

Write-Host "Built executable: $exePath ($((Get-Item $exePath).Length) bytes)"
Write-Host "Copied to dist:   $distExePath"
