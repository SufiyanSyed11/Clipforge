# Start API, worker and web app on Windows, each in its own window.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$venv = Join-Path $root ".venv\Scripts"
if (-not (Test-Path (Join-Path $venv "python.exe"))) { throw "Run scripts\setup.ps1 first" }

& (Join-Path $venv "clipforge-check.exe") | Out-Null
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv\clipforge-api.exe'" -WorkingDirectory $root
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$venv\clipforge-worker.exe'" -WorkingDirectory $root
Start-Process powershell -ArgumentList "-NoExit", "-Command", "npm run dev" -WorkingDirectory (Join-Path $root "web")
Write-Host "ClipForge: http://localhost:3000  (API http://127.0.0.1:8000). Close the three windows to stop."
