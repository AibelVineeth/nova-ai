#!/usr/bin/env python3
"""
Nova auto-setup & launcher
==========================
Downloads / installs everything Nova needs, then starts the app.

Usage:
    python setup_nova.py              # full install + run
    python setup_nova.py --no-run     # install only
    python setup_nova.py --core-only  # skip heavy optional ML packages
    python setup_nova.py --no-venv    # install into the current Python (not recommended)

Place this file next to nova.py (and optionally requirements.txt, yolov8n.pt).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NOVA_SCRIPT = APP_DIR / "nova.py"
REQUIREMENTS = APP_DIR / "requirements.txt"
VENV_DIR = APP_DIR / ".venv"
YOLO_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt"
YOLO_PATH = APP_DIR / "yolov8n.pt"

# Core packages — needed for a basic voice + GUI session
CORE_PACKAGES = [
    "SpeechRecognition>=3.10.0",
    "edge-tts>=6.1.0",
    "pygame>=2.5.0",
    "pystray>=0.19.0",
    "Pillow>=10.0.0",
    "psutil>=5.9.0",
    "PyAudio>=0.2.13",  # mic backend for SpeechRecognition on many systems
]

# Optional / feature packages — installed unless --core-only
OPTIONAL_PACKAGES = [
    "opencv-python>=4.8.0",
    "mediapipe>=0.10.0",
    "ultralytics>=8.0.0",
    "torch",  # large; pulled by ultralytics too, listed for clarity
    "selenium>=4.15.0",
    "webdriver-manager>=4.0.0",
    "sympy>=1.12",
]

# Windows-only helpers
WINDOWS_PACKAGES = [
    "pycaw>=20240210",
    "comtypes>=1.2.0",
    "pywinauto>=0.6.8",
]


def log(msg: str) -> None:
    print(f"[Nova setup] {msg}", flush=True)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log(" ".join(cmd))
    return subprocess.run(cmd, check=check)


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        sys.exit(
            f"Python 3.10+ is required. You have {sys.version.split()[0]}.\n"
            "Download from https://www.python.org/downloads/"
        )


def python_for_venv() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def create_venv(use_venv: bool) -> Path:
    """Return path to the Python interpreter we should use for installs + launch."""
    if not use_venv:
        log("Using current Python (no venv).")
        return Path(sys.executable)

    if VENV_DIR.exists():
        py = python_for_venv()
        if py.exists():
            log(f"Reusing existing virtualenv at {VENV_DIR}")
            return py
        log("Broken .venv — recreating…")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    log(f"Creating virtual environment in {VENV_DIR}")
    run([sys.executable, "-m", "venv", str(VENV_DIR)])
    py = python_for_venv()
    if not py.exists():
        sys.exit(f"Virtualenv created but interpreter not found: {py}")
    return py


def pip_install(py: Path, packages: list[str]) -> None:
    if not packages:
        return
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    # Install in batches so a single failure is easier to spot
    for pkg in packages:
        try:
            run([str(py), "-m", "pip", "install", pkg])
        except subprocess.CalledProcessError as e:
            log(f"WARNING: failed to install {pkg}: {e}")
            log("  Nova will still start; that feature may be unavailable.")


def install_from_requirements(py: Path) -> None:
    if REQUIREMENTS.exists():
        log(f"Installing from {REQUIREMENTS.name}")
        try:
            run([str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
            return
        except subprocess.CalledProcessError:
            log("requirements.txt install had errors — falling back to package lists.")
    packages = list(CORE_PACKAGES) + list(OPTIONAL_PACKAGES)
    if platform.system() == "Windows":
        packages += WINDOWS_PACKAGES
    pip_install(py, packages)


def install_core_only(py: Path) -> None:
    packages = list(CORE_PACKAGES)
    if platform.system() == "Windows":
        packages += WINDOWS_PACKAGES
    pip_install(py, packages)


def download_yolo_if_needed() -> None:
    if YOLO_PATH.exists() and YOLO_PATH.stat().st_size > 1_000_000:
        log(f"Vision model already present: {YOLO_PATH.name}")
        return
    log(f"Downloading YOLO model from {YOLO_URL}")
    try:
        tmp = YOLO_PATH.with_suffix(".pt.part")
        urllib.request.urlretrieve(YOLO_URL, tmp)
        tmp.replace(YOLO_PATH)
        log(f"Saved {YOLO_PATH.name} ({YOLO_PATH.stat().st_size // 1024} KB)")
    except Exception as e:
        log(f"Could not download yolov8n.pt: {e}")
        log("  Vision features will try to download it the first time you use the camera.")


def ensure_nova_present() -> None:
    if not NOVA_SCRIPT.exists():
        sys.exit(
            f"nova.py not found next to this script.\n"
            f"Expected: {NOVA_SCRIPT}\n"
            "Download nova.py into the same folder as setup_nova.py."
        )


def launch_nova(py: Path) -> None:
    log("Starting Nova…")
    # Replace this process so Ctrl+C / exit codes behave normally
    os.execv(str(py), [str(py), str(NOVA_SCRIPT)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Nova dependencies and launch the app.")
    parser.add_argument("--no-run", action="store_true", help="Install packages only; do not start Nova")
    parser.add_argument("--core-only", action="store_true", help="Skip heavy optional packages (torch, mediapipe, …)")
    parser.add_argument("--no-venv", action="store_true", help="Do not create/use .venv")
    args = parser.parse_args()

    print("=" * 56)
    print("  Nova Assistant — automatic setup")
    print("=" * 56)

    ensure_python_version()
    ensure_nova_present()

    py = create_venv(use_venv=not args.no_venv)

    log("Upgrading pip tooling…")
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], check=False)

    if args.core_only:
        log("Installing CORE packages only…")
        install_core_only(py)
    else:
        log("Installing core + optional packages (this can take several minutes)…")
        install_from_requirements(py)

    download_yolo_if_needed()

    log("Setup finished.")
    if args.no_run:
        log("Skipping launch (--no-run). Run:  python nova.py   or use the venv python.")
        return

    launch_nova(py)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Nova setup] Cancelled.")
        sys.exit(130)
