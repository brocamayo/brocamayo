# One-click Windows setup for socialq.
# Right-click this file > "Run with PowerShell"
# (or: powershell -ExecutionPolicy Bypass -File setup-windows.ps1)
#
# Installs dependencies, creates your config files, and registers a
# background task that checks the queue every 10 minutes.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Find-Python {
    foreach ($cmd in @("py", "python")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found -and $found.Source -notlike "*WindowsApps*") { return $cmd }
    }
    return $null
}

Write-Host "`n== socialq setup ==`n" -ForegroundColor Cyan

$python = Find-Python
if (-not $python) {
    Write-Host "Python isn't installed. Installing it with winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Write-Host "`nPython installed. Close this window and run setup-windows.ps1 again." -ForegroundColor Green
    Read-Host "Press Enter to exit"
    exit 0
}

if (-not (Test-Path "$root\.venv")) {
    Write-Host "Creating virtual environment..."
    & $python -m venv "$root\.venv"
}
$venvPython = "$root\.venv\Scripts\python.exe"
$venvPythonW = "$root\.venv\Scripts\pythonw.exe"

Write-Host "Installing dependencies..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r "$root\requirements.txt"

Write-Host "Creating config files..."
& $venvPython -m socialq init $root

# Background task: every 10 minutes, hidden (pythonw = no console window pop-ups).
$taskName = "socialq"
$action = New-ScheduledTaskAction -Execute $venvPythonW -Argument "-m socialq run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Posts queued videos to YouTube, Instagram, TikTok and X" -Force | Out-Null
Write-Host "Registered background task '$taskName' (runs every 10 minutes)." -ForegroundColor Green

# Small helper so you can type `.\socialq list` instead of the full python path.
Set-Content -Path "$root\socialq.cmd" -Value "@pushd `"%~dp0`" & `".venv\Scripts\python.exe`" -m socialq %* & popd"

Write-Host "`n== Done ==" -ForegroundColor Cyan
Write-Host @"

Next steps (see README.md > Platform setup):
  1. Fill in the developer-app keys in .env   (opening it now)
  2. Check your accounts, timezone and posting slots in config.yaml
  3. See which logins are still needed (it prints the exact commands):
       .\socialq accounts
     e.g. .\socialq auth youtube --account main
  4. Queue a video:
       .\socialq add videos\my-video.mp4 --title "..." --caption "..." --tags a,b

To stop auto-posting: Unregister-ScheduledTask -TaskName socialq
"@
notepad "$root\.env"
Read-Host "`nPress Enter to close"
