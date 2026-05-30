#!/usr/bin/env bash
# Linux/macOS one-click build script. Requires Python 3.8+ in PATH.
#
# Produces: dist/TekkenRevOnline/TekkenRevOnline

set -e
cd "$(dirname "$0")"

# Linux: tkinter is often a separate package
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "ERROR: python3-tk not installed."
    echo "Install it with one of:"
    echo "  sudo apt install python3-tk     (Debian/Ubuntu)"
    echo "  sudo dnf install python3-tkinter (Fedora)"
    echo "  sudo pacman -S tk               (Arch)"
    exit 1
fi

python3 build.py
