#!/usr/bin/env python3
"""
Tekken Revolution Online — Installer for RPCS3

Zero dependencies: pure Python 3.8+ stdlib. Tkinter GUI.
Open source — every byte the installer touches is visible in this file.

What it does (each step is opt-in via its own button):

  1. Install PS3 firmware via RPCS3 CLI.

  2. Patch rpcs3.exe / rpcs3 (Linux): 1 byte in .text section
     (cmp r13d, 10 -> cmp r13d, 40). Backup auto-created.

  3. Game install (only if missing): downloads the official Sony CDN
     .pkg of NPUB31250 and installs it via rpcs3's --installpkg.

  4. Game updates 1.01-1.05: official update packages from Sony CDN.

  5. .rap license install: bundled or user-supplied.

  6. Trophy mirror NPWR04645_01: copies TROPHY.TRP from _00/ to _01/.

  7. Patched EBOOT.BIN install: replaces the game EBOOT with the
     pre-patched one. Original auto-backed up.

  8. Custom config: writes config_NPUB31250.yml (60 FPS / Vulkan
     recommended settings) to <rpcs3>/config/custom_configs/.
     Does NOT touch the user's other configs, only the per-game one.

  9. RPCN account: open signup page in browser, OR enter existing
     credentials (NPID + password + token). Password is hashed with
     PBKDF2-HMAC-SHA3-256 (matching RPCS3) and written to rpcn.yml.

The user's config.yml (global preferences) and all other games'
data are NEVER modified.

Source: open-source repository (URL set per release)
License: MIT
"""

import hashlib
import os
import platform
import re
import shutil
import struct
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ----------------------------------------------------------------------- #
# Configuration constants
# ----------------------------------------------------------------------- #

APP_TITLE = "Tekken Revolution Online"
VERSION   = "1.0.0"

GAME_TITLE_ID = "NPUB31250"
GAME_PKG_URL  = (
    "http://zeus.dl.playstation.net/cdn/UP0700/NPUB31250_00/"
    "AwDgloExEVqYaPmkLlAfgqqVkFQRAcUsrMvCUBCUybGizAxEiBpfuLRzYEQZOjVi.pkg"
)
# Game updates 1.01..1.05, applied in order on top of the base install.
GAME_UPDATE_URLS = [
    ("1.01", "http://b0.ww.np.dl.playstation.net/tppkg/np/NPUB31250/NPUB31250_T4/15994ba7a66f2ed8/UP0700-NPUB31250_00-TEKKENREVOLUTION-A0101-V0100-PE.pkg"),
    ("1.02", "http://b0.ww.np.dl.playstation.net/tppkg/np/NPUB31250/NPUB31250_T4/15994ba7a66f2ed8/UP0700-NPUB31250_00-TEKKENREVOLUTION-A0102-V0100-PE.pkg"),
    ("1.03", "http://b0.ww.np.dl.playstation.net/tppkg/np/NPUB31250/NPUB31250_T4/15994ba7a66f2ed8/UP0700-NPUB31250_00-TEKKENREVOLUTION-A0103-V0100-PE.pkg"),
    ("1.04", "http://b0.ww.np.dl.playstation.net/tppkg/np/NPUB31250/NPUB31250_T4/15994ba7a66f2ed8/UP0700-NPUB31250_00-TEKKENREVOLUTION-A0104-V0100-PE.pkg"),
    ("1.05", "http://b0.ww.np.dl.playstation.net/tppkg/np/NPUB31250/NPUB31250_T4/15994ba7a66f2ed8/UP0700-NPUB31250_00-TEKKENREVOLUTION-A0105-V0100-PE.pkg"),
]
RAP_FILENAME       = "UP0700-NPUB31250_00-TEKKENREVOLUTION.rap"
TROPHY_COMM_BASE   = "NPWR04645_00"
TROPHY_COMM_MIRROR = "NPWR04645_01"

# P2PS retry patch — extended patterns for safe matching.
#
# The short pattern 41 83 FD 0A (cmp r13d, 10) appears ~15 times in
# rpcs3's .text section; blindly patching all of them corrupts the binary.
# The P2PS retry loop is the ONLY site where cmp r13d,10 is followed by
# a near backward jl (0F 8C xx xx xx xx with negative rel32).  We match
# this 6-byte extended pattern to ensure we only touch the retry loop.
#
# Unpatched: 41 83 FD 0A 0F 8C  (cmp r13d, 10; jl <backward>)
# Patched:   41 83 FD 28 0F 8C  (cmp r13d, 40; jl <backward>)
#
PATTERN_UNPATCHED_EXT = bytes([0x41, 0x83, 0xFD, 0x0A, 0x0F, 0x8C])
PATTERN_PATCHED_EXT   = bytes([0x41, 0x83, 0xFD, 0x28, 0x0F, 0x8C])
# Byte offset within the extended pattern where 0A→28 happens:
PATCH_BYTE_OFFSET = 3

# ----------------------------------------------------------------------- #
# Paths — resolve both in "python tekkennb.py" mode and in PyInstaller
#         bundle mode (where assets/data live under sys._MEIPASS)
# ----------------------------------------------------------------------- #

def _bundle_root() -> Path:
    """Return the directory where data/ and assets/ live, whether we're
    running as a plain script or as a PyInstaller bundle (--onefile or
    --onedir)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def installer_dir() -> Path:
    """The directory where the .exe / script file actually lives on disk.
    Different from _bundle_root() under PyInstaller (which points to the
    extracted temp _MEI* folder). This is the right place to put
    temporary downloads so they're alongside the installer file the
    user can see, and easy to clean up."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

BUNDLE_DIR = _bundle_root()
DATA_DIR   = BUNDLE_DIR / "data"
ASSETS_DIR = BUNDLE_DIR / "assets"
PATCHED_EBOOT     = DATA_DIR / "EBOOT.BIN"
RECOMMENDED_CFG   = DATA_DIR / "config_NPUB31250.yml"
BUNDLED_FIRMWARE  = DATA_DIR / "PS3UPDAT.PUP"
BUNDLED_RAP       = DATA_DIR / RAP_FILENAME


# ----------------------------------------------------------------------- #
# Helpers: binary patcher (PE / ELF detection)
# ----------------------------------------------------------------------- #

def detect_format_and_text(data: bytes):
    """Return ('PE'|'ELF', text_file_offset, text_size) or (None, None, None)."""
    # PE detection
    if data[:2] == b"MZ":
        e_lfanew = struct.unpack("<I", data[0x3C:0x40])[0]
        if data[e_lfanew:e_lfanew+4] == b"PE\x00\x00":
            num_sections = struct.unpack("<H", data[e_lfanew+6:e_lfanew+8])[0]
            opt_size     = struct.unpack("<H", data[e_lfanew+20:e_lfanew+22])[0]
            sec_start    = e_lfanew + 24 + opt_size
            for i in range(num_sections):
                s = sec_start + i * 40
                name = data[s:s+8].rstrip(b"\x00")
                if name == b".text":
                    raw_off  = struct.unpack("<I", data[s+20:s+24])[0]
                    raw_size = struct.unpack("<I", data[s+16:s+20])[0]
                    return ("PE", raw_off, raw_size)
    # ELF detection
    if data[:4] == b"\x7fELF":
        ei_class = data[4]
        ei_data  = data[5]
        endian   = "<" if ei_data == 1 else ">"
        is64 = (ei_class == 2)
        if is64:
            e_phoff   = struct.unpack(f"{endian}Q", data[0x20:0x28])[0]
            e_phentsz = struct.unpack(f"{endian}H", data[0x36:0x38])[0]
            e_phnum   = struct.unpack(f"{endian}H", data[0x38:0x3A])[0]
            for i in range(e_phnum):
                b = e_phoff + i * e_phentsz
                p_type  = struct.unpack(f"{endian}I", data[b:b+4])[0]
                p_flags = struct.unpack(f"{endian}I", data[b+4:b+8])[0]
                if p_type == 1 and (p_flags & 0x1):
                    p_offset = struct.unpack(f"{endian}Q", data[b+8 :b+16])[0]
                    p_filesz = struct.unpack(f"{endian}Q", data[b+32:b+40])[0]
                    return ("ELF", p_offset, p_filesz)
        else:
            e_phoff   = struct.unpack(f"{endian}I", data[0x1C:0x20])[0]
            e_phentsz = struct.unpack(f"{endian}H", data[0x2A:0x2C])[0]
            e_phnum   = struct.unpack(f"{endian}H", data[0x2C:0x2E])[0]
            for i in range(e_phnum):
                b = e_phoff + i * e_phentsz
                p_type  = struct.unpack(f"{endian}I", data[b:b+4])[0]
                p_flags = struct.unpack(f"{endian}I", data[b+24:b+28])[0]
                if p_type == 1 and (p_flags & 0x1):
                    p_offset = struct.unpack(f"{endian}I", data[b+4:b+8])[0]
                    p_filesz = struct.unpack(f"{endian}I", data[b+16:b+20])[0]
                    return ("ELF", p_offset, p_filesz)
    return (None, None, None)


def _find_p2ps_sites(text: bytes, text_off: int, pattern_ext: bytes):
    """Find P2PS retry sites in .text using the 6-byte extended pattern,
    then verify the jl (0F 8C) has a negative (backward) rel32 offset —
    the hallmark of a retry loop.  Returns list of file offsets."""
    hits = []
    start = 0
    while True:
        pos = text.find(pattern_ext, start)
        if pos == -1:
            break
        # Verify the jl rel32 after the 6-byte match is a BACKWARD jump
        jl_rel_off = pos + 6  # jl's rel32 starts right after 0F 8C
        if jl_rel_off + 4 <= len(text):
            rel32 = struct.unpack("<i", text[jl_rel_off:jl_rel_off + 4])[0]
            if rel32 < 0:
                hits.append(text_off + pos)
        start = pos + 1
    return hits


def check_rpcs3_patch_status(rpcs3_path: Path) -> str:
    """Return 'patched', 'unpatched', or 'unknown'.

    Uses the 6-byte extended pattern (cmp r13d,N + jl backward) so we
    only look at the actual P2PS retry loop, not the ~15 other sites in
    rpcs3 that happen to contain the same 4-byte sequence."""
    if not rpcs3_path.is_file():
        return "missing"
    try:
        with open(rpcs3_path, "rb") as f:
            data = f.read()
    except Exception:
        return "unknown"
    fmt, text_off, text_size = detect_format_and_text(data)
    if fmt is None:
        return "unknown"
    text = data[text_off:text_off + text_size]

    unpatched_sites = _find_p2ps_sites(text, text_off, PATTERN_UNPATCHED_EXT)
    patched_sites   = _find_p2ps_sites(text, text_off, PATTERN_PATCHED_EXT)

    if unpatched_sites:
        return "unpatched"
    if patched_sites:
        return "patched"
    return "unknown"


