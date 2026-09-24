# Instalation.MD — Windows installation guide

This guide installs **voiceyt** on another Windows PC.

## Requirements

- Windows 10/11
- Python 3.11 with `py -3.11`
- [mpv](https://github.com/shinchiro/mpv-winbuild-cmake/releases) on `PATH`
- A microphone
- Internet access
- Deno recommended for YouTube search

## 1. Get the project

```powershell
cd C:\Projects
git clone https://github.com/kaltyiontrish/Youtube_Voice_App.git
cd Youtube_Voice_App
```

Alternatively, copy the project folder to the new PC. If using a separate UI branch:

```powershell
git switch ui-modernization
```

## 2. Install prerequisites

```powershell
winget install --id Python.Python.3.11 -e
winget install mpv
winget install DenoLand.Deno
```

Close and reopen PowerShell, then verify:

```powershell
py -3.11 --version
mpv --version
where.exe deno
deno --version
```

## 3. Create the virtual environment

Run from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

## 4. Install dependencies

For CUDA 12:

```powershell
.\.venv\Scripts\python.exe -m pip install "onnxruntime-gpu[cuda,cudnn]<1.27"
```

Then install the project requirements:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

For CPU-only operation, set `asr.device: cpu` in `config.yaml`.

## 5. Configure voiceyt

The main configuration is `config.yaml`. Important settings:

```yaml
asr:
  backend: whisper
  models_dir: ./models
  device: cuda
player:
  mpv_path: mpv
  playlist_path: ./playlists.json
search:
  results: 10
```

List microphones:

```powershell
.\.venv\Scripts\python.exe -m voiceyt --list-devices
```

## 6. Download models

The daemon does not download models automatically.

Whisper and VAD:

```powershell
.\.venv\Scripts\python.exe -m voiceyt --download-models whisper vad
```

Expected paths:

```text
models/whisper/faster-whisper-large-v3-turbo/
models/vad/silero_vad.onnx
```

Other backends:

```powershell
.\.venv\Scripts\python.exe -m voiceyt --download-models parakeet vad
.\.venv\Scripts\python.exe -m voiceyt --download-models nemotron vad
.\.venv\Scripts\python.exe -m voiceyt --download-models vosk vad
.\.venv\Scripts\python.exe -m voiceyt --download-models all
```

## 7. Start the application

Double-click `voiceyt.bat`, or run:

```powershell
.\.venv\Scripts\python.exe -m voiceyt
```

Check the microphone without starting the daemon:

```powershell
.\.venv\Scripts\python.exe -m voiceyt --meter
```

## 8. Basic voice commands

```text
youtube play <artist or song>
youtube next
youtube previous
youtube stop
youtube pause
youtube continue
youtube add
youtube remove
youtube jump 3
```

`youtube play 3` and `youtube jump 3` select the third saved-playlist track. Numbers are 1-based.

## 9. Troubleshooting

### Virtual environment not found

```powershell
py -3.11 -m venv .venv
```

### Broken copied virtual environment

A `.venv` is not portable between PCs. Recreate it:

```powershell
Remove-Item -Recurse -Force .venv
py -3.11 -m venv .venv
```

Then reinstall dependencies and download the required model again.

### Model missing

```powershell
.\.venv\Scripts\python.exe -m voiceyt --download-models whisper vad
```

### Deno warning

```powershell
winget install DenoLand.Deno
.\.venv\Scripts\python.exe -m pip install -U "yt-dlp[default]"
```

Restart PowerShell and `voiceyt.bat` afterward.

### mpv not recognized

Install mpv or set the complete path in `config.yaml`:

```yaml
player:
  mpv_path: C:/Tools/mpv/mpv.exe
```

### UI does not open

Check that `behaviour.ui` is `true` and read the console traceback.

## 10. Transferring to another PC

Usually copy:

- `voiceyt\`
- `tests\`
- `config.yaml`
- `requirements.txt`
- `voiceyt.bat`
- `models\` (optional, avoids redownload)
- `playlists.json` (optional)
- `logs\ui_state.json` (optional)

Usually do not copy:

- `.venv\`
- `__pycache__\`
- `.pytest_cache\`
- temporary `.tmp` files

## 11. Updating

Stop voiceyt, then run from the project folder:

```powershell
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
