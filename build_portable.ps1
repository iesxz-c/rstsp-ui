param(
    [string]$EnvName = "rtsp39",
    [string]$BundleName = "RTSP_Project_Portable",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptRoot

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "conda is not available in PATH. Install Anaconda/Miniconda on the build machine."
}

$gstCandidates = @(
    "C:\gstreamer\1.0\msvc_x86_64",
    "C:\Program Files\gstreamer\1.0\msvc_x86_64"
)

$gstRoot = $null
foreach ($candidate in $gstCandidates) {
    if (Test-Path $candidate) {
        $gstRoot = $candidate
        break
    }
}

if (-not $gstRoot) {
    throw "GStreamer root not found. Expected one of: $($gstCandidates -join ', ')"
}

$condaPackOk = $true
conda run -n $EnvName conda-pack --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    $condaPackOk = $false
}

if (-not $condaPackOk) {
    Write-Host "Installing conda-pack into environment '$EnvName'..."
    conda run -n $EnvName python -m pip install conda-pack
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install conda-pack into environment '$EnvName'."
    }
}

$distDir = Join-Path $scriptRoot $OutputDir
$buildRoot = Join-Path $scriptRoot "_portable_build"
$portableRoot = Join-Path $buildRoot $BundleName
$envTar = Join-Path $buildRoot "rtsp_env.tar.gz"
$envDir = Join-Path $portableRoot "rtsp_env"
$gstTarget = Join-Path $portableRoot "gstreamer\1.0\msvc_x86_64"
$zipPath = Join-Path $distDir "$BundleName.zip"

if (Test-Path $buildRoot) {
    Remove-Item -Path $buildRoot -Recurse -Force
}

if (-not (Test-Path $distDir)) {
    New-Item -Path $distDir -ItemType Directory | Out-Null
}

New-Item -Path $portableRoot -ItemType Directory -Force | Out-Null
New-Item -Path $envDir -ItemType Directory -Force | Out-Null

Write-Host "Packing conda environment '$EnvName'..."
conda run -n $EnvName conda-pack -n $EnvName -o $envTar --force

if (-not (Test-Path $envTar)) {
    throw "conda-pack did not create archive: $envTar"
}

Write-Host "Extracting packed environment..."
tar -xf $envTar -C $envDir

if (-not (Test-Path (Join-Path $envDir "python.exe"))) {
    throw "Packed environment extraction failed. python.exe not found in $envDir"
}

Write-Host "Copying application files..."
Copy-Item -Path (Join-Path $scriptRoot "app.py") -Destination $portableRoot -Force
Copy-Item -Path (Join-Path $scriptRoot "run.bat") -Destination $portableRoot -Force

if (-not (Test-Path (Join-Path $portableRoot "run.bat"))) {
    throw "run.bat was not copied to portable bundle."
}

Write-Host "Copying local GStreamer runtime..."
New-Item -Path $gstTarget -ItemType Directory -Force | Out-Null
robocopy $gstRoot $gstTarget /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "robocopy failed while copying GStreamer runtime (exit code $LASTEXITCODE)."
}

Write-Host "Creating portable ZIP..."
if (Test-Path $zipPath) {
    Remove-Item -Path $zipPath -Force
}
Compress-Archive -Path (Join-Path $portableRoot "*") -DestinationPath $zipPath -Force

Write-Host "Portable package created: $zipPath"
Write-Host "Deployment steps: Extract ZIP, open folder, double-click run.bat"
