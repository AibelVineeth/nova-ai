#!/usr/bin/env python3
"""
Nova auto-setup & launcher
==========================
Installs everything Nova needs, verifies imports, then starts the app.

Usage:
    python setup_nova.py              # full install + run
    python setup_nova.py --no-run     # install only
    python setup_nova.py --core-only  # skip heavy optional ML packages
    python setup_nova.py --no-venv    # install into the current Python

Place this file next to nova.py (and optionally requirements.txt, yolov8n.pt).

Note on Ollama:
    Nova does NOT `import ollama`. It talks to the Ollama desktop app over
    HTTP (http://localhost:11434). Install Ollama separately from
    https://ollama.com then run:  ollama pull llama3.2-vision
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

# Core packages — required for voice + GUI
CORE_PACKAGES = [
    "SpeechRecognition>=3.10.0",
    "edge-tts>=6.1.0",
    "pygame>=2.5.0",
    "pystray>=0.19.0",
    "Pillow>=10.0.0",
    "psutil>=5.9.0",
    "PyAudio>=0.2.13",
]

# Optional feature packages
OPTIONAL_PACKAGES = [
    "opencv-python>=4.8.0",
    "mediapipe>=0.10.0",
    "ultralytics>=8.0.0",
    "torch",
    "selenium>=4.15.0",
    "webdriver-manager>=4.0.0",
    "sympy>=1.12",
    "sentence-transformers>=2.2.0",  # semantic memory search
]

WINDOWS_PACKAGES = [
    "pycaw>=20240210",
    "comtypes>=1.2.0",
    "pywinauto>=0.6.8",
]

# Modules that MUST import successfully before we launch Nova
REQUIRED_IMPORT_CHECKS = [
    ("pygame", "pygame"),
    ("speech_recognition", "SpeechRecognition"),
    ("edge_tts", "edge-tts"),
    ("PIL", "Pillow"),
    ("pystray", "pystray"),
    ("psutil", "psutil"),
]


def log(msg: str) -> None:
    print(f"[Nova setup] {msg}", flush=True)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log(" ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check)


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        sys.exit(
            f"Python 3.10+ is required. You have {sys.version.split()[0]}.\n"
            "Download from https://www.python.org/downloads/\n"
            "On Windows, check 'Add Python to PATH' during install."
        )


def python_for_venv() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def create_venv(use_venv: bool) -> Path:
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


def pip_install_one(py: Path, package: str) -> bool:
    """Install one package. Returns True on success."""
    try:
        run([str(py), "-m", "pip", "install", "--upgrade", package])
        return True
    except subprocess.CalledProcessError as e:
        log(f"WARNING: failed to install {package}: {e}")
        return False


def pip_install_list(py: Path, packages: list[str]) -> None:
    for pkg in packages:
        ok = pip_install_one(py, pkg)
        if not ok and pkg.lower().startswith("pygame"):
            log("Retrying pygame with --only-binary=:all: …")
            try:
                run([str(py), "-m", "pip", "install", "--only-binary=:all:", "pygame"])
            except subprocess.CalledProcessError:
                log("pygame still failed. Try manually:  pip install pygame")
        if not ok and pkg.lower().startswith("pyaudio"):
            log("PyAudio often needs a prebuilt wheel on Windows.")
            pip_install_one(py, "PyAudio")
            log("  If mic still fails later: pip install pipwin && pipwin install pyaudio")


def install_from_requirements(py: Path) -> None:
    if REQUIREMENTS.exists():
        log(f"Installing from {REQUIREMENTS.name}")
        try:
            run([str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
            for pkg in CORE_PACKAGES:
                name = pkg.split(">=")[0].split("==")[0].strip()
                mod = {
                    "SpeechRecognition": "speech_recognition",
                    "edge-tts": "edge_tts",
                    "Pillow": "PIL",
                    "PyAudio": "pyaudio",
                }.get(name, name.lower().replace("-", "_"))
                r = subprocess.run(
                    [str(py), "-c", f"import {mod}"],
                    capture_output=True,
                )
                if r.returncode != 0:
                    log(f"Re-installing missing core package: {pkg}")
                    pip_install_one(py, pkg)
            return
        except subprocess.CalledProcessError:
            log("requirements.txt had errors — installing package lists instead.")

    packages = list(CORE_PACKAGES) + list(OPTIONAL_PACKAGES)
    if platform.system() == "Windows":
        packages += WINDOWS_PACKAGES
    pip_install_list(py, packages)


def install_core_only(py: Path) -> None:
    packages = list(CORE_PACKAGES)
    if platform.system() == "Windows":
        packages += WINDOWS_PACKAGES
    pip_install_list(py, packages)


def verify_required_imports(py: Path) -> list[str]:
    """Return list of human-readable failures. Empty = all good."""
    failures = []
    for mod, pip_name in REQUIRED_IMPORT_CHECKS:
        r = subprocess.run(
            [str(py), "-c", f"import {mod}"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            brief = err[-1] if err else "import failed"
            failures.append(f"{mod}  (pip package: {pip_name}) — {brief}")
            log(f"IMPORT FAIL: {mod} — trying reinstall of {pip_name}…")
            pip_install_one(py, pip_name)
            r2 = subprocess.run(
                [str(py), "-c", f"import {mod}"],
                capture_output=True,
                text=True,
            )
            if r2.returncode == 0:
                log(f"IMPORT OK after reinstall: {mod}")
                failures.pop()
            else:
                log(f"IMPORT STILL FAIL: {mod}")
        else:
            log(f"IMPORT OK: {mod}")

    r = subprocess.run(
        [str(py), "-c", "import tkinter"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        failures.append(
            "tkinter — reinstall Python from python.org and enable tcl/tk"
        )
        log("IMPORT FAIL: tkinter (GUI will not start)")
    else:
        log("IMPORT OK: tkinter")

    return failures


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
        log("  Vision can download it the first time you use the camera.")


def ensure_nova_present() -> None:
    if not NOVA_SCRIPT.exists():
        sys.exit(
            f"nova.py not found next to this script.\n"
            f"Expected: {NOVA_SCRIPT}\n"
            "Unzip Nova-Bundle.zip fully so nova.py sits next to setup_nova.py."
        )


def print_ollama_hint() -> None:
    log("")
    log("Optional — local AI brain (screen help / 'ask AI'):")
    log("  Nova does NOT need 'pip install ollama'.")
    log("  Install the Ollama app:  https://ollama.com")
    log("  Then in a terminal:     ollama pull llama3.2-vision")
    log("  Keep Ollama running in the background while using screen-AI features.")
    log("")


def launch_nova(py: Path) -> None:
    log(f"Starting Nova with: {py}")
    log(f"  script: {NOVA_SCRIPT}")
    result = subprocess.run([str(py), str(NOVA_SCRIPT)], cwd=str(APP_DIR))
    sys.exit(result.returncode)


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

    log("Verifying required imports…")
    failures = verify_required_imports(py)

    download_yolo_if_needed()
    print_ollama_hint()

    if failures:
        print()
        print("*" * 56)
        print("  SETUP INCOMPLETE — these imports still fail:")
        for f in failures:
            print(f"   • {f}")
        print()
        print("  Common fixes (run inside the same folder):")
        print(f'    "{py}" -m pip install pygame')
        print(f'    "{py}" -m pip install SpeechRecognition edge-tts Pillow pystray psutil')
        print(f'    "{py}" -m pip install PyAudio')
        print()
        print("  Then run again:  python setup_nova.py")
        print("*" * 56)
        sys.exit(1)

    log("Setup finished — all required imports OK.")
    if args.no_run:
        log("Skipping launch (--no-run).")
        log(f'To start later:  "{py}" nova.py')
        return

    launch_nova(py)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Nova setup] Cancelled.")
        sys.exit(130)