def apply_rpcs3_patch(rpcs3_path: Path, log) -> bool:
    with open(rpcs3_path, "rb") as f:
        data = f.read()
    fmt, text_off, text_size = detect_format_and_text(data)
    if fmt is None:
        log("ERROR: not a recognised PE or ELF binary")
        return False
    log(f"Detected format: {fmt}")
    text = data[text_off:text_off + text_size]

    positions = _find_p2ps_sites(text, text_off, PATTERN_UNPATCHED_EXT)

    if not positions:
        patched = _find_p2ps_sites(text, text_off, PATTERN_PATCHED_EXT)
        if patched:
            log("Already patched — nothing to do.")
            return True
        log("ERROR: P2PS retry pattern not found in .text.")
        log("  Expected: cmp r13d,10 + jl <backward> (41 83 FD 0A 0F 8C)")
        log("  This RPCS3 build may be too new/old for this patch.")
        return False

    backup = rpcs3_path.with_suffix(rpcs3_path.suffix + ".bak_pre_p2ps")
    if not backup.exists():
        shutil.copy2(rpcs3_path, backup)
        log(f"Backup created: {backup.name}")

    new_data = bytearray(data)
    for file_off in positions:
        target = file_off + PATCH_BYTE_OFFSET  # the 0A byte within the ext pattern
        old_val = new_data[target]
        new_data[target] = 0x28
        log(f"  Patched at file offset 0x{file_off:X}: "
            f"byte +{PATCH_BYTE_OFFSET} changed 0x{old_val:02X} -> 0x28")
    with open(rpcs3_path, "wb") as f:
        f.write(bytes(new_data))
    log(f"P2PS retry limit raised from 10 to 40 ({len(positions)} site(s)).")
    return True


def restore_rpcs3_patch(rpcs3_path: Path, log) -> bool:
    backup = rpcs3_path.with_suffix(rpcs3_path.suffix + ".bak_pre_p2ps")
    if not backup.exists():
        log(f"No backup found ({backup.name}); cannot restore.")
        return False
    shutil.copy2(backup, rpcs3_path)
    log(f"Restored {rpcs3_path.name} from {backup.name}")
    return True


# ----------------------------------------------------------------------- #
# Helpers: RPCS3 dev_hdd0 layout discovery
# ----------------------------------------------------------------------- #

def rpcs3_root(rpcs3_binary_path: Path) -> Path:
    return rpcs3_binary_path.parent


def game_usrdir(rpcs3_binary_path: Path) -> Path:
    return rpcs3_root(rpcs3_binary_path) / "dev_hdd0" / "game" / GAME_TITLE_ID / "USRDIR"


def game_tropdir(rpcs3_binary_path: Path) -> Path:
    return rpcs3_root(rpcs3_binary_path) / "dev_hdd0" / "game" / GAME_TITLE_ID / "TROPDIR"


def rap_exdata_dir(rpcs3_binary_path: Path) -> Path:
    # RAP files live in /dev_hdd0/home/00000001/exdata/
    return rpcs3_root(rpcs3_binary_path) / "dev_hdd0" / "home" / "00000001" / "exdata"


def custom_config_dir(rpcs3_binary_path: Path) -> Path:
    return rpcs3_root(rpcs3_binary_path) / "config" / "custom_configs"


def firmware_installed(rpcs3_binary_path: Path) -> bool:
    """RPCS3 extracts firmware to <rpcs3>/dev_flash/. We treat the
    presence of a known core library as proof the firmware is installed."""
    marker = rpcs3_root(rpcs3_binary_path) / "dev_flash" / "sys" / "external" / "libfs.sprx"
    return marker.is_file()


# ----------------------------------------------------------------------- #
# Download with progress
# ----------------------------------------------------------------------- #

