param(
    [switch]$SkipInstall,
    [switch]$NoVenv,
    [switch]$NoRun
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptRoot

Write-Host "Workspace: $scriptRoot"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not available in PATH. Install Python 3.10+ and retry."
}

$pythonCmd = "python"

$gstRootCandidates = @(
    "C:\gstreamer\1.0\msvc_x86_64",
    "C:\Program Files\gstreamer\1.0\msvc_x86_64"
)

$gstRoot = $null
foreach ($candidate in $gstRootCandidates) {
    if (Test-Path $candidate) {
        $gstRoot = $candidate
        break
    }
}

if ($gstRoot) {
    $gstBin = Join-Path $gstRoot "bin"
    $gstSitePackages = Join-Path $gstRoot "lib\site-packages"

    if (Test-Path $gstBin) {
        if (-not ($env:PATH -split ';' | Where-Object { $_ -eq $gstBin })) {
            $env:PATH = "$gstBin;$env:PATH"
        }
    }

    if (Test-Path $gstSitePackages) {
        if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
            $env:PYTHONPATH = $gstSitePackages
        }
        elseif (-not ($env:PYTHONPATH -split ';' | Where-Object { $_ -eq $gstSitePackages })) {
            $env:PYTHONPATH = "$gstSitePackages;$env:PYTHONPATH"
        }
    }
}

if (-not $NoVenv) {
    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        Write-Host "Creating virtual environment (.venv)..."
        python -m venv .venv
    }
    $pythonCmd = ".\.venv\Scripts\python.exe"
}

if (-not $SkipInstall) {
    Write-Host "Installing Python dependencies..."
    & $pythonCmd -m pip install --upgrade pip
    & $pythonCmd -m pip install -r requirements.txt
}

if (-not $NoRun) {
    Write-Host "Starting RTSP client..."
    & $pythonCmd app.py
}
