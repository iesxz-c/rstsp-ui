# Universal RTSP IP Camera Client (PyQt + GStreamer)

Client-only RTSP viewer with low-latency tuning, protocol selection, monitoring logs, and auto-reconnect.

## Features

- RTSP URL input
- Connect / Disconnect controls
- Latency tuning (`rtspsrc latency`)
- Protocol selection: `AUTO` / `TCP` / `UDP`
- Auto-reconnect toggle + retry interval
- Embedded video rendering in PyQt
- Detailed monitoring logs:
  - Connect attempts
  - Pipeline state transitions (`NULL → READY → PAUSED → PLAYING`)
  - Warnings/errors
  - Reconnect attempts count

## Prerequisites (Windows)

1. Python 3.10+
2. GStreamer 1.22+ installed (Runtime + Plugins):
   - core runtime
   - good/bad/ugly plugins
   - libav plugin (`avdec_h264`)
3. Environment variables (example):
   - Add `C:\gstreamer\1.0\msvc_x86_64\bin` to `PATH`

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python app.py
```

or one-click setup + run:

```powershell
.\run.ps1
```

optional flags:

```powershell
.\run.ps1 -SkipInstall   # run without pip install
.\run.ps1 -NoVenv        # use system python directly
.\run.ps1 -NoRun         # setup only, do not launch UI
```

## Notes

- If `d3d11videosink` is unavailable, app falls back to `glimagesink`, then `autovideosink`.
- For strict low-latency behavior, prefer `TCP` when networks are unstable, `UDP` when jitter is low.

## Offline Portable Packaging (Air-Gapped)

This project supports a fully offline, no-install deployment model for secure lab environments.

### Build machine (one-time packaging)

Prerequisites on the build machine:

- Anaconda/Miniconda available (`conda` command works)
- Conda env (default: `rtsp39`) already tested with this app
- GStreamer runtime installed in one of:
   - `C:\Program Files\gstreamer\1.0\msvc_x86_64`
   - `C:\gstreamer\1.0\msvc_x86_64`

Create portable ZIP:

```powershell
.\build_portable.ps1
```

Custom env/output example:

```powershell
.\build_portable.ps1 -EnvName rtsp39 -BundleName RTSP_Project_Portable -OutputDir dist
```

Output:

- `dist\RTSP_Project_Portable.zip`

### Secure lab deployment (air-gapped)

1. Copy `RTSP_Project_Portable.zip` to target machine
2. Extract ZIP
3. Open extracted folder
4. Double-click `run.bat`

No internet, no pip install, no conda install, and no admin setup required on the target machine.
