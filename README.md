# Tekken Revolution Online

> Installer that revives **Tekken Revolution (NPUB31250)** online play
> on RPCS3 — no external DLLs needed.

GUI installer, 100% open source, written in Python with tkinter. Zero
additional dependencies. Every byte it touches is visible in this repo.

## What the installer does

| Step | Action | Reversible |
|---|---|---|
| 1 | Installs PS3 firmware (PS3UPDAT.PUP) via RPCS3 CLI | (managed by RPCS3) |
| 2 | Patches `rpcs3.exe` / `rpcs3` (Linux) — 1 byte in `.text` that raises the internal P2PS retry limit from 10 to 40 | Backup `.bak_pre_p2ps` auto-created |
| 3 | Downloads the official `.pkg` from Sony CDN and installs it (only if the game is not already present) | (managed by RPCS3) |
| 4 | Downloads and installs game updates 1.01-1.05 from Sony CDN | (managed by RPCS3) |
| 5 | Installs the `.rap` license (bundled or user-supplied) into `dev_hdd0/home/00000001/exdata/` | Delete the `.rap` to revert |
| 6 | Creates the trophy mirror `NPWR04645_01` from `_00` (avoids the bogus "343 GB" error from Sony 1.04) | Delete the folder to revert |
| 7 | Replaces `EBOOT.BIN` with the pre-patched version (online fixes baked-in) | Backup `.original` auto-created |
| 8 | Creates `config/custom_configs/config_NPUB31250.yml` with recommended settings (60 FPS, Vulkan, etc.) — **per-game only, does NOT touch your global config** | Backup `.bak` if one already existed |
| 9 | RPCN account setup: register or enter existing credentials (NPID + password + token) | Edit rpcn.yml manually to revert |

## What the installer does NOT touch

- Your `config/config.yml` (global preferences)
- Your `config/rpcn.yml` (only written if you use step 9)
- Configs for other games in `custom_configs/`
- Input, audio, or other peripheral configs
- Saves and trophies for other games

## Requirements

- **Python 3.8+** (comes with Tkinter on Windows; on Linux:
  `apt install python3-tk` if needed)
- **RPCS3 v0.0.40+** already installed
- **Internet connection** for downloading the `.pkg` and updates

## How to use

```
python tekkennb.py
```

GUI:
1. Click **"Browse..."** and select your `rpcs3.exe` (or `rpcs3` on Linux)
2. Each step has its own button and status — run them in order
3. The **Refresh** button on step 2 checks if your RPCS3 is already patched
4. **Preview** on step 8 shows what config will be written before doing it

## Building a standalone executable

To distribute without requiring Python on the user's machine:

### Windows

```cmd
pip install pyinstaller
build.bat
```

Or: `python build.py`

Output: **`dist\TekkenRevOnline\`** (folder with the .exe + dependencies).
Distribute the entire folder as a ZIP. The user extracts it and runs
`TekkenRevOnline.exe` from inside.

**Technical note**: We use `--onedir` (not `--onefile`) because `--onefile`
extracts DLLs to a temp `_MEI*` directory that injects into the DLL search
path. When the installer spawns `rpcs3.exe`, RPCS3 inherits that path and
loads the bundled VCRUNTIME140.dll instead of the system one — and rejects
it. `--onedir` avoids this entirely.

### Linux

```bash
# If tkinter is missing: sudo apt install python3-tk
pip3 install pyinstaller
./build.sh
```

Output: **`dist/TekkenRevOnline/TekkenRevOnline`**. Distribute the folder.

### Cross-compilation

PyInstaller does **not** cross-compile:

- **Windows .exe**: build ON a Windows machine
- **Linux binary**: build ON a Linux machine (or WSL / Docker / VM)
- **Automatable** via GitHub Actions with `windows-latest` and `ubuntu-latest` runners

### Executable icon (Windows)

Place an `assets/logo.ico` file and PyInstaller will use it as the .exe icon.

## Open source — full transparency

Everything the installer does is in **one Python file** (`tekkennb.py`)
that you can read and audit.

- **The RPCS3 patch** is 1 byte at a specific offset (visible in code as
  `PATTERN_UNPATCHED_EXT` / `PATTERN_PATCHED_EXT`).
- **The EBOOT.BIN** is in `data/EBOOT.BIN` (pre-patched).
- **The `.pkg`** is downloaded from Sony's official CDN URL.

## License

MIT — use, modify, fork, and redistribute freely.

## Not included

- **RPCS3** itself (install from rpcs3.net)
