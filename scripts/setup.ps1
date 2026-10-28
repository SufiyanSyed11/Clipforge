# One-time setup on Windows (PowerShell). Needs Python 3.11+, Node 20+, ffmpeg on PATH.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e "backend[dev,pipeline]"
Push-Location web; npm install; Pop-Location
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\clipforge-check.exe
