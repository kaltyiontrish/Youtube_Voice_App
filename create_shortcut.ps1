# One-time helper: creates a "YouTube Voice" shortcut on the Desktop.
# Run:  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
#   -NoConsole switch -> start the app without a console window (logs invisible)

param([switch]$NoConsole)

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')

if ($NoConsole) {
    $target  = Join-Path $root '.venv\Scripts\pythonw.exe'
    $args    = '-m voiceyt'
    $style   = 1
} else {
    $target  = "$env:ComSpec"
    $args    = "/c `"$root\voiceyt.bat`""
    $style   = 7   # minimized console
}

$shortcut = Join-Path $desktop 'YouTube Voice.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($shortcut)
$sc.TargetPath    = $target
$sc.Arguments     = $args
$sc.WorkingDirectory = $root
$sc.WindowStyle   = $style
$sc.Description   = 'Voice-controlled YouTube player (pt/en)'
$ico = Join-Path $root 'voiceyt.ico'
if (Test-Path $ico) { $sc.IconLocation = $ico }
$sc.Save()

Write-Host "Created: $shortcut"
Write-Host "Tip: to auto-start with Windows, copy the shortcut to:"
Write-Host "  $($env:APPDATA)\Microsoft\Windows\Start Menu\Programs\Startup"
