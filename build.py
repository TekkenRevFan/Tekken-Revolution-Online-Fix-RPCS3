#!/usr/bin/env python3
"""
build.py — Compile Tekken Revolution Online installer into a standalone executable.

Produces:
  - Windows: dist/TekkenRevOnline/TekkenRevOnline.exe  (~25-40 MB, no Python needed)
  - Linux:   dist/TekkenRevOnline/TekkenRevOnline      (~25-40 MB, no Python needed)

Usage:
  pip install pyinstaller
  python build.py

The output is a directory bundle that includes:
  - Python interpreter
  - tkinter GUI library
  - data/EBOOT.BIN (the pre-patched game executable)
  - data/PS3UPDAT.PUP (firmware)
  - data/*.rap (license)

The user just runs the executable — no Python install needed.

PyInstaller does NOT cross-compile: build on Windows for .exe, build on
Linux for Linux binary. For both, use a CI like GitHub Actions or build
in a Linux VM/Docker.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "tekkennb.py"
DATA_DIR = HERE / "data"
ASSETS_DIR = HERE / "assets"

# PyInstaller uses ';' on Windows and ':' on Linux/Mac for --add-data
SEP = ";" if os.name == "nt" else ":"

OUTPUT_NAME = "TekkenRevOnline"


def check_pyinstaller():
    try:
        subprocess.check_call(
            [sys.executable, "-m", "PyInstaller", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def kill_running_instance():
    """If the previously-built executable is running,
    terminate it so the build can overwrite the output."""
    if os.name != "nt":
        return
    exe_name = f"{OUTPUT_NAME}.exe"
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10)
        if exe_name.lower() in result.stdout.lower():
            print(f"Detected running {exe_name} — terminating it...")
            subprocess.run(
                ["taskkill", "/F", "/IM", exe_name],
                capture_output=True, text=True, timeout=10)
            # Give Windows a moment to fully release the file handle
            import time
            time.sleep(1.5)
    except Exception as e:
        print(f"(could not check for running {exe_name}: {e})")


def force_remove(path: Path, label: str):
    """Remove a file/dir with retries — Windows sometimes holds file
    handles open briefly after a process exits."""
    if not path.exists():
        return
    for attempt in range(5):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except PermissionError as e:
            print(f"  attempt {attempt+1}/5: still locked ({e}); retrying...")
            import time
            time.sleep(1.0)
    print(f"!! Could not remove {label} at {path}")
    print(f"   Close any open instances of {OUTPUT_NAME}.exe and try again.")
    sys.exit(1)


def main():
    if not check_pyinstaller():
        print("PyInstaller not installed.")
        print("Install it with:  pip install pyinstaller")
        print()
        ans = input("Install it now? [y/N] ").strip().lower()
        if ans == "y":
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        else:
            sys.exit(1)

    # If the previously-built .exe is running, kill it so we can overwrite
    kill_running_instance()

    # Clean previous builds (with retry-on-PermissionError)
    for d in ("build", "dist", "__pycache__"):
        p = HERE / d
        if p.exists():
            print(f"Cleaning {p}")
            force_remove(p, d)
    spec = HERE / f"{OUTPUT_NAME}.spec"
    if spec.exists():
        force_remove(spec, "spec file")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",                             # directory bundle (NOT --onefile)
        "--noconsole",                          # no console window (GUI only)
        "--name", OUTPUT_NAME,
        "--add-data", f"{DATA_DIR}{SEP}data",   # bundle data/
        "--add-data", f"{ASSETS_DIR}{SEP}assets", # bundle assets/
        "--clean",
        "--noconfirm",
        str(ENTRY),
    ]
    # WHY --onedir instead of --onefile:
    # In --onefile mode, PyInstaller extracts everything to a temporary
    # `_MEI*` directory in %TEMP%, and Windows's DLL search path for the
    # process includes that `_MEI*` dir. When the installer spawns any
    # child process (rpcs3.exe in our case), Windows lets that child
    # inherit the parent's DLL search, so it ends up loading our bundled
    # VCRUNTIME140.dll from `_MEI*` and RPCS3 refuses to run.
    # --onedir places all dependencies in a normal subfolder next to the
    # exe (no _MEI*, no special DLL search injection), so spawned child
    # processes get a clean Windows search order.

    # Optional: use logo as the exe icon (Windows only, requires .ico)
    icon_ico = ASSETS_DIR / "logo.ico"
    if icon_ico.exists():
        cmd.extend(["--icon", str(icon_ico)])
        print(f"Using icon: {icon_ico}")
    else:
        print("(no assets/logo.ico — exe will use the default icon)")

    print()
    print("Running:", " ".join(cmd))
    print()
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print()
        print(f"!!! Build failed (PyInstaller returned {result.returncode}) !!!")
        sys.exit(result.returncode)

    # Locate the output (--onedir puts everything in dist/<NAME>/)
    exe_name = f"{OUTPUT_NAME}.exe" if os.name == "nt" else OUTPUT_NAME
    bundle_dir = HERE / "dist" / OUTPUT_NAME
    output = bundle_dir / exe_name

    # ── Remove VC++ runtime DLLs from the bundle ──────────────────
    # PyInstaller bundles these "just in case" the system doesn't have
    # the VC++ Redistributable.  But their presence in _internal/ causes
    # RPCS3 (which we spawn as a child process) to load them from there
    # instead of System32.  RPCS3's module verifier then rejects the
    # non-system path and refuses to start.
    #
    # Since RPCS3 itself requires VC++ 2015-2022, it's safe to assume
    # the user has it installed.  Our app falls through to System32.
    internal_dir = bundle_dir / "_internal"
    vc_dlls = [
        "vcruntime140.dll", "vcruntime140_1.dll",
        "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "concrt140.dll",
    ]
    if internal_dir.is_dir():
        for dll in vc_dlls:
            p = internal_dir / dll
            if p.exists():
                p.unlink()
                print(f"Removed from bundle: _internal/{dll}")

    # Copy README.txt into the bundle root (next to the exe)
    readme_src = HERE / "README.txt"
    if readme_src.is_file():
        shutil.copy2(readme_src, bundle_dir / "README.txt")
        print(f"Copied README.txt into bundle")

    if output.exists():
        bundle_size = sum(f.stat().st_size for f in bundle_dir.rglob("*") if f.is_file())
        size_mb = bundle_size / (1024 * 1024)
        print()
        print("=" * 60)
        print(f"Build successful!")
        print(f"  Bundle directory: {bundle_dir}")
        print(f"  Executable:       {output}")
        print(f"  Bundle size:      {size_mb:.1f} MB (everything inside the folder)")
        print()
        print(f"  To distribute: zip the entire {OUTPUT_NAME}/ folder.")
        print(f"  Users extract the zip, then run {OUTPUT_NAME}/{exe_name}.")
        print(f"  No Python needed on the user's machine.")
        print("=" * 60)
    else:
        print(f"Build appeared to succeed but output not found at {output}")
        sys.exit(1)


if __name__ == "__main__":
    main()