def download_with_progress(url: str, dest: Path, progress_cb, log) -> bool:
    """progress_cb(bytes_done, bytes_total)."""
    try:
        log(f"Downloading {url}")
        log(f"  -> {dest}")
        req = urllib.request.Request(url, headers={"User-Agent": "TekkenRevOnline-Installer/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                done = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk: break
                    f.write(chunk)
                    done += len(chunk)
                    progress_cb(done, total)
        log(f"Download complete: {done:,} bytes")
        return True
    except Exception as e:
        log(f"Download failed: {e}")
        return False


# ----------------------------------------------------------------------- #
# Recommended config (preview content)
# ----------------------------------------------------------------------- #

# ----------------------------------------------------------------------- #
# RPCN account integration
# ----------------------------------------------------------------------- #

RPCN_DEFAULT_HOST = "np.rpcs3.net"
RPCN_DEFAULT_PORT = 31313                              # TCP port for RPCN server
RPCN_SIGNUP_URL = "https://rpcn.rpcs3.net/register"    # web signup page
RPCN_INFO_URL   = "https://rpcn.rpcs3.net/"            # general info page

# Each URL always redirects (302) to the latest release on GitHub.
RPCS3_DOWNLOADS = {
    "Windows": "https://github.com/RPCS3/rpcs3-binaries-win/releases/latest",
    "Linux":   "https://github.com/RPCS3/rpcs3-binaries-linux/releases/latest",
    "macOS":   "https://github.com/RPCS3/rpcs3-binaries-mac/releases/latest",
}
# GitHub REST API — any of the three repos shares the same version tag.
RPCS3_LATEST_API = "https://api.github.com/repos/RPCS3/rpcs3-binaries-win/releases/latest"


def rpcn_yml_path(rpcs3_binary_path: Path) -> Path:
    return rpcs3_root(rpcs3_binary_path) / "config" / "rpcn.yml"


RPCN_PW_SALT       = b"No matter where you go, everybody's connected."
RPCN_PW_ITERATIONS  = 200_000
RPCN_PW_DKLEN       = 32          # SHA3-256 digest length


def hash_rpcn_password(plain_password: str) -> str:
    """Derive the RPCN password using PBKDF2-HMAC-SHA3-256.

    Matches RPCS3's derive_password() in rpcn_settings_dialog.cpp:
      salt       = "No matter where you go, everybody's connected."
      iterations = 200 000
      output     = 32 bytes → 64-char uppercase hex
    """
    pwd = plain_password.encode("utf-8")
    for name in ("sha3_256", "sha3-256"):
        try:
            dk = hashlib.pbkdf2_hmac(
                name, pwd, RPCN_PW_SALT,
                RPCN_PW_ITERATIONS, RPCN_PW_DKLEN)
            return dk.hex().upper()
        except (ValueError, AttributeError):
            continue
    raise RuntimeError(
        "PBKDF2-HMAC-SHA3-256 not available in this Python build.\n"
        "Requires Python 3.8+ with OpenSSL 1.1.1+.\n"
        "Enter your password through RPCS3's own settings instead.")


def parse_rpcn_yml(path: Path) -> dict:
    """Cheap line-based YAML parser, just for the rpcn.yml schema."""
    out = {}
    if not path.is_file(): return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                if ":" in line:
                    k, _, v = line.partition(":")
                    out[k.strip()] = v.strip().strip('"')
    except Exception:
        pass
    return out


def write_rpcn_yml(path: Path, host: str, npid: str, password_hashed: str,
                   token: str = "") -> None:
    """Write rpcn.yml in the format RPCS3 expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "Version: 2\n"
        f"Host: {host}\n"
        f"NPID: {npid}\n"
        f"Password: {password_hashed}\n"
        f"Token: {token}\n"
        f"Hosts: Official RPCN Server|{host}\n"
        "Experimental IPv6 support: false\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


def _recv_exact(sock, n: int, timeout: float = 15.0) -> bytes | None:
    """Receive exactly *n* bytes from *sock*, or None on timeout/EOF."""
    import socket as _socket
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        except _socket.timeout:
            return None
    return buf


def test_rpcn_login(host: str, port: int,
                    npid: str, password_hash: str, token: str,
                    timeout: float = 15.0) -> tuple:
    """Perform a real RPCN login and return (success, message).

    This speaks the actual RPCN binary protocol:
      1. TLS connect
      2. Read ServerInfo (protocol version check)
      3. Send Login packet (NPID + password_hash + token)
      4. Read Reply and check ErrorType

    *password_hash* is the SHA256 hex as stored in rpcn.yml.
    The server applies Argon2 on top of it server-side.

    Zero external dependencies — pure stdlib (socket, ssl, struct).
    """
    import socket
    import ssl

    # -- protocol constants ------------------------------------------------
    HEADER_SIZE = 15
    # PacketType (u8)
    PT_REQUEST    = 0
    PT_REPLY      = 1
    PT_SERVERINFO = 3
    # CommandType (u16 LE)
    CMD_LOGIN = 0
    # ErrorType (u8)
    ERR_OK                = 0
    ERR_LOGIN_ERROR       = 5
    ERR_ALREADY_LOGGED_IN = 6
    ERR_INVALID_USERNAME  = 7
    ERR_INVALID_PASSWORD  = 8
    ERR_INVALID_TOKEN     = 9

    ERROR_LABELS = {
        ERR_OK:                "Login OK — credentials are valid",
        ERR_LOGIN_ERROR:       "Login error (server-side issue)",
        ERR_ALREADY_LOGGED_IN: "Already logged in (credentials valid, but RPCS3 is connected)",
        ERR_INVALID_USERNAME:  "Invalid username — NPID not registered on RPCN",
        ERR_INVALID_PASSWORD:  "Invalid password",
        ERR_INVALID_TOKEN:     "Invalid token (check your registration email)",
    }

    # -- Step 1: TLS connect -----------------------------------------------
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        return (False, f"Connection timed out ({host}:{port})")
    except ConnectionRefusedError:
        return (False, f"Connection refused — server may be down ({host}:{port})")
    except OSError as e:
        return (False, f"Connection failed: {e}")

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # RPCN uses self-signed certs
        tls = ctx.wrap_socket(raw, server_hostname=host)
    except Exception as e:
        raw.close()
        return (False, f"TLS handshake failed: {e}")

    try:
        # -- Step 2: read ServerInfo packet --------------------------------
        hdr = _recv_exact(tls, HEADER_SIZE, timeout)
        if hdr is None:
            return (False, "No ServerInfo from server (timeout)")
        if hdr[0] != PT_SERVERINFO:
            return (False, f"Expected ServerInfo, got packet type {hdr[0]}")

        # packet_size field INCLUDES the 15-byte header — subtract to
        # get the payload length.  (Confirmed against live server.)
        si_total = struct.unpack_from("<I", hdr, 3)[0]
        si_payload_len = max(si_total - HEADER_SIZE, 0)
        si_body = _recv_exact(tls, si_payload_len, timeout) if si_payload_len else b""
        server_ver = 0
        if si_body and len(si_body) >= 4:
            server_ver = struct.unpack_from("<I", si_body, 0)[0]

        # -- Step 3: send Login packet -------------------------------------
        payload = (
            npid.encode("utf-8") + b"\x00"
            + password_hash.encode("utf-8") + b"\x00"
            + token.encode("utf-8") + b"\x00"
        )
        # packet_size = header + payload (RPCN convention)
        header = struct.pack("<BHIQ",
                             PT_REQUEST,                # PacketType  (u8)
                             CMD_LOGIN,                 # CommandType (u16 LE)
                             HEADER_SIZE + len(payload), # PacketSize  (u32 LE)
                             1)                         # PacketID    (u64 LE)
        tls.sendall(header + payload)

        # -- Step 4: read Reply --------------------------------------------
        rhdr = _recv_exact(tls, HEADER_SIZE, timeout)
        if rhdr is None:
            return (False, "Server did not reply (timeout)")
        if rhdr[0] != PT_REPLY:
            return (False, f"Expected Reply, got packet type {rhdr[0]}")
        r_total = struct.unpack_from("<I", rhdr, 3)[0]
        r_payload_len = max(r_total - HEADER_SIZE, 0)
        r_body = _recv_exact(tls, r_payload_len, timeout) if r_payload_len else b""
        if not r_body:
            return (False, "Empty reply from server")

        err = r_body[0]
        label = ERROR_LABELS.get(err, f"Server error code {err}")

        if err == ERR_OK:
            return (True, label)
        if err == ERR_ALREADY_LOGGED_IN:
            # Credentials were accepted but NPID is online elsewhere
            return (True, label)
        return (False, label)

    except socket.timeout:
        return (False, "Server response timed out")
    except Exception as e:
        return (False, f"Protocol error: {e}")
    finally:
        try:
            tls.close()
        except Exception:
            pass


def create_rpcn_account(host: str, port: int,
                        npid: str, password_hash: str,
                        email: str,
                        timeout: float = 15.0) -> tuple:
    """Create a new RPCN account via the binary protocol.

    Payload: NPID\\0 + password_hash\\0 + online_name\\0 + avatar_url\\0 + email\\0
    CommandType = Create (2).

    avatar_url MUST be non-empty — the server's StreamExtractor uses
    get_string(false) for every field, which rejects empty strings
    and returns Malformed.  We use RPCS3's default avatar URL.

    Returns (success: bool, message: str).
    """
    import socket
    import ssl

    HEADER_SIZE   = 15
    PT_REQUEST    = 0
    PT_REPLY      = 1
    PT_SERVERINFO = 3
    CMD_CREATE    = 2

    ERR_OK                    = 0
    ERR_MALFORMED             = 1
    ERR_INVALID_INPUT         = 3
    ERR_CREATION_ERROR        = 10
    ERR_EXISTING_USERNAME     = 11
    ERR_BANNED_EMAIL          = 12
    ERR_EXISTING_EMAIL        = 13

    ERROR_LABELS = {
        ERR_OK:                 "Account created — check your email for the token",
        ERR_MALFORMED:          "Server rejected the request (Malformed packet)",
        ERR_INVALID_INPUT:      "Invalid username or email format",
        ERR_CREATION_ERROR:     "Account creation failed (server error)",
        ERR_EXISTING_USERNAME:  "Username already taken — choose a different NPID",
        ERR_BANNED_EMAIL:       "Email provider not allowed (use a different email)",
        ERR_EXISTING_EMAIL:     "Email already registered — use 'I have an account'",
    }

    # -- connect + TLS -------------------------------------------------
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:
        return (False, f"Connection failed: {e}")

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(raw, server_hostname=host)
    except Exception as e:
        raw.close()
        return (False, f"TLS failed: {e}")

    try:
        # -- ServerInfo ------------------------------------------------
        hdr = _recv_exact(tls, HEADER_SIZE, timeout)
        if hdr is None or hdr[0] != PT_SERVERINFO:
            return (False, "No ServerInfo from server")
        si_total = struct.unpack_from("<I", hdr, 3)[0]
        si_plen  = max(si_total - HEADER_SIZE, 0)
        if si_plen:
            _recv_exact(tls, si_plen, timeout)       # consume payload

        # -- Create packet ---------------------------------------------
        online_name = npid        # same as NPID (RPCS3 default)
        avatar_url  = "https://rpcs3.net/cdn/netplay/DefaultAvatar.png"
        payload = (
            npid.encode("utf-8")         + b"\x00"
            + password_hash.encode("utf-8") + b"\x00"
            + online_name.encode("utf-8")   + b"\x00"
            + avatar_url.encode("utf-8")    + b"\x00"
            + email.encode("utf-8")         + b"\x00"
        )
        header = struct.pack("<BHIQ",
                             PT_REQUEST,
                             CMD_CREATE,
                             HEADER_SIZE + len(payload),
                             1)
        tls.sendall(header + payload)

        # -- Reply -----------------------------------------------------
        rhdr = _recv_exact(tls, HEADER_SIZE, timeout)
        if rhdr is None:
            return (False, "Server did not reply (timeout)")
        r_total = struct.unpack_from("<I", rhdr, 3)[0]
        r_plen  = max(r_total - HEADER_SIZE, 0)
        r_body  = _recv_exact(tls, r_plen, timeout) if r_plen else b""
        if not r_body:
            return (False, "Empty reply from server")

        err = r_body[0]
        label = ERROR_LABELS.get(err, f"Server error code {err}")

        return (err == ERR_OK, label)

    except socket.timeout:
        return (False, "Server response timed out")
    except Exception as e:
        return (False, f"Protocol error: {e}")
    finally:
        try:
            tls.close()
        except Exception:
            pass


RECOMMENDED_CFG_CONTENT = """\
Core:
  PPU Decoder: Recompiler (LLVM)
  PPU Threads: 2
  SPU Decoder: Recompiler (LLVM)
  SPU Block Size: Safe
  Accurate SPU Reservations: true
  SPU XFloat Accuracy: Approximate
  PPU LLVM Java Mode Handling: true
Video:
  Renderer: Vulkan
  Resolution: 1280x720
  Aspect ratio: 16:9
  Frame limit: 60
  VSync Mode: Full
  Shader Mode: Async Recompiler (multi-threaded)
  Shader Precision: Low
  Write Color Buffers: true
  Resolution Scale: 150
  Anisotropic Filter Override: 0
  Multithreaded RSX: false
  Relaxed ZCULL Sync: true
Audio:
  Renderer: Cubeb
  Audio Format: Stereo
  Master Volume: 100
  Enable Buffering: true
  Desired Audio Buffer Duration: 34
System:
  License Area: SCEA
  Language: English (US)
Net:
  Internet enabled: Connected
  DNS address: 8.8.8.8
  UPNP Enabled: true
  PSN status: RPCN
Miscellaneous:
  Start games in fullscreen mode: true
  Prevent display sleep while running games: true
"""


# ----------------------------------------------------------------------- #
# GUI
# ----------------------------------------------------------------------- #

class TekkenRevApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1100x700")
        root.minsize(900, 520)

        # State
        self.rpcs3_path: Path | None = None
        self.rap_source: Path | None = None
        self.busy = False

        self._build_ui()
        # Try auto-detect rpcs3 in common locations
        self._auto_detect_rpcs3()

    # -- Layout ---------------------------------------------------------- #

    def _build_ui(self):
        # Header — red banner title
        header = tk.Frame(self.root, pady=10)
        header.pack(fill=tk.X)
        tk.Label(header, text=APP_TITLE, fg="red",
                 font=("TkDefaultFont", 20, "bold")).pack()

        # Body: two-column horizontal split.
        #   LEFT pane  = scrollable list of setup steps
        #   RIGHT pane = log (expandable) + footer
        body = tk.Frame(self.root)
        body.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        body.grid_columnconfigure(0, weight=3, minsize=420)  # left wider
        body.grid_columnconfigure(1, weight=2, minsize=320)  # right narrower
        body.grid_rowconfigure(0, weight=1)

        left_panel  = tk.Frame(body)
        right_panel = tk.Frame(body)
        left_panel .grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # Save reference so the right-pane builder can attach to it later
        self._right_panel = right_panel

        # Scrollable area inside the LEFT pane (steps).
        canvas = tk.Canvas(left_panel, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(left_panel, orient="vertical",
                            command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        content = tk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content,
                                              anchor="nw")

        # Keep the inner frame at the same width as the canvas (so child
        # widgets stretch horizontally) and update scrollregion when its
        # height changes.
        def _on_content_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(event):
            canvas.itemconfig(content_window, width=event.width)
        content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling (Windows / Linux / Mac).
        def _on_mousewheel(event):
            if hasattr(event, "delta") and event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)   # Windows / Mac
        canvas.bind_all("<Button-4>", _on_mousewheel)     # Linux scroll up
        canvas.bind_all("<Button-5>", _on_mousewheel)     # Linux scroll down

        # Make sure the scroll position starts at the top after layout.
        self.root.after(50, lambda: canvas.yview_moveto(0.0))

        # -- RPCS3 path picker --
        # Layout: row 1 = buttons + download link, row 2 = status text
        f0 = tk.LabelFrame(content, text="0. RPCS3 binary", padx=10, pady=8)
        f0.pack(fill=tk.X, pady=4)
        f0_btns = tk.Frame(f0)
        f0_btns.pack(fill=tk.X)
        tk.Button(f0_btns, text="Browse...", command=self._pick_rpcs3).pack(side=tk.LEFT, padx=2)

        # Download links — one per OS (Windows / Linux / macOS)
        self._rpcs3_ver_var = tk.StringVar(value="")
        link_font = ("TkDefaultFont", 9, "underline")

        tk.Label(f0_btns, text="Download RPCS3:").pack(side=tk.LEFT, padx=(12, 2))
        for os_name, url in RPCS3_DOWNLOADS.items():
            lbl = tk.Label(f0_btns, text=os_name, fg="blue",
                           cursor="hand2", font=link_font)
            lbl.pack(side=tk.LEFT, padx=3)
            lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

        # Version label — filled in by the background thread
        ver_lbl = tk.Label(f0_btns, textvariable=self._rpcs3_ver_var,
                           fg="gray40", font=("TkDefaultFont", 8))
        ver_lbl.pack(side=tk.LEFT, padx=(4, 0))

        self.path_var = tk.StringVar(value="(not selected)")
        tk.Label(f0, textvariable=self.path_var,
                 anchor="w").pack(fill=tk.X)

        # Fetch the current RPCS3 version tag in the background
        threading.Thread(target=self._fetch_rpcs3_latest_tag, daemon=True).start()

        # -- Step 1: install firmware (PS3UPDAT.PUP)
        fFW = tk.LabelFrame(content, text="1. Install PS3 firmware (PS3UPDAT.PUP)",
                            padx=10, pady=8)
        fFW.pack(fill=tk.X, pady=4)
        fFW_btns = tk.Frame(fFW)
        fFW_btns.pack(fill=tk.X)
        self.sFW_button = tk.Button(fFW_btns, text="Install firmware",
                                    command=self._install_firmware)
        self.sFW_button.pack(side=tk.LEFT, padx=2)
        tk.Button(fFW_btns, text="Refresh", command=self._refresh_firmware_status).pack(side=tk.LEFT, padx=2)
        self.sFW_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(fFW, textvariable=self.sFW_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 2: patch RPCS3 --
        f1 = tk.LabelFrame(content, text="2. Patch RPCS3 binary (P2PS retry)",
                           padx=10, pady=8)
        f1.pack(fill=tk.X, pady=4)
        f1_btns = tk.Frame(f1)
        f1_btns.pack(fill=tk.X)
        tk.Button(f1_btns, text="Apply patch", command=self._apply_patch).pack(side=tk.LEFT, padx=2)
        tk.Button(f1_btns, text="Restore", command=self._restore_patch).pack(side=tk.LEFT, padx=2)
        tk.Button(f1_btns, text="Refresh", command=self._refresh_patch_status).pack(side=tk.LEFT, padx=2)
        self.s1_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f1, textvariable=self.s1_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 3: game install --
        f2 = tk.LabelFrame(content, text="3. Game install (NPUB31250)",
                           padx=10, pady=8)
        f2.pack(fill=tk.X, pady=4)
        f2_btns = tk.Frame(f2)
        f2_btns.pack(fill=tk.X)
        self.s2_button = tk.Button(f2_btns, text="Download & install",
                                   command=self._download_game)
        self.s2_button.pack(side=tk.LEFT, padx=2)
        self.s2_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f2, textvariable=self.s2_status,
                 anchor="w").pack(fill=tk.X)
        self.s2_progress = ttk.Progressbar(f2, mode="determinate")
        self.s2_progress.pack(fill=tk.X, pady=(5, 0))

        # -- Step 4: game updates (1.01..1.05) --
        fUP = tk.LabelFrame(content, text="4. Game updates (1.01 - 1.05)",
                            padx=10, pady=8)
        fUP.pack(fill=tk.X, pady=4)
        fUP_btns = tk.Frame(fUP)
        fUP_btns.pack(fill=tk.X)
        self.sUP_button = tk.Button(fUP_btns, text="Download & install all updates",
                                    command=self._download_updates)
        self.sUP_button.pack(side=tk.LEFT, padx=2)
        self.sUP_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(fUP, textvariable=self.sUP_status,
                 anchor="w").pack(fill=tk.X)
        self.sUP_progress = ttk.Progressbar(fUP, mode="determinate")
        self.sUP_progress.pack(fill=tk.X, pady=(5, 0))

        # -- Step 5: .rap license --
        f3 = tk.LabelFrame(content, text="5. License .rap",
                           padx=10, pady=8)
        f3.pack(fill=tk.X, pady=4)
        f3_btns = tk.Frame(f3)
        f3_btns.pack(fill=tk.X)
        tk.Button(f3_btns, text="Install .rap", command=self._install_rap).pack(side=tk.LEFT, padx=2)
        tk.Button(f3_btns, text="Select .rap...", command=self._pick_rap).pack(side=tk.LEFT, padx=2)
        self.s3_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f3, textvariable=self.s3_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 6: trophy mirror --
        f4 = tk.LabelFrame(content, text="6. Trophy mirror (NPWR04645_01)",
                           padx=10, pady=8)
        f4.pack(fill=tk.X, pady=4)
        f4_btns = tk.Frame(f4)
        f4_btns.pack(fill=tk.X)
        tk.Button(f4_btns, text="Create mirror", command=self._make_trophy_mirror).pack(side=tk.LEFT, padx=2)
        self.s4_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f4, textvariable=self.s4_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 7: patched EBOOT --
        f5 = tk.LabelFrame(content, text="7. Patched EBOOT.BIN (online fixes)",
                           padx=10, pady=8)
        f5.pack(fill=tk.X, pady=4)
        f5_btns = tk.Frame(f5)
        f5_btns.pack(fill=tk.X)
        tk.Button(f5_btns, text="Install EBOOT", command=self._install_eboot).pack(side=tk.LEFT, padx=2)
        tk.Button(f5_btns, text="Restore vanilla", command=self._restore_eboot).pack(side=tk.LEFT, padx=2)
        self.s5_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f5, textvariable=self.s5_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 8: custom config --
        f6 = tk.LabelFrame(content, text="8. Recommended graphics config (per-game only)",
                           padx=10, pady=8)
        f6.pack(fill=tk.X, pady=4)
        f6_btns = tk.Frame(f6)
        f6_btns.pack(fill=tk.X)
        tk.Button(f6_btns, text="Install config", command=self._install_config).pack(side=tk.LEFT, padx=2)
        tk.Button(f6_btns, text="Preview", command=self._preview_config).pack(side=tk.LEFT, padx=2)
        self.s6_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f6, textvariable=self.s6_status,
                 anchor="w").pack(fill=tk.X)

        # -- Step 9: RPCN account --
        f7 = tk.LabelFrame(content, text="9. RPCN account (online matchmaking)",
                           padx=10, pady=8)
        f7.pack(fill=tk.X, pady=4)
        f7_btns = tk.Frame(f7)
        f7_btns.pack(fill=tk.X)
        tk.Button(f7_btns, text="I have an account",   command=self._enter_rpcn_credentials).pack(side=tk.LEFT, padx=2)
        tk.Button(f7_btns, text="Create new account",  command=self._open_rpcn_signup).pack(side=tk.LEFT, padx=2)
        tk.Button(f7_btns, text="Test connection", command=self._test_rpcn).pack(side=tk.LEFT, padx=2)
        tk.Button(f7_btns, text="Refresh", command=self._refresh_rpcn_status).pack(side=tk.LEFT, padx=2)
        self.s7_status = tk.StringVar(value="(select rpcs3 first)")
        tk.Label(f7, textvariable=self.s7_status,
                 anchor="w").pack(fill=tk.X)

        # -- RIGHT panel content: Log (expandable) + Footer (bottom) --
        log_frame = tk.LabelFrame(self._right_panel, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.log_widget = scrolledtext.ScrolledText(
            log_frame, height=8, wrap=tk.WORD)
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        self.log_widget.configure(state=tk.DISABLED)

        # Footer — version line + open-source statement
        footer = tk.Frame(self._right_panel)
        footer.pack(fill=tk.X, pady=(0, 0))

        tk.Label(footer,
                 text=f"v{VERSION}  |  open source  |  MIT license",
                 font=("TkDefaultFont", 9)).pack(anchor="w")

        tk.Label(footer,
                 text=("Free and public open-source software. Build it yourself from source with\n"
                       "complete confidence — no tedious or complex setups."),
                 font=("TkDefaultFont", 9), justify="left"
                 ).pack(anchor="w", pady=(4, 0))

    # -- Logging --------------------------------------------------------- #

    def log(self, msg: str):
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, msg + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)
        self.root.update_idletasks()

    # -- Auto-detect RPCS3 ---------------------------------------------- #

    def _auto_detect_rpcs3(self):
        candidates = []
        system = platform.system()
        if system == "Windows":
            candidates = [
                Path("C:/RPCS3/rpcs3.exe"),
                Path.home() / "RPCS3" / "rpcs3.exe",
                Path.home() / "Desktop" / "rpcs3" / "rpcs3.exe",
            ]
        else:
            candidates = [
                Path("/usr/bin/rpcs3"),
                Path("/usr/local/bin/rpcs3"),
                Path.home() / ".local/bin/rpcs3",
                Path.home() / "Applications/rpcs3.AppImage",
            ]
        for c in candidates:
            if c.exists():
                self._set_rpcs3_path(c)
                return

    def _fetch_rpcs3_latest_tag(self):
        """Background: hit GitHub API and show the latest version next
        to the OS download links, e.g. '(v0.0.40-19400)'."""
        try:
            import urllib.request, json
            req = urllib.request.Request(
                RPCS3_LATEST_API,
                headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "TekkenRevOnline-Installer/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            # tag_name looks like "build-<commit>"; the asset name has
            # the human-readable version, e.g. rpcs3-v0.0.40-19400-…
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".7z") and not name.endswith(".sha256"):
                    # Extract "v0.0.40-19400" from "rpcs3-v0.0.40-19400-abc_win64_msvc.7z"
                    parts = name.split("-")          # ['rpcs3', 'v0.0.40', '19400', ...]
                    if len(parts) >= 3:
                        ver = f"{parts[1]}-{parts[2]}"      # "v0.0.40-19400"
                        self.root.after(0, lambda v=ver:
                            self._rpcs3_ver_var.set(f"({v})"))
                    break
        except Exception:
            pass  # version label stays empty — links still work

    def _pick_rpcs3(self):
        system = platform.system()
        if system == "Windows":
            ft = [("RPCS3 executable", "*.exe"), ("All files", "*.*")]
        else:
            ft = [("All files", "*.*")]
        p = filedialog.askopenfilename(title="Select RPCS3 executable", filetypes=ft)
        if p:
            self._set_rpcs3_path(Path(p))

    def _set_rpcs3_path(self, p: Path):
        self.rpcs3_path = p
        self.path_var.set(str(p))
        self.log(f"RPCS3 selected: {p}")
        # Clean up any VC++ DLLs a previous installer run may have staged
        # next to rpcs3.exe — RPCS3 rejects them on startup.
        self._cleanup_staged_dlls()
        self._refresh_all_status()

    # -- Status refresh -------------------------------------------------- #

    def _refresh_all_status(self):
        self._refresh_firmware_status()
        self._refresh_patch_status()
        self._refresh_game_status()
        self._refresh_updates_status()
        self._refresh_rap_status()
        self._refresh_trophy_status()
        self._refresh_eboot_status()
        self._refresh_config_status()
        self._refresh_rpcn_status()

    def _refresh_updates_status(self):
        if not self.rpcs3_path:
            self.sUP_status.set("(select rpcs3 first)")
            return
        # We can't easily check which updates are installed (RPCS3 merges
        # them into the game's USRDIR transparently). Just show a hint.
        eboot = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if eboot.is_file():
            self.sUP_status.set(f"Ready to download/install {len(GAME_UPDATE_URLS)} updates")
        else:
            self.sUP_status.set("✗ Install the base game first")

    def _refresh_firmware_status(self):
        if not self.rpcs3_path:
            self.sFW_status.set("(select rpcs3 first)")
            return
        if firmware_installed(self.rpcs3_path):
            self.sFW_status.set("✓ PS3 firmware installed")
        elif BUNDLED_FIRMWARE.is_file():
            mb = BUNDLED_FIRMWARE.stat().st_size / (1024*1024)
            self.sFW_status.set(f"✗ Firmware not installed (bundled PUP: {mb:.0f} MB ready)")
        else:
            self.sFW_status.set("✗ Firmware not installed (PUP not bundled either)")

    def _refresh_patch_status(self):
        if not self.rpcs3_path:
            self.s1_status.set("(select rpcs3 first)")
            return
        status = check_rpcs3_patch_status(self.rpcs3_path)
        msg = {
            "patched":   "✓ Patched (P2PS retry = 40)",
            "unpatched": "✗ Not patched (P2PS retry = 10, default)",
            "missing":   "✗ Binary not found",
            "unknown":   "? Unknown — pattern not detected",
        }.get(status, "?")
        self.s1_status.set(msg)

    def _refresh_game_status(self):
        if not self.rpcs3_path:
            self.s2_status.set("(select rpcs3 first)")
            return
        eboot = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if eboot.is_file():
            self.s2_status.set(f"✓ Game installed ({eboot.stat().st_size:,} bytes)")
        else:
            self.s2_status.set("✗ Game not installed")

    def _refresh_rap_status(self):
        if not self.rpcs3_path:
            self.s3_status.set("(select rpcs3 first)")
            return
        rap = rap_exdata_dir(self.rpcs3_path) / RAP_FILENAME
        if rap.is_file():
            self.s3_status.set(f"✓ License installed")
        elif BUNDLED_RAP.is_file():
            self.s3_status.set("✗ Not installed — bundled .rap ready (click Install)")
        else:
            src_info = f" (source: {self.rap_source})" if self.rap_source else ""
            self.s3_status.set(f"✗ License not installed{src_info}")

    def _refresh_trophy_status(self):
        if not self.rpcs3_path:
            self.s4_status.set("(select rpcs3 first)")
            return
        trp = game_tropdir(self.rpcs3_path) / TROPHY_COMM_MIRROR / "TROPHY.TRP"
        if trp.is_file():
            self.s4_status.set("✓ Trophy mirror present")
        else:
            src = game_tropdir(self.rpcs3_path) / TROPHY_COMM_BASE / "TROPHY.TRP"
            if src.is_file():
                self.s4_status.set("✗ Mirror missing (source _00 OK, can create)")
            else:
                self.s4_status.set("✗ Mirror missing (and _00 source not found either)")

    def _refresh_eboot_status(self):
        if not self.rpcs3_path:
            self.s5_status.set("(select rpcs3 first)")
            return
        eboot = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if not eboot.is_file():
            self.s5_status.set("✗ Install the game first")
            return
        # Compare hash to detect patched vs vanilla
        if PATCHED_EBOOT.exists():
            try:
                with open(PATCHED_EBOOT, "rb") as f: patched = f.read()
                with open(eboot, "rb")        as f: current = f.read()
                if hashlib.sha256(patched).digest() == hashlib.sha256(current).digest():
                    self.s5_status.set("✓ Patched EBOOT installed")
                    return
            except Exception:
                pass
        self.s5_status.set("✗ Vanilla EBOOT (online fixes not applied)")

    def _refresh_config_status(self):
        if not self.rpcs3_path:
            self.s6_status.set("(select rpcs3 first)")
            return
        cfg = custom_config_dir(self.rpcs3_path) / f"config_{GAME_TITLE_ID}.yml"
        if cfg.is_file():
            self.s6_status.set(f"✓ Custom config present")
        else:
            self.s6_status.set("✗ Custom config not installed")

    def _refresh_rpcn_status(self):
        if not self.rpcs3_path:
            self.s7_status.set("(select rpcs3 first)")
            return
        cfg = parse_rpcn_yml(rpcn_yml_path(self.rpcs3_path))
        npid  = cfg.get("NPID", "")
        pwd   = cfg.get("Password", "")
        host  = cfg.get("Host", "")
        token = cfg.get("Token", "")
        if npid and pwd and len(pwd) >= 32 and host and token:
            self.s7_status.set(f"✓ '{npid}' @ {host} (token set)")
        elif npid and pwd and len(pwd) >= 32 and host and not token:
            self.s7_status.set(f"⚠ '{npid}' @ {host} — TOKEN MISSING (check email)")
        elif npid or pwd:
            self.s7_status.set("⚠ Partial credentials — re-enter")
        else:
            self.s7_status.set("✗ No RPCN account configured")

    # -- Step actions ---------------------------------------------------- #

    def _check_rpcs3(self) -> bool:
        if not self.rpcs3_path or not self.rpcs3_path.is_file():
            messagebox.showerror("RPCS3 not selected",
                "Select your RPCS3 binary (rpcs3.exe on Windows, rpcs3 on Linux) first.")
            return False
        return True

    def _cleanup_staged_dlls(self) -> None:
        """Remove any VC++ runtime DLLs that a previous installer run placed
        next to rpcs3.exe. RPCS3 explicitly checks that these DLLs are NOT in
        its own folder — they must come from the system-wide VC++ Redistributable.
        We only remove a DLL when System32 has it too, so RPCS3 can still load
        it from the correct location afterwards."""
        if os.name != "nt" or not self.rpcs3_path:
            return
        rpcs3_dir = self.rpcs3_path.parent
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        for dll in ("vcruntime140.dll", "vcruntime140_1.dll",
                    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                    "concrt140.dll"):
            dst = rpcs3_dir / dll
            if not dst.exists():
                continue
            # Only remove it when System32 has it (so RPCS3 can still find it)
            if not (system32 / dll).exists():
                self.log(f"  WARNING: {dll} in rpcs3 dir but not in System32 — "
                         f"install VC++ 2015-2022 x64 Redistributable")
                continue
            try:
                dst.unlink()
                self.log(f"  removed misplaced DLL: {dll}")
            except Exception as e:
                self.log(f"  (could not remove {dll}: {e})")

    def _clean_env_for_child(self) -> dict:
        """Return a copy of the current environment with PyInstaller's
        bundle directories stripped from PATH.

        PyInstaller (both --onefile and --onedir) prepends its extraction
        directory to PATH.  Child processes inherit that PATH and end up
        finding bundled DLLs (vcruntime140.dll, msvcp140.dll, …) there
        instead of the system-installed copies.  RPCS3 checks the load
        path of those DLLs and refuses to start if they come from a
        non-system location.

        Previous code filtered for '_MEI' which only matches the --onefile
        temp dir.  In --onedir mode the directory is called '_internal',
        so that filter missed it.  We now strip ANY PATH entry that falls
        inside sys._MEIPASS or the frozen executable's own directory."""
        env = os.environ.copy()
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return env
        meipass  = getattr(sys, "_MEIPASS", "")
        exe_dir  = str(Path(sys.executable).parent)
        if "PATH" not in env:
            return env
        old_parts = env["PATH"].split(os.pathsep)
        clean, stripped = [], []
        for p in old_parts:
            skip = False
            try:
                norm = os.path.normcase(os.path.abspath(p))
                if meipass and norm.startswith(
                        os.path.normcase(os.path.abspath(meipass))):
                    skip = True
                elif exe_dir and norm.startswith(
                        os.path.normcase(os.path.abspath(exe_dir))):
                    skip = True
            except Exception:
                pass
            (stripped if skip else clean).append(p)
        if stripped:
            env["PATH"] = os.pathsep.join(clean)
            for s in stripped:
                self.log(f"  stripped from PATH: {s}")
        return env

    def _rpcs3_run(self, *cli_args: str) -> int:
        """Launch rpcs3 and wait.

        PyInstaller's C bootloader calls SetDllDirectoryW(_internal/) at
        startup.  That Win32 call is **inherited by child processes** via
        CreateProcess — so rpcs3 finds the bundled vcruntime140.dll before
        the system copy, and RPCS3's module verifier rejects it.

        The fix (documented in PyInstaller issue #3795): call
        SetDllDirectoryW(NULL) right before CreateProcess to restore the
        default DLL search order.  Combined with stripping _internal/
        from PATH, this completely eliminates the contamination.
        """
        self._cleanup_staged_dlls()
        import subprocess

        cmdline = [str(self.rpcs3_path), *cli_args]
        self.log(f"  cmd: {' '.join(cmdline)}")

        # ---- DLL isolation (Windows + PyInstaller only) ----
        env = self._clean_env_for_child()       # strip _internal from PATH
        if os.name == "nt" and getattr(sys, "frozen", False):
            try:
                import ctypes
                # Reset the DLL search directory that PyInstaller set.
                # Must happen BEFORE CreateProcess (subprocess.run).
                ctypes.windll.kernel32.SetDllDirectoryW(None)
                self.log("  SetDllDirectoryW(NULL) — DLL search reset")
            except Exception as e:
                self.log(f"  (SetDllDirectoryW failed: {e})")

        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.run(
            cmdline,
            cwd=str(self.rpcs3_path.parent),
            env=env,
            creationflags=flags,
            capture_output=True, text=True, timeout=900)
        self.log(f"  returncode: {proc.returncode}")
        if proc.stderr and proc.stderr.strip():
            self.log(f"  stderr: {proc.stderr[-400:].strip()}")
        return proc.returncode

    def _rpcs3_open_gui(self):
        """Launch RPCS3 in normal GUI mode (no CLI flags)."""
        import subprocess
        env = self._clean_env_for_child()
        if os.name == "nt" and getattr(sys, "frozen", False):
            try:
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            except Exception:
                pass
        subprocess.Popen(
            [str(self.rpcs3_path)],
            env=env,
            cwd=str(self.rpcs3_path.parent))

    # -- Win32 dialog auto-accept ----------------------------------------- #

    def _auto_accept_dialogs(self, proc, timeout=120):
        """Find dialog windows belonging to *proc* (by PID) and auto-click
        any Yes / OK / Install button.  Runs in a background thread.

        Uses Win32 EnumWindows + EnumChildWindows to discover buttons
        inside Qt dialogs.  Falls back to sending the Enter key to the
        window if no child buttons are found (Qt renders its own widgets
        and sometimes child HWNDs are invisible to EnumChildWindows).

        Keeps running until the process exits or *timeout* — handles
        multiple sequential dialogs (needed for batch update installs)."""
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        import time

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        GWL_STYLE    = -16
        WS_POPUP     = 0x80000000
        BM_CLICK     = 0x00F5
        WM_KEYDOWN   = 0x0100
        WM_KEYUP     = 0x0101
        VK_RETURN    = 0x0D
        ACCEPT_WORDS = {"yes", "ok", "install", "si", "sí", "accept",
                        "&yes", "&ok", "&install"}

        pid = proc.pid
        start = time.time()
        accepted_hwnds = set()          # track windows we already handled
        main_hwnd = None                # will be set to the largest window

        while time.time() - start < timeout:
            if proc.poll() is not None:
                return                          # process already gone
            time.sleep(0.4)

            # ---- collect top-level windows for this PID ----
            windows = []
            def _enum_top(hwnd, _lp):
                if user32.IsWindowVisible(hwnd):
                    wpid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                    if wpid.value == pid:
                        windows.append(hwnd)
                return True
            user32.EnumWindows(WNDENUMPROC(_enum_top), 0)

            if not windows:
                continue

            # Identify the main RPCS3 window (the largest one) so we
            # never send Enter to it.  Dialog windows are smaller.
            if main_hwnd is None or main_hwnd not in windows:
                best_area, best_hw = 0, None
                rect = wintypes.RECT()
                for hw in windows:
                    if user32.GetWindowRect(hw, ctypes.byref(rect)):
                        area = (rect.right - rect.left) * (rect.bottom - rect.top)
                        if area > best_area:
                            best_area, best_hw = area, hw
                if best_hw is not None:
                    main_hwnd = best_hw

            for hwnd in windows:
                if hwnd == main_hwnd:
                    continue                    # never touch the main window
                if hwnd in accepted_hwnds:
                    continue                    # already accepted this one

                # ---- try 1: find a child "Button" with accept text ----
                clicked = [False]
                def _enum_btn(child, _lp):
                    cn = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(child, cn, 64)
                    if "button" not in cn.value.lower():
                        return True
                    length = user32.GetWindowTextLengthW(child)
                    if length <= 0:
                        return True
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(child, buf, length + 1)
                    text = buf.value.strip().lower()
                    if text in ACCEPT_WORDS or any(w in text for w in ACCEPT_WORDS):
                        user32.PostMessageW(child, BM_CLICK, 0, 0)
                        clicked[0] = True
                        return False            # stop enum
                    return True
                user32.EnumChildWindows(hwnd, WNDENUMPROC(_enum_btn), 0)
                if clicked[0]:
                    self.log("  Auto-accepted RPCS3 dialog (button click)")
                    accepted_hwnds.add(hwnd)
                    time.sleep(0.5)             # cooldown before next accept
                    continue

                # ---- try 2: smart keyboard accept ----
                # Qt renders its own buttons — EnumChildWindows often
                # finds nothing.  Problem: Enter activates the *default*
                # button, which Qt may set to "No" in Yes/No dialogs.
                #
                # Fix: use keybd_event to send mnemonic shortcuts first:
                #   Alt+Y  → Qt "&Yes"  button  (English)
                #   Alt+S  → Qt "&Sí"   button  (Spanish)
                # Then fall back to Enter for OK-only dialogs where
                # the default button IS the one we want.
                KEYEVENTF_KEYUP = 0x0002
                VK_MENU = 0x12          # Alt key
                VK_Y    = 0x59
                VK_S    = 0x53

                title_len = user32.GetWindowTextLengthW(hwnd)
                title_str = ""
                if title_len > 0:
                    tbuf = ctypes.create_unicode_buffer(title_len + 1)
                    user32.GetWindowTextW(hwnd, tbuf, title_len + 1)
                    title_str = tbuf.value

                # Bring dialog to foreground so keybd_event reaches it
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.1)

                # Alt+Y — activates "&Yes" via Qt mnemonic
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_Y, 0, 0, 0)
                user32.keybd_event(VK_Y, 0, KEYEVENTF_KEYUP, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.15)

                # If still visible, try Alt+S (Spanish "&Sí")
                if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                    user32.keybd_event(VK_MENU, 0, 0, 0)
                    user32.keybd_event(VK_S, 0, 0, 0)
                    user32.keybd_event(VK_S, 0, KEYEVENTF_KEYUP, 0)
                    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.15)

                # Still visible → OK-only dialog; Enter activates default
                if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
                    user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)

                self.log(f"  Auto-accepted RPCS3 dialog ('{title_str}')")
                accepted_hwnds.add(hwnd)
                time.sleep(0.5)                 # cooldown

    # -- RPCS3 install helpers ---------------------------------------------- #

    def _rpcs3_launch_popen(self, *cli_args):
        """Launch RPCS3 as a Popen (non-blocking), with DLL isolation.
        Returns the Popen object."""
        import subprocess
        self._cleanup_staged_dlls()
        cmdline = [str(self.rpcs3_path), *cli_args]
        self.log(f"  cmd: {' '.join(cmdline)}")

        env = self._clean_env_for_child()
        if os.name == "nt" and getattr(sys, "frozen", False):
            try:
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
                self.log("  SetDllDirectoryW(NULL) — DLL search reset")
            except Exception as e:
                self.log(f"  (SetDllDirectoryW failed: {e})")

        # No CREATE_NO_WINDOW — Qt must be able to create its windows
        # (the dialog auto-accepter handles them).
        proc = subprocess.Popen(
            cmdline,
            cwd=str(self.rpcs3_path.parent),
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.log(f"  RPCS3 launched (PID {proc.pid})")
        return proc

    def _wait_for_install(self, proc, watch_dir: Path,
                          label: str = "install", max_wait: int = 600):
        """Wait for files under *watch_dir* to appear and stabilise, then
        terminate RPCS3.  Returns True if files changed (success) or
        False if RPCS3 exited/timed out without changes (cancel/error)."""
        import time

        def _snap():
            """Return (file_count, total_bytes, newest_mtime) so we detect
            both new files and modifications to existing ones (updates)."""
            if not watch_dir.is_dir():
                return (0, 0, 0.0)
            try:
                count, total, newest = 0, 0, 0.0
                for f in watch_dir.rglob("*"):
                    if f.is_file():
                        st = f.stat()
                        count += 1
                        total += st.st_size
                        if st.st_mtime > newest:
                            newest = st.st_mtime
                return (count, total, newest)
            except Exception:
                return (0, 0, 0.0)

        snap_before = _snap()
        stable_count = 0
        last_snap = snap_before
        poll = 2.0
        elapsed = 0.0
        started = False

        while elapsed < max_wait:
            ret = proc.poll()
            if ret is not None:
                # RPCS3 exited — check if files actually changed
                final = _snap()
                if final != snap_before:
                    self.log(f"  RPCS3 exited (code {ret}), {label} files changed — OK")
                    return True
                self.log(f"  RPCS3 exited (code {ret}) WITHOUT changes — "
                         f"{label} was likely cancelled or failed")
                return False

            time.sleep(poll)
            elapsed += poll

            current = _snap()
            if current != snap_before and not started:
                started = True
                self.log(f"  {label.capitalize()} activity detected (files being written)...")

            if started:
                if current == last_snap:
                    stable_count += 1
                else:
                    stable_count = 0
                last_snap = current

                if stable_count >= 3:
                    self.log(f"  {label.capitalize()} complete "
                             f"(stable for {stable_count * poll:.0f}s)")
                    break

        # Terminate RPCS3
        if proc.poll() is None:
            self.log(f"  Terminating RPCS3 (auto-close after {label})...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)

        final = _snap()
        return final != snap_before

    def _rpcs3_installpkg(self, pkg_path: Path) -> int:
        """Install a .pkg via RPCS3 CLI.

        Launches RPCS3, auto-accepts the confirmation dialog via Win32
        window enumeration, waits for install to complete (file monitoring),
        then terminates RPCS3.  Returns 0 on success, 1 on failure/cancel.
        """
        proc = self._rpcs3_launch_popen("--installpkg", str(pkg_path))

        # Auto-accept thread: finds the RPCS3 dialog and clicks Yes
        accept_thread = threading.Thread(
            target=self._auto_accept_dialogs, args=(proc,),
            daemon=True)
        accept_thread.start()

        game_dir = game_usrdir(self.rpcs3_path).parent
        ok = self._wait_for_install(proc, game_dir, "install", max_wait=600)
        return 0 if ok else 1

    def _install_firmware(self):
        if not self._check_rpcs3(): return
        if self.busy:
            self.log("Already busy — ignoring duplicate click.")
            return
        if firmware_installed(self.rpcs3_path):
            if not messagebox.askyesno("Firmware already installed",
                "PS3 firmware appears to already be installed.\n"
                "Reinstall anyway?"):
                return
        if not BUNDLED_FIRMWARE.is_file():
            messagebox.showerror("Missing PS3UPDAT.PUP",
                f"data/PS3UPDAT.PUP not found in installer.\n"
                f"Expected at: {BUNDLED_FIRMWARE}")
            return

        def worker():
            self.busy = True
            try:
                self.log("=" * 50)
                self.log("Installing PS3 firmware via RPCS3 CLI...")
                self.sFW_status.set("Installing firmware (this takes 1-3 min)...")
                self.root.update_idletasks()
                rc = self._rpcs3_installfw(BUNDLED_FIRMWARE)
                self.log(f"  RPCS3 firmware install exited with code {rc}")
                if firmware_installed(self.rpcs3_path):
                    self.log("Firmware install verified — dev_flash populated.")
                    self.sFW_status.set("✓ PS3 firmware installed")
                else:
                    self.log("WARNING: RPCS3 exited but firmware not detected.")
                    self.log("  Try running RPCS3 manually: File > Install Firmware")
                    self.sFW_status.set("⚠ Install may have failed — click 🔄 to check")
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.sFW_status.set("✗ Error during firmware install")
            finally:
                self.busy = False
                try: self.sFW_button.configure(state=tk.NORMAL)
                except Exception: pass

        try: self.sFW_button.configure(state=tk.DISABLED)
        except Exception: pass
        threading.Thread(target=worker, daemon=True).start()

    def _rpcs3_installfw(self, pup_path: Path) -> int:
        """Install firmware via RPCS3 --installfw with auto-accept + auto-close."""
        proc = self._rpcs3_launch_popen("--installfw", str(pup_path))

        accept_thread = threading.Thread(
            target=self._auto_accept_dialogs, args=(proc,),
            daemon=True)
        accept_thread.start()

        flash_dir = rpcs3_root(self.rpcs3_path) / "dev_flash"
        ok = self._wait_for_install(proc, flash_dir, "firmware", max_wait=300)
        return 0 if ok else 1

    def _apply_patch(self):
        if not self._check_rpcs3(): return
        if self.busy: return
        self.busy = True
        try:
            apply_rpcs3_patch(self.rpcs3_path, self.log)
            self._refresh_patch_status()
        finally:
            self.busy = False

    def _restore_patch(self):
        if not self._check_rpcs3(): return
        restore_rpcs3_patch(self.rpcs3_path, self.log)
        self._refresh_patch_status()

    def _download_game(self):
        if not self._check_rpcs3(): return
        if self.busy:
            self.log("Already busy — ignoring duplicate click.")
            return
        eboot = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if eboot.is_file():
            if not messagebox.askyesno("Game already installed",
                "The game appears to already be installed.\n"
                "Download and reinstall anyway?"):
                return

        def worker():
            self.busy = True
            pkg_path = installer_dir() / f"{GAME_TITLE_ID}.pkg"
            try:
                self.log("=" * 50)
                self.log("Downloading official Sony CDN .pkg...")
                self.log(f"  destination: {pkg_path}")

                def cb(done, total):
                    if total > 0:
                        self.s2_progress["value"] = done * 100 / total
                        mb_done  = done  / (1024*1024)
                        mb_total = total / (1024*1024)
                        self.s2_status.set(f"Downloading: {mb_done:.1f} / {mb_total:.1f} MB")

                if not download_with_progress(GAME_PKG_URL, pkg_path, cb, self.log):
                    self.s2_status.set("✗ Download failed")
                    return

                # Install via rpcs3 CLI
                self.log("Installing .pkg via RPCS3 CLI...")
                self.s2_status.set("Installing .pkg (this can take a few minutes)...")
                self.root.update_idletasks()
                rc = self._rpcs3_installpkg(pkg_path)
                self.log(f"  rpcs3 exited with code {rc}")

                # Verify the install actually happened — RPCS3 can return 0
                # even if it bailed early (e.g. its own startup error dialog).
                eboot_after = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
                if eboot_after.is_file():
                    self.log("Install verified — EBOOT.BIN present.")
                    self._refresh_game_status()
                    self._refresh_eboot_status()
                    self._refresh_trophy_status()
                else:
                    self.log("ERROR: RPCS3 exited but game files are not on disk.")
                    self.log("  RPCS3 likely showed a startup error and aborted.")
                    self.log("  Most common cause: VC++ 2015-2022 x64 Redistributable")
                    self.log("  is not installed (or was corrupted).")
                    self.log("  Fix:")
                    self.log("    1. Install: https://aka.ms/vs/17/release/VC_redist.x64.exe")
                    self.log("    2. Click 'Download & install' here again.")
                    self.s2_status.set("✗ Install failed — see log for fix")
                    return
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.s2_status.set("✗ Error during install")
            finally:
                # Always clean up the downloaded .pkg, even on failure
                try:
                    if pkg_path.exists():
                        pkg_path.unlink()
                        self.log(f"Deleted local .pkg: {pkg_path.name}")
                except Exception as e:
                    self.log(f"(could not delete {pkg_path.name}: {e})")
                self.busy = False
                self.s2_progress["value"] = 0
                # Re-enable the button after the worker finishes
                try: self.s2_button.configure(state=tk.NORMAL)
                except Exception: pass

        # Disable the button immediately so double-click doesn't queue 2 downloads
        try: self.s2_button.configure(state=tk.DISABLED)
        except Exception: pass
        threading.Thread(target=worker, daemon=True).start()

    def _download_updates(self):
        if not self._check_rpcs3(): return
        if self.busy:
            self.log("Already busy — ignoring duplicate click.")
            return
        eboot = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if not eboot.is_file():
            messagebox.showerror("Base game missing",
                "Install the base game first (step 3).\n"
                "Updates layer on top of the base install.")
            return
        if not messagebox.askyesno("Install game updates",
            f"This will download and install {len(GAME_UPDATE_URLS)} "
            f"official updates (1.01 → 1.05) in order.\n\n"
            "Each .pkg is downloaded to the installer's folder, installed\n"
            "via RPCS3, then deleted. Total download: ~hundreds of MB.\n\n"
            "Continue?"):
            return

        def worker():
            self.busy = True
            installer_folder = installer_dir()
            try:
                for idx, (label, url) in enumerate(GAME_UPDATE_URLS, start=1):
                    self.log("=" * 50)
                    self.log(f"[{idx}/{len(GAME_UPDATE_URLS)}] Update {label}")
                    pkg_name = f"NPUB31250_UPDATE_{label.replace('.', '_')}.pkg"
                    pkg_path = installer_folder / pkg_name

                    def cb(done, total, _label=label, _idx=idx):
                        if total > 0:
                            # Per-file progress combined with overall idx
                            self.sUP_progress["value"] = done * 100 / total
                            mb_done  = done  / (1024*1024)
                            mb_total = total / (1024*1024)
                            self.sUP_status.set(
                                f"[{_idx}/{len(GAME_UPDATE_URLS)}] {_label}: "
                                f"{mb_done:.1f} / {mb_total:.1f} MB")

                    if not download_with_progress(url, pkg_path, cb, self.log):
                        self.log(f"Download FAILED for update {label}, aborting.")
                        self.sUP_status.set(f"✗ Download failed at update {label}")
                        return

                    self.log(f"Installing update {label}...")
                    self.sUP_status.set(f"[{idx}/{len(GAME_UPDATE_URLS)}] {label}: installing...")
                    self.root.update_idletasks()
                    rc = self._rpcs3_installpkg(pkg_path)
                    if rc != 0:
                        self.log(f"Install of update {label} returned {rc}")
                        # try to clean up the .pkg before bailing
                        try:
                            if pkg_path.exists(): pkg_path.unlink()
                        except Exception: pass
                        self.sUP_status.set(f"✗ Install failed at update {label}")
                        return
                    self.log(f"Update {label} installed.")
                    # Delete the .pkg as soon as it's done
                    try:
                        if pkg_path.exists():
                            pkg_path.unlink()
                            self.log(f"Deleted local .pkg: {pkg_path.name}")
                    except Exception as e:
                        self.log(f"(could not delete {pkg_path.name}: {e})")
                self.sUP_status.set(f"✓ All {len(GAME_UPDATE_URLS)} updates installed")
                self.log("All updates installed successfully.")
                self._refresh_eboot_status()
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.sUP_status.set("✗ Error during updates install")
            finally:
                self.busy = False
                self.sUP_progress["value"] = 0
                try: self.sUP_button.configure(state=tk.NORMAL)
                except Exception: pass

        try: self.sUP_button.configure(state=tk.DISABLED)
        except Exception: pass
        threading.Thread(target=worker, daemon=True).start()

    def _pick_rap(self):
        p = filedialog.askopenfilename(title="Select .rap license file",
            filetypes=[("RAP files", "*.rap"), ("All files", "*.*")])
        if p:
            self.rap_source = Path(p)
            self.log(f".rap source selected: {p}")
            self._refresh_rap_status()

    def _install_rap(self):
        if not self._check_rpcs3(): return
        # Determine the .rap source: bundled takes priority, then user-selected
        source = None
        if BUNDLED_RAP.is_file():
            source = BUNDLED_RAP
        elif self.rap_source and self.rap_source.is_file():
            source = self.rap_source
        else:
            messagebox.showerror(".rap not available",
                "No bundled .rap found and no external file selected.\n\n"
                "Click 'Select .rap...' to pick one manually.\n\n"
                "The .rap file is your license file. Common sources:\n"
                "  - PSNStuff exdata folder\n"
                "  - Your own backup from PS3")
            return
        dst_dir = rap_exdata_dir(self.rpcs3_path)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / RAP_FILENAME
        shutil.copy2(source, dst)
        self.log(f"License installed: {source.name} -> {dst}")
        self._refresh_rap_status()

    def _make_trophy_mirror(self):
        if not self._check_rpcs3(): return
        src = game_tropdir(self.rpcs3_path) / TROPHY_COMM_BASE / "TROPHY.TRP"
        if not src.is_file():
            messagebox.showerror("Cannot create mirror",
                f"Source trophy file not found:\n{src}\n\n"
                "Install the game first (step 2).")
            return
        dst = game_tropdir(self.rpcs3_path) / TROPHY_COMM_MIRROR / "TROPHY.TRP"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        self.log(f"Trophy mirror created: {dst}")
        self._refresh_trophy_status()

    def _install_eboot(self):
        if not self._check_rpcs3(): return
        if not PATCHED_EBOOT.exists():
            messagebox.showerror("Missing patched EBOOT",
                f"data/EBOOT.BIN not found in installer.\n"
                f"Expected at: {PATCHED_EBOOT}")
            return
        dst = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        if not dst.parent.exists():
            messagebox.showerror("Game not installed",
                "Game directory missing. Install the game first (step 3).")
            return
        bak = dst.with_suffix(".BIN.original")
        if dst.exists() and not bak.exists():
            shutil.copy2(dst, bak)
            self.log(f"Backup created: {bak.name}")
        # Plain file copy, no subprocess — no _MEI* DLL hazard here.
        shutil.copy2(PATCHED_EBOOT, dst)
        self.log(f"Installed patched EBOOT: {dst}")
        self._refresh_eboot_status()

    def _restore_eboot(self):
        if not self._check_rpcs3(): return
        dst = game_usrdir(self.rpcs3_path) / "EBOOT.BIN"
        bak = dst.with_suffix(".BIN.original")
        if not bak.exists():
            messagebox.showerror("No backup",
                f"No backup found at {bak}. Nothing to restore.")
            return
        shutil.copy2(bak, dst)
        self.log(f"Restored vanilla EBOOT from {bak.name}")
        self._refresh_eboot_status()

    def _preview_config(self):
        win = tk.Toplevel(self.root)
        win.title("Config preview — per-game only")
        win.geometry("600x500")
        tk.Label(win, text=f"This is the content that will be written to:\n"
                            f"config/custom_configs/config_{GAME_TITLE_ID}.yml\n"
                            f"(per-game file; does NOT touch your global config.yml)",
                 anchor="w", justify="left").pack(fill=tk.X, padx=10, pady=10)
        txt = scrolledtext.ScrolledText(win, wrap=tk.WORD)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        txt.insert("1.0", RECOMMENDED_CFG_CONTENT)
        txt.configure(state=tk.DISABLED)
        tk.Button(win, text="Close", command=win.destroy).pack(pady=8)

    def _install_config(self):
        if not self._check_rpcs3(): return
        dst_dir = custom_config_dir(self.rpcs3_path)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"config_{GAME_TITLE_ID}.yml"
        if dst.exists():
            if not messagebox.askyesno("Overwrite existing config?",
                f"A config for this game already exists at:\n{dst}\n\n"
                "Overwrite it with the recommended config?"):
                return
            bak = dst.with_suffix(".yml.bak")
            shutil.copy2(dst, bak)
            self.log(f"Existing config backed up to {bak.name}")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(RECOMMENDED_CFG_CONTENT)
        self.log(f"Custom config written: {dst}")
        self._refresh_config_status()

    # -- RPCN account ---------------------------------------------------- #

    def _open_rpcn_signup(self):
        """Create a new RPCN account via the binary protocol — no web
        browser needed (the old rpcn.rpcs3.net/register page is gone)."""
        if not self._check_rpcs3():
            return

        win = tk.Toplevel(self.root)
        win.title("Create RPCN account")
        win.geometry("480x380")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="Create new RPCN account",
                 font=("TkDefaultFont", 13, "bold")).pack(pady=(15, 5))
        tk.Label(win,
                 text="Choose a username (NPID), password, and enter your email.\n"
                      "After creation, RPCN will send a token to your email.\n"
                      "Use that token in 'I have an account' to finish setup.",
                 justify="left").pack(pady=(0, 10), padx=15, anchor="w")

        form = tk.Frame(win)
        form.pack(padx=15, fill=tk.X)

        tk.Label(form, text="NPID (username):", anchor="e", width=14
                ).grid(row=0, column=0, sticky="e", pady=4)
        npid_var = tk.StringVar()
        tk.Entry(form, textvariable=npid_var, width=30).grid(
            row=0, column=1, pady=4, sticky="ew")

        tk.Label(form, text="Password:", anchor="e", width=14
                ).grid(row=1, column=0, sticky="e", pady=4)
        pwd_var = tk.StringVar()
        tk.Entry(form, textvariable=pwd_var, show="*", width=30).grid(
            row=1, column=1, pady=4, sticky="ew")

        tk.Label(form, text="Confirm password:", anchor="e", width=14
                ).grid(row=2, column=0, sticky="e", pady=4)
        pwd2_var = tk.StringVar()
        tk.Entry(form, textvariable=pwd2_var, show="*", width=30).grid(
            row=2, column=1, pady=4, sticky="ew")

        tk.Label(form, text="Email:", anchor="e", width=14
                ).grid(row=3, column=0, sticky="e", pady=4)
        email_var = tk.StringVar()
        tk.Entry(form, textvariable=email_var, width=30).grid(
            row=3, column=1, pady=4, sticky="ew")

        form.grid_columnconfigure(1, weight=1)

        status_var = tk.StringVar()
        tk.Label(win, textvariable=status_var, wraplength=400,
                 justify="left").pack(padx=15, pady=(10, 0), anchor="w")

        def on_create():
            n = npid_var.get().strip()
            p = pwd_var.get()
            p2 = pwd2_var.get()
            e = email_var.get().strip()

            if not n:
                messagebox.showerror("Missing NPID", "Username is required.", parent=win)
                return
            if len(n) < 3 or len(n) > 16:
                messagebox.showerror("Invalid NPID",
                    "Username must be 3-16 characters.", parent=win)
                return
            if not p:
                messagebox.showerror("Missing password", "Password is required.", parent=win)
                return
            if p != p2:
                messagebox.showerror("Password mismatch",
                    "Passwords do not match.", parent=win)
                return
            if not e or "@" not in e:
                messagebox.showerror("Missing email",
                    "A valid email is required to receive the token.", parent=win)
                return

            try:
                pw_hash = hash_rpcn_password(p)
            except RuntimeError as ex:
                messagebox.showerror("Hash error", str(ex), parent=win)
                return

            status_var.set("Creating account...")
            win.update_idletasks()

            self.log(f"Creating RPCN account: NPID='{n}', email='{e}'...")

            def _worker():
                ok, msg = create_rpcn_account(
                    RPCN_DEFAULT_HOST, RPCN_DEFAULT_PORT,
                    n, pw_hash, e)
                self.log(f"  {msg}")
                if ok:
                    # Save credentials to rpcn.yml (token empty — user
                    # will enter it from the email they receive)
                    try:
                        write_rpcn_yml(
                            rpcn_yml_path(self.rpcs3_path),
                            RPCN_DEFAULT_HOST, n, pw_hash, "")
                        self.log("  Credentials saved to rpcn.yml (token pending)")
                    except Exception as ex:
                        self.log(f"  WARNING: could not save rpcn.yml: {ex}")
                    self._refresh_rpcn_status()
                    win.after(0, lambda: [
                        status_var.set(""),
                        messagebox.showinfo("Account created", (
                            f"{msg}\n\n"
                            "Check your email for the RPCN token, then\n"
                            "click 'I have an account' and enter the token."),
                            parent=win),
                        win.destroy()])
                else:
                    win.after(0, lambda: [
                        status_var.set(""),
                        messagebox.showerror("Creation failed", msg, parent=win)])

            threading.Thread(target=_worker, daemon=True).start()

        btns = tk.Frame(win)
        btns.pack(pady=15)
        tk.Button(btns, text="Create account", command=on_create,
                  width=14).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="Cancel", command=win.destroy,
                  width=10).pack(side=tk.LEFT, padx=6)

    def _test_rpcn(self):
        """Perform a real RPCN login attempt using credentials from rpcn.yml.
        Speaks the actual RPCN binary protocol and reports the server's
        response (InvalidUsername, InvalidPassword, InvalidToken, etc.)."""
        if not self.rpcs3_path:
            messagebox.showerror("RPCS3 not selected",
                "Select your RPCS3 binary first.")
            return
        cfg = parse_rpcn_yml(rpcn_yml_path(self.rpcs3_path))
        host  = cfg.get("Host", RPCN_DEFAULT_HOST) or RPCN_DEFAULT_HOST
        npid  = cfg.get("NPID", "")
        pwd   = cfg.get("Password", "")
        token = cfg.get("Token", "")

        # ---- credential validation before hitting the server ----
        problems = []
        if not npid:
            problems.append("NPID (username) is missing")
        if not pwd or len(pwd) < 32:
            problems.append("Password is missing or not hashed")
        if not token:
            problems.append("Token is missing (check your registration email)")
        if problems:
            detail = "\n".join(f"  - {p}" for p in problems)
            self.log("RPCN credential check FAILED:")
            self.log(detail)
            self.log("  Fix: click 'I have an account' and fill ALL fields.")
            self.s7_status.set(f"✗ Incomplete credentials ({len(problems)} issue(s))")
            messagebox.showerror("RPCN credentials incomplete",
                f"Login will fail because:\n\n{detail}\n\n"
                "Click 'I have an account' to enter the missing fields.\n"
                "The token is in the email RPCN sent when you registered.")
            return

        self.log(f"Testing RPCN login: NPID='{npid}', host={host}:{RPCN_DEFAULT_PORT}...")
        self.s7_status.set(f"Logging in to {host}...")
        self.root.update_idletasks()

        def worker():
            ok, msg = test_rpcn_login(host, RPCN_DEFAULT_PORT,
                                      npid, pwd, token)
            self.log(f"  {msg}")
            if ok:
                self.root.after(0, lambda: self.s7_status.set(
                    f"✓ {msg} ('{npid}')"))
            else:
                self.log("  Fix the issue and try again.")
                self.root.after(0, lambda: self.s7_status.set(f"✗ {msg}"))
        threading.Thread(target=worker, daemon=True).start()

    def _enter_rpcn_credentials(self):
        if not self._check_rpcs3(): return

        # Modal dialog for credentials
        win = tk.Toplevel(self.root)
        win.title("RPCN credentials")
        win.geometry("540x480")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="RPCN account setup",
                 font=("TkDefaultFont", 13, "bold")).pack(pady=(15, 5))
        tk.Label(win,
                 text="1. Register at rpcn.rpcs3.net/register (if you haven't)\n"
                      "2. Check your email for 'Your token for RPCN'\n"
                      "3. Enter all fields below, including the token from the email\n"
                      "4. Password is hashed locally (PBKDF2) — never stored as plaintext",
                 justify="left"
                 ).pack(pady=(0, 10), padx=15, anchor="w")

        form = tk.Frame(win)
        form.pack(padx=15, fill=tk.X)

        # Row 0: Host
        tk.Label(form, text="Host:", anchor="e", width=14
                ).grid(row=0, column=0, sticky="e", pady=4)
        host_var = tk.StringVar(value=RPCN_DEFAULT_HOST)
        tk.Entry(form, textvariable=host_var, width=36).grid(row=0, column=1, pady=4, sticky="ew")

        # Row 1: NPID
        tk.Label(form, text="NPID (username):", anchor="e", width=14
                ).grid(row=1, column=0, sticky="e", pady=4)
        npid_var = tk.StringVar()
        tk.Entry(form, textvariable=npid_var, width=36).grid(row=1, column=1, pady=4, sticky="ew")

        # Row 2: Password
        tk.Label(form, text="Password:", anchor="e", width=14
                ).grid(row=2, column=0, sticky="e", pady=4)
        pwd_var = tk.StringVar()
        pwd_entry = tk.Entry(form, textvariable=pwd_var, show="*", width=36)
        pwd_entry.grid(row=2, column=1, pady=4, sticky="ew")
        show_var = tk.BooleanVar(value=False)
        def toggle_show():
            pwd_entry.configure(show="" if show_var.get() else "*")
        tk.Checkbutton(form, text="Show", variable=show_var,
                       command=toggle_show
                       ).grid(row=2, column=2, padx=4)

        # Row 3: Token (THE KEY FIELD — from registration email)
        tk.Label(form, text="Token (email):", anchor="e", width=14
                ).grid(row=3, column=0, sticky="e", pady=4)
        token_var = tk.StringVar()
        token_entry = tk.Entry(form, textvariable=token_var, width=36)
        token_entry.grid(row=3, column=1, pady=4, sticky="ew")
        tk.Label(form, text="required",
                 font=("TkDefaultFont", 8)).grid(row=3, column=2, padx=4)

        form.grid_columnconfigure(1, weight=1)

        # Token help note
        token_note = tk.Frame(win)
        token_note.pack(fill=tk.X, padx=15, pady=(5, 0))
        tk.Label(token_note,
                 text="The token is in the email RPCN sent when you registered.\n"
                      "Subject: 'Your token for RPCN'. If you didn't get it,\n"
                      "create a new account or check spam.",
                 justify="left",
                 font=("TkDefaultFont", 9, "italic")).pack(anchor="w")

        # Pre-fill from existing rpcn.yml if any
        existing = parse_rpcn_yml(rpcn_yml_path(self.rpcs3_path))
        if existing.get("NPID"):   npid_var.set(existing["NPID"])
        if existing.get("Host"):   host_var.set(existing["Host"])
        if existing.get("Token"):  token_var.set(existing["Token"])

        def on_save():
            npid  = npid_var.get().strip()
            pwd   = pwd_var.get()
            host  = host_var.get().strip() or RPCN_DEFAULT_HOST
            token = token_var.get().strip()
            if not npid:
                messagebox.showerror("Missing NPID", "NPID (username) is required.", parent=win)
                return
            if not pwd:
                # If password field is empty but we already have a hash, keep it
                old_hash = existing.get("Password", "")
                if old_hash and len(old_hash) >= 64:
                    hashed = old_hash
                else:
                    messagebox.showerror("Missing password", "Password is required.", parent=win)
                    return
            else:
                try:
                    hashed = hash_rpcn_password(pwd)
                except RuntimeError as e:
                    messagebox.showerror("Hash error", str(e), parent=win)
                    return
            if not token:
                if not messagebox.askyesno("Token missing",
                    "The Token field is empty. Without a valid token,\n"
                    "RPCS3 will show 'RPCN Login Error: Invalid Token'.\n\n"
                    "The token was sent to your email when you registered.\n"
                    "Subject: 'Your token for RPCN'.\n\n"
                    "Save anyway without token?", parent=win):
                    return
            try:
                write_rpcn_yml(rpcn_yml_path(self.rpcs3_path), host, npid, hashed, token)
                self.log(f"rpcn.yml written: NPID='{npid}' Host='{host}' Token={'set' if token else 'EMPTY'}")
                self.log("  (password stored as PBKDF2 hash, never as plaintext)")
                if token:
                    self.log("  Token set — RPCS3 should login successfully on next boot.")
                else:
                    self.log("  WARNING: Token is empty — RPCS3 will fail to login.")
                    self.log("  Check your registration email for the token.")
                self._refresh_rpcn_status()
                win.destroy()
            except Exception as e:
                messagebox.showerror("Save failed", str(e), parent=win)

        def on_test():
            """Real RPCN login test from the dialog — verifies credentials
            against the live server."""
            h = host_var.get().strip() or RPCN_DEFAULT_HOST
            n = npid_var.get().strip()
            p = pwd_var.get()
            t = token_var.get().strip()

            # Check fields before hashing / connecting
            problems = []
            if not n:
                problems.append("NPID is empty")
            if not p and not (existing.get("Password", "") and len(existing.get("Password", "")) >= 64):
                problems.append("Password is empty")
            if not t:
                problems.append("Token is empty (check your registration email)")
            if problems:
                detail = "\n".join(f"  - {p}" for p in problems)
                win.after(0, lambda: messagebox.showerror(
                    "Credentials incomplete",
                    f"Login will fail because:\n\n{detail}\n\n"
                    "Fill ALL fields before testing.",
                    parent=win))
                return

            # Derive password hash: use entered text, or existing hash
            if p:
                try:
                    pw_hash = hash_rpcn_password(p)
                except RuntimeError as e:
                    win.after(0, lambda: messagebox.showerror(
                        "Hash error", str(e), parent=win))
                    return
            else:
                pw_hash = existing.get("Password", "")

            self.log(f"RPCN login test: {h}:{RPCN_DEFAULT_PORT} NPID='{n}'...")
            def _worker():
                ok, msg = test_rpcn_login(h, RPCN_DEFAULT_PORT,
                                          n, pw_hash, t)
                self.log(f"  {msg}")
                if ok:
                    win.after(0, lambda: messagebox.showinfo(
                        "Login OK", msg, parent=win))
                else:
                    win.after(0, lambda: messagebox.showerror(
                        "Login failed", msg, parent=win))
            threading.Thread(target=_worker, daemon=True).start()

        btns = tk.Frame(win)
        btns.pack(pady=15)
        tk.Button(btns, text="Save", command=on_save, width=12).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="Test connection", command=on_test, width=14).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="Cancel", command=win.destroy, width=12).pack(side=tk.LEFT, padx=6)


# ----------------------------------------------------------------------- #
# Main
# ----------------------------------------------------------------------- #

def main():
    root = tk.Tk()
    TekkenRevApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
