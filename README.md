# Trainer Hub

> 简体中文版见 [README_zh.md](README_zh.md) · English below

**Trainer Hub** is a portable Windows desktop tool that manages single-player game trainers in one place:

**register → search → one-click launch → auto-detect running games → download from the official site.**

Built with Python 3.10 + PySide6.

![image-20260817033614377](assets/image-20260817033614377.png)

## Features

| Feature | Description |
| --- | --- |
| Game library | Card wall (Steam covers), search, filter by source (Fling/本地/Local) |
| Import games | Auto-parses desktop `.url`/`.lnk` shortcuts; Steam games auto-fetch Chinese name + cover |
| Launch | One-click launch of games and trainers (admin elevation, UAC prompt is normal), combined game+trainer launch |
| Running detection | Cards highlight "running" while the game process is alive |
| Scan trainers | Scan any folder, auto-identify trainers by keywords (Fling/Trainer/修改器/…), add to library |
| Official download | Built-in FlingTrainer adapter: search → download → auto-extract → add to library (best-effort) |
| Defender whitelist | One-click add the trainers directory to Windows Defender exclusions |

## Quick Start

**Option A — Source (portable)**
1. Double-click `启动.bat` — first run creates a virtual env and installs dependencies automatically.
2. Trainer Hub scans Steam shortcuts in the desktop `game` folder on first launch.
3. Add trainers (manual / folder scan / official download), then launch.

**Option B — Packaged green build (no Python required)**
- `dist/TrainerHub/TrainerHub.exe` — copy the whole folder to any Windows PC and run. No installation, no registry writes.
- Data (library, covers, trainers) is created next to the exe and travels with the folder.

## Safety

- **First-run confirmation**: trainers downloaded from the official site require a one-click confirmation (with SHA-256 shown) before first launch; local trainers are unaffected.
- **Download whitelist**: only `flingtrainer.com` (HTTPS) is allowed; every download is hashed with SHA-256 for the record.
- **Zip-slip protection**: archive entries with `../`, absolute paths, etc. are rejected.
- **No telemetry, no auto-update**: no data collection; only local audit logs of download/launch events are written.
- Antivirus false positives are normal for memory-modification tools; whitelist the `trainers` folder and **only download trainers from the official source**.

## Disclaimer

- This tool is intended for **single-player games only**.
- It only registers, launches and downloads trainers from official sources; it contains **no cracking or injection code**.
- THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND — see the [LICENSE](LICENSE) for details.

## Development

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

## License

[MIT](LICENSE) © 2026 2asz