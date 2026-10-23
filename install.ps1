# Installs live-reporting for Claude Code on Windows:
#   1. a private Python venv for the capture (speech-to-text + screen + window)
#   2. the speech model download (about 1.6 GB with an NVIDIA GPU, 150 MB without)
#   3. a link from %USERPROFILE%\.claude\skills\live-reporting to this folder
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$home_ = if ($env:LIVE_REPORTING_HOME) { $env:LIVE_REPORTING_HOME } else { Join-Path $env:LOCALAPPDATA 'live-reporting' }
$venv = Join-Path $home_ 'venv'

$pyExe = $null; $pyArgs = @()
foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $rest = @($c | Select-Object -Skip 1)
    $v = & $c[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($v -and [version]$v -ge [version]'3.10') { $pyExe = $c[0]; $pyArgs = $rest; break }
}
if (-not $pyExe) { throw 'Python 3.10 or newer is required (https://www.python.org/downloads/).' }

Write-Host "venv: $venv"
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { & $pyExe @pyArgs -m venv $venv }
$vpy = Join-Path $venv 'Scripts\python.exe'
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $repo 'requirements.txt')
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host 'NVIDIA GPU found: installing the CUDA libraries for fast transcription'
    & $vpy -m pip install --quiet -r (Join-Path $repo 'requirements-cuda.txt')
}

Write-Host 'Downloading the speech model (first time only)...'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('live-reporting-setup-' + [guid]::NewGuid())
& $vpy (Join-Path $repo 'scripts\capture.py') --session $tmp --download
Get-Content (Join-Path $tmp 'events.jsonl') | Select-Object -Last 1
Remove-Item -Recurse -Force $tmp

$skills = Join-Path $env:USERPROFILE '.claude\skills'
$link = Join-Path $skills 'live-reporting'
New-Item -ItemType Directory -Force $skills | Out-Null
if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.LinkType) { Write-Host "skill link already there: $link -> $($item.Target)" }
    else { Write-Warning "$link exists and is not a link; leaving it alone" }
} else {
    New-Item -ItemType Junction -Path $link -Target $repo | Out-Null  # a junction needs no admin rights
    Write-Host "skill linked: $link -> $repo"
}

& $vpy (Join-Path $repo 'scripts\live.py') doctor
Write-Host "`nDone. In Claude Code: /live-reporting <what you want to explain>"
