# Create a Desktop shortcut to SATB Splitter.exe with custom icon
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "SATB Splitter.exe"
$ico = Join-Path $root "assets\satb_splitter.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "SATB Splitter.lnk"

if (-not (Test-Path $exe)) {
    & (Join-Path $PSScriptRoot "build_exe.ps1")
}

$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut($lnkPath)
$sc.TargetPath = $exe
$sc.WorkingDirectory = $root
$sc.WindowStyle = 1
if (Test-Path $exe) {
    $sc.IconLocation = "$exe,0"
} elseif (Test-Path $ico) {
    $sc.IconLocation = "$ico,0"
}
$sc.Description = "SATB Splitter Studio - Choral PDF & MusicXML to Soprano, Alto, Tenor, Bass"
$sc.Save()

Write-Host "Shortcut created: $lnkPath -> $exe"
