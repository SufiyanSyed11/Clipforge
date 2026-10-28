# One-time setup on Windows (PowerShell). Needs Python 3.11-3.13, Node 20+, ffmpeg on PATH.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# PowerShell doesn't stop on a failing native command by itself; check each one.
function Invoke-Step([string]$What, [scriptblock]$Cmd) {
    Write-Host "==> $What"
    & $Cmd
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

foreach ($tool in "py", "node", "npm", "ffmpeg", "ffprobe") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found on PATH. See README.md (Run it on Windows) for prerequisites."
    }
}

# faster-whisper's engine ships Windows wheels for these versions; prefer the most tested.
$pyVersion = $null
# Windows PowerShell 5.1 turns a native command's stderr into an error under "Stop";
# a missing version is expected here, so probe with errors relaxed.
$ErrorActionPreference = "Continue"
foreach ($v in "3.12", "3.11", "3.13") {
    & py "-$v" -c "import sys" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $pyVersion = $v; break }
}
$ErrorActionPreference = "Stop"
if (-not $pyVersion) {
    throw "Python 3.11, 3.12 or 3.13 is required (py -0 lists what is installed). Install 3.12: winget install Python.Python.3.12"
}
Write-Host "Using Python $pyVersion"

if (-not (Test-Path .venv\Scripts\python.exe)) {
    Invoke-Step "Create virtual environment" { py "-$pyVersion" -m venv .venv }
}
Invoke-Step "Upgrade pip" { .\.venv\Scripts\python.exe -m pip install --upgrade pip }
Invoke-Step "Install backend" { .\.venv\Scripts\python.exe -m pip install -e "backend[dev,pipeline]" }
Push-Location web
try { Invoke-Step "Install web app" { npm install } } finally { Pop-Location }
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example (add your ANTHROPIC_API_KEY there)"
}
Invoke-Step "Check this PC" { .\.venv\Scripts\clipforge-check.exe }
Write-Host "Setup done. Start the app with: powershell -ExecutionPolicy Bypass -File scripts\dev.ps1"
