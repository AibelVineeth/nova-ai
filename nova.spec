# nova.spec
# PyInstaller build spec for Nova.
#
# Run with:  pyinstaller nova.spec
# (NOT "pyinstaller nova.py" directly - the spec file has the extra
#  settings these ML libraries need to package correctly)
#
# Produces a FOLDER (dist/Nova/) rather than a single .exe on purpose -
# for an installer-based app this starts faster and is easier to debug
# than a --onefile build, which re-extracts everything on every launch.

import os
import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# ---- Libraries known to need extra help bundling their data/hidden imports ----
# torch, mediapipe, and ultralytics all ship non-Python data files (model
# configs, compiled extensions) that PyInstaller's static analysis can miss.
# collect_all() grabs everything for a package: python modules, data files,
# and binaries, which is the most reliable (if heavy-handed) fix.
datas = []
binaries = []
hiddenimports = []

for pkg in [
    "torch",
    "mediapipe",
    "ultralytics",
    "sentence_transformers",
    "speech_recognition",
    "edge_tts",
    "selenium",
    "webdriver_manager",
    "pycaw",
    "comtypes",
    "pystray",
    "PIL",
    "pyaudio",
    "psutil",
    "sympy",  # Productive Mode's maths solver
    "pywinauto",  # optional - only used for the Phone Link "press Call" automation.
                  # Not required: if it isn't installed, that one feature just
                  # falls back to "I put the number in Phone Link, press Call."
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"[nova.spec] Warning: couldn't collect_all for {pkg}: {e}")

# Bundled resources, loaded at runtime via resource_path() in nova.py:
#  - nova_icon.ico : tray/window icon
#  - yolov8n.pt    : YOLO vision model (otherwise it tries to download at runtime)
# Note: "whisper" is intentionally not collected - USE_LOCAL_WHISPER is False.
# Add "whisper" back to the package list above if you enable it.
# upx is disabled: UPX is known to corrupt torch/other large DLLs.
datas += [("nova_icon.ico", "."), ("yolov8n.pt", "."), ("kochi_intelligence_map.html", ".")]

# Phone control (ADB backend): if you've put Google's "platform-tools" folder
# (adb.exe + its DLLs) next to this spec file, bundle it so ADB works on a
# machine that doesn't have adb on its PATH. Entirely optional - if the
# folder isn't here, Nova still works, it just falls back to Phone Link /
# a system-wide adb install for phone calls.
if os.path.isdir("platform-tools"):
    datas += [("platform-tools", "platform-tools")]
else:
    print("[nova.spec] Note: no platform-tools/ folder found - ADB phone "
          "control will rely on adb being on the target machine's PATH.")

a = Analysis(
    ["nova.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Nova",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no black terminal window.
                     # All print() output goes to %APPDATA%/Nova/nova_log.txt instead.
    icon="nova_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Nova",
)
