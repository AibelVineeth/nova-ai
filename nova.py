"""
Nova Assistant - single-file build.

Everything (conversation, memory, app control, volume, browser automation,
and camera vision) lives in this one file.

Requirements (core, needed every run):
    Installed automatically on first launch (SpeechRecognition, edge-tts,
    pygame, pystray, Pillow, psutil, PyAudio) - no manual pip install step
    needed, whether you run this via setup_nova.py or launch nova.py
    directly. See _ensure_core_packages() below.

Optional, only needed if you use these features (imported lazily, so the
app starts instantly even if these aren't installed - you'll just get a
clear spoken error the moment you try to use that specific feature):
    pip install opencv-python mediapipe ultralytics      (camera vision)
    pip install selenium webdriver-manager               (browser search/control, Productive Mode's redirect)
    pip install pycaw comtypes                           (Windows volume control)
    pip install sympy                                    (Productive Mode's maths solver)

Startup is intentionally fast: heavy libraries (mediapipe, ultralytics/
PyTorch, selenium) are NOT imported until the moment you actually use
vision or browser features. Importing PyTorch alone can take 5-15 seconds,
so deferring it is most of what makes this feel instant on launch.

Languages: Nova understands and replies in English, Hindi, and Malayalam
(see the LANGUAGE panel, or say "speak Hindi" / "reply in Malayalam" /
"auto language"). No extra packages needed - speech recognition already
supports these locales, edge-tts already ships neural voices for them
(hi-IN-MadhurNeural, ml-IN-MidhunNeural), and translation between them and
English uses a plain HTTPS call (urllib, already in the standard library).
This does mean Hindi/Malayalam input needs an internet connection for the
translation step, same as the speech recognition already required - if
translation is briefly unreachable, Nova says so and falls back to English
rather than going silent.
"""

import sys
sys.unraisablehook = lambda unraisable: None  # silences harmless pycaw COM cleanup warnings on interpreter exit

import threading
import time
import random
import math
import subprocess
import platform
import os
import re
import json
import asyncio
import tempfile
import webbrowser
import base64
import urllib.request
import xml.etree.ElementTree as ET
import html as _html_module
import urllib.parse
from collections import deque

# ---------------------------------------------------------------------
# Self-installing core dependencies
# ---------------------------------------------------------------------
# setup_nova.py (the recommended way to launch Nova) already installs all
# of this - but nova.py can also end up being run directly (double-clicked,
# launched from an IDE, a shortcut someone made themselves, etc.), and
# until now that path just crashed on the first missing import with a bare
# ModuleNotFoundError. This makes nova.py self-sufficient either way: on
# startup it checks these core packages, pip-installs anything missing
# into the current Python environment, then imports them - pygame
# included. Optional/heavy packages (opencv, mediapipe, torch, selenium,
# sympy, pycaw...) are untouched here; they stay lazily imported exactly
# where they're used, same as before, so startup is still fast.
_CORE_PACKAGES = [
    ("speech_recognition", "SpeechRecognition>=3.10.0"),
    ("edge_tts", "edge-tts>=6.1.0"),
    ("pygame", "pygame>=2.5.0"),
    ("pystray", "pystray>=0.19.0"),
    ("PIL", "Pillow>=10.0.0"),
    ("psutil", "psutil>=5.9.0"),
    ("pyaudio", "PyAudio>=0.2.13"),
]


def _ensure_core_packages():
    import importlib

    # A packaged Nova.exe already contains everything it needs, and there
    # sys.executable is Nova.exe itself - "sys.executable -m pip" would
    # relaunch the app rather than run pip. Nothing to do.
    if getattr(sys, "frozen", False):
        return

    # Packages that failed to install on an earlier launch are remembered
    # and skipped, so something uninstallable on this machine (PyAudio has
    # no prebuilt wheel for some Python versions) doesn't cost a slow,
    # failing pip run on EVERY startup. Delete the marker file to retry.
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nova_install_failed")
    try:
        with open(marker, encoding="utf-8") as f:
            known_failed = {line.strip() for line in f if line.strip()}
    except OSError:
        known_failed = set()

    missing, skipped = [], []
    for module_name, pip_name in _CORE_PACKAGES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            (skipped if module_name in known_failed else missing).append((module_name, pip_name))
    if skipped:
        print("[Nova] Skipping auto-install (failed on an earlier launch): "
              + ", ".join(p for _, p in skipped)
              + f"  - delete {os.path.basename(marker)} to retry.")
    if not missing:
        return

    print("[Nova] First run (or a package is missing) - installing: "
          + ", ".join(p for _, p in missing))
    for module_name, pip_name in missing:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pip_name])
        except Exception as e:
            print(f"[Nova] Could not install {pip_name}: {e}")
            if module_name == "pygame":
                # pygame occasionally fails to build from source on a bare
                # pip - forcing a prebuilt wheel usually fixes it.
                print("[Nova] Retrying pygame with a prebuilt wheel...")
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install",
                                           "--only-binary=:all:", "pygame"])
                except Exception as e2:
                    print(f"[Nova] pygame still failed: {e2}")

    still_missing = []
    for module_name, pip_name in missing:
        try:
            importlib.import_module(module_name)
        except ImportError:
            still_missing.append(pip_name)
    if still_missing:
        try:
            failed_modules = {m for m, p in missing if p in still_missing}
            with open(marker, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(known_failed | failed_modules)) + "\n")
        except OSError:
            pass  # read-only folder - worst case it just retries next launch
        print("=" * 56)
        print("[Nova] Some packages could not be installed automatically:")
        for p in still_missing:
            print("   -", p)
        # Quoted: an unquoted ">=" in cmd/PowerShell/bash is a redirect, so a
        # copy-pasted hint would silently create a file instead of installing.
        print(f'[Nova] Try manually:  "{sys.executable}" -m pip install '
              + " ".join(f'"{p}"' for p in still_missing))
        print("[Nova] Continuing anyway - the matching feature(s) may not work.")
        print("=" * 56)
    else:
        print("[Nova] All core packages are ready.")


_ensure_core_packages()

import speech_recognition as sr
import edge_tts
import pygame
import io
import wave
import pystray
from PIL import Image, ImageTk
import psutil

# ---------- Configuration ----------
LISTEN_PHRASE_LIMIT = 6
AMBIENT_CALIBRATE_SECONDS = 1.0
MAX_MEMORY = 8
OS_NAME = platform.system().lower()
USER = os.environ.get("USERNAME") or os.environ.get("USER") or ""
TTS_VOICE = "en-GB-RyanNeural"

if OS_NAME.startswith("windows"):
    # Without this, Windows silently scales the whole Tkinter window on any
    # display over 100% scaling, which throws off every screen-pixel
    # coordinate Nova relies on - both the existing full-screen capture
    # (capture_screen_b64) and the new Screen Picker's drag-to-select box
    # would land offset from what's actually on screen. Best-effort: two
    # different Windows APIs across OS versions, either working is fine.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Windows 8.1+
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # older fallback
        except Exception as e:
            print("Couldn't set DPI awareness (screen coordinates may be slightly off):", e)


def get_app_dir():
    """
    Directory to store/read files next to the app. Using __file__ alone
    breaks once packaged with PyInstaller - it points inside a temp
    extraction path, not the actual install folder, which would silently
    make persistent memory not actually persist. sys.frozen is the
    standard PyInstaller flag for 'am I running as a packaged .exe'.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(filename):
    """Find a bundled resource (like the tray icon) whether running as a
    plain script or as a packaged PyInstaller app."""
    base = getattr(sys, "_MEIPASS", get_app_dir())
    return os.path.join(base, filename)


APP_DIR = get_app_dir()
# Memory + log live in %APPDATA%\Nova so they survive reinstalls/upgrades and
# work even if the app is installed somewhere read-only (e.g. Program Files).
DATA_DIR = os.path.join(os.environ.get("APPDATA") or APP_DIR, "Nova")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = APP_DIR
MEMORY_FILE = os.path.join(DATA_DIR, "nova_memory.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "nova_archive.jsonl")  # append-only, never trimmed - the permanent record
LOG_FILE = os.path.join(DATA_DIR, "nova_log.txt")
ICON_FILE = resource_path("nova_icon.ico")
KOCHI_MAP_FILE = resource_path("kochi_intelligence_map.html")  # bundled interactive Kochi map
MAX_HISTORY = 600  # the "hot" working-memory window used for quick context/semantic search - see ARCHIVE_FILE for the untrimmed permanent copy


# =====================================================================
# ---------- Sound effects (startup chime, mode-change beep) ----------
# =====================================================================
def _play_beep(frequency=880, duration_ms=200):
    """Best-effort short beep. Uses winsound on Windows; silently no-ops
    elsewhere rather than crashing (no extra install needed either way)."""
    try:
        import winsound
        winsound.Beep(frequency, duration_ms)
    except Exception as e:
        print("Beep unavailable (non-Windows, or no audio device):", e)


def play_startup_chime():
    """A short rising chime played once when Nova launches."""
    for freq in (660, 880, 1100):
        _play_beep(freq, 130)


def play_mode_revert_beep():
    """A short descending beep played when leaving camera mode, so it's
    audibly distinct from the startup chime."""
    for freq in (500, 320):
        _play_beep(freq, 130)


def ui_beep(frequency=850, duration_ms=45):
    """Short UI feedback click (button presses, toggles, etc). Always
    dispatched on its own thread - winsound.Beep blocks for its full
    duration, and this gets called from inside Tkinter click handlers on
    the main thread, which must never block (that's exactly the kind of
    thing that causes a window to go "Not Responding")."""
    threading.Thread(target=_play_beep, args=(frequency, duration_ms), daemon=True).start()


def ui_beep_sequence(freqs, duration_ms=90):
    """Several beeps played in order on one background thread - a quick
    rising/falling tone sweep (power up/down, etc), not overlapping beeps."""
    def _run():
        for f in freqs:
            _play_beep(f, duration_ms)
    threading.Thread(target=_run, daemon=True).start()


# =====================================================================
# ---------- Persistent memory (facts + history survive restarts) ----------
# =====================================================================
_DEFAULT_MEMORY = {"history": [], "facts": {}, "notes": []}


def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return json.loads(json.dumps(_DEFAULT_MEMORY))
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("history", [])
        data.setdefault("facts", {})
        data.setdefault("notes", [])
        return data
    except Exception as e:
        print("Memory load error (starting fresh):", e)
        return json.loads(json.dumps(_DEFAULT_MEMORY))


def save_memory(memory):
    try:
        temp_path = MEMORY_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, MEMORY_FILE)
        return True
    except Exception as e:
        print("Memory save error:", e)
        return False


memory_data = load_memory()

FACT_PATTERNS = [
    ("name", re.compile(r"\bmy name is (\w+)", re.IGNORECASE)),
    ("name", re.compile(r"\bcall me (\w+)", re.IGNORECASE)),
]
LIKE_PATTERN = re.compile(r"\bi (?:like|love|enjoy) (.+)", re.IGNORECASE)


def extract_facts(user_text):
    facts = {}
    for key, pattern in FACT_PATTERNS:
        m = pattern.search(user_text)
        if m:
            facts[key] = m.group(1).strip().capitalize()
    m = LIKE_PATTERN.search(user_text)
    if m:
        liked_thing = m.group(1).strip().rstrip(".!")
        if liked_thing:
            facts["likes"] = liked_thing
    return facts


def append_to_archive(role, text):
    """Writes ONE line to the permanent, never-trimmed archive file. This is
    what actually makes memory cover 'everything' rather than just the last
    MAX_HISTORY turns: nova_memory.json's history list gets trimmed for
    performance, but this file never does - append-only, one JSON object
    per line, so it stays cheap to write even after years of use (no
    read-modify-rewrite of a giant file, unlike save_memory())."""
    if not text:
        return
    try:
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"role": role, "text": text, "timestamp": time.time()}, ensure_ascii=False) + "\n")
    except Exception as e:
        print("Archive write error (non-fatal - the hot history still has this):", e)


def add_exchange(user_text, nova_text):
    global memory_data
    append_to_archive("user", user_text)
    append_to_archive("nova", nova_text)

    memory_data["history"].append({"role": "user", "text": user_text, "timestamp": time.time()})
    memory_data["history"].append({"role": "nova", "text": nova_text, "timestamp": time.time()})
    if len(memory_data["history"]) > MAX_HISTORY:
        memory_data["history"] = memory_data["history"][-MAX_HISTORY:]

    new_facts = extract_facts(user_text)
    for key, value in new_facts.items():
        if key == "likes":
            existing = memory_data["facts"].get("likes", [])
            if not isinstance(existing, list):
                existing = [existing]
            if value not in existing:
                existing.append(value)
            memory_data["facts"]["likes"] = existing
        else:
            memory_data["facts"][key] = value

    save_memory(memory_data)


def get_recent_history(n=10):
    return memory_data["history"][-n:]


def search_archive(query, max_results=5):
    """Substring search (case-insensitive) over the ENTIRE permanent
    archive, including anything already trimmed out of the hot history.
    This is what makes 'did I ever mention X' actually reach back to day
    one, not just the last ~300 exchanges. Plain substring matching, not
    semantic - simple, fast even on a large file, and honest about what
    it does (won't catch a paraphrase, only the words actually used)."""
    if not query.strip() or not os.path.exists(ARCHIVE_FILE):
        return []
    needle = query.strip().lower()
    matches = []
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if needle in entry.get("text", "").lower():
                    matches.append(entry)
    except Exception as e:
        print("Archive search error:", e)
        return []
    matches.sort(key=lambda e: e["timestamp"], reverse=True)
    return matches[:max_results]


def recall_reply(query):
    """Unified recall: semantic search (meaning-based, catches paraphrases,
    but only over the 'hot' ~600-entry window) plus a literal substring
    search over the FULL permanent archive (exact-words-only, but reaches
    every conversation ever had). Combines both rather than picking one,
    since they cover different gaps."""
    query = query.strip()
    if not query:
        return "What would you like me to recall?"

    semantic_hits = semantic_search_memory(query)
    archive_hits = search_archive(query)

    parts = []
    seen_texts = set()
    if semantic_hits:
        _sim, best_text, _reply = semantic_hits[0]
        parts.append(f'You mentioned: "{best_text}"')
        seen_texts.add(best_text.strip().lower())

    extra = [m for m in archive_hits if m["role"] == "user" and m["text"].strip().lower() not in seen_texts]
    if extra:
        lines = []
        for m in extra[:4]:
            when = time.strftime("%b %d", time.localtime(m["timestamp"]))
            lines.append(f'[{when}] "{m["text"]}"')
        parts.append("From my full archive: " + "; ".join(lines))

    if not parts:
        return f"I don't have anything matching '{query}' in memory."
    return " || ".join(parts)


def count_archive_entries():
    """Cheap line count for the archive file - used for the 'what do you
    remember about me' summary. Doesn't parse each line, just counts them,
    so it stays fast even on a large archive."""
    if not os.path.exists(ARCHIVE_FILE):
        return 0
    try:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception as e:
        print("Archive count error:", e)
        return 0


def add_note(text):
    memory_data.setdefault("notes", []).append({"text": text.strip(), "timestamp": time.time()})
    save_memory(memory_data)


def get_all_notes():
    return memory_data.get("notes", [])


def notes_reply():
    notes = get_all_notes()
    if not notes:
        return "You don't have any saved notes yet. Say 'remember that ...' or 'note that ...' to add one."
    lines = [f"{n['text']}" for n in notes[-10:]]  # most recent 10, oldest of that batch first
    return f"Your last {len(lines)} note{'s' if len(lines) != 1 else ''}: " + "; ".join(lines)


NOTE_PATTERN = re.compile(
    r"^(?:remember|note|remember that|note that|remember this|note this)[:,]?\s+(.+)", re.IGNORECASE)
RECALL_PATTERN = re.compile(
    r"\b(?:do you remember|did i (?:ever )?(?:tell|mention)|recall|search my memory for|"
    r"what did i say about)\s+(?:anything about\s+|that\s+)?(.+?)\??$", re.IGNORECASE)
NOTES_LIST_PATTERN = re.compile(r"\b(?:read my notes|what have you noted|what are my notes|list my notes)\b",
                                 re.IGNORECASE)


def handle_note_and_recall_command(cmd):
    """Returns a spoken reply if `cmd` matched a general-purpose 'remember
    everything' command (a free-form note, a recall/search, or a request
    to list notes), else None. Deliberately checked AFTER the more specific
    preference/routine patterns in handle_preference_and_routine_command,
    so 'remember that I prefer quiet mornings' is captured as a preference,
    not a generic note - the two shouldn't double up."""
    if NOTES_LIST_PATTERN.search(cmd):
        return notes_reply()

    m = RECALL_PATTERN.search(cmd)
    if m:
        return recall_reply(m.group(1).strip().rstrip("?.!"))

    m = NOTE_PATTERN.match(cmd.strip())
    if m:
        text = m.group(1).strip().rstrip(".!")
        text = re.sub(r"^(?:that|this)\s+", "", text, flags=re.IGNORECASE)
        if text:
            add_note(text)
            return "Got it, I've made a note of that."
    return None


def get_fact(key, default=None):
    return memory_data["facts"].get(key, default)


def get_all_facts():
    return dict(memory_data["facts"])


def clear_memory():
    """Wipes facts/preferences/routines/notes and the hot history window.
    Deliberately does NOT touch the permanent archive - that needs the
    separate, more explicit clear_archive_forever(), so a casual 'forget
    everything' can't accidentally destroy years of archived history."""
    global memory_data
    memory_data = json.loads(json.dumps(_DEFAULT_MEMORY))
    save_memory(memory_data)


def clear_archive_forever():
    """Permanently deletes the archive file. This is the one truly
    irreversible memory action, so it's gated behind its own much more
    explicit phrase (see the voice command pattern below) rather than
    living under the same trigger as clear_memory()."""
    try:
        if os.path.exists(ARCHIVE_FILE):
            os.remove(ARCHIVE_FILE)
        return True
    except Exception as e:
        print("Archive delete error:", e)
        return False


# =====================================================================
# ---------- Contextual intelligence: preferences, routines, tone ----------
# =====================================================================
# Extends the existing facts/history memory with two more persistent buckets
# (preferences, routines) plus session-only adaptive-tone tracking and
# simple multi-turn reference resolution ("he"/"that"/"it" -> whatever was
# last talked about). None of this needs a network call or an LLM - it's
# straightforward pattern-matching and a rolling in-memory context, so it's
# honest about being a heuristic, not genuine language understanding.

_DEFAULT_MEMORY["preferences"] = {}
_DEFAULT_MEMORY["routines"] = {}


def _ensure_memory_schema():
    """Older nova_memory.json files won't have these keys yet - add them
    in place rather than requiring people to delete their memory file."""
    memory_data.setdefault("preferences", {})
    memory_data.setdefault("routines", {})


def set_preference(key, value):
    _ensure_memory_schema()
    memory_data["preferences"][key.strip().lower()] = value.strip()
    save_memory(memory_data)


def get_preference(key, default=None):
    _ensure_memory_schema()
    return memory_data["preferences"].get(key.strip().lower(), default)


def get_all_preferences():
    _ensure_memory_schema()
    return dict(memory_data["preferences"])


def add_general_preference(text):
    """Free-form 'I prefer quiet mornings' style statements, kept as a
    deduplicated list under preferences['general'] (separate from the
    single-value key/value preferences like a stored tone_style)."""
    _ensure_memory_schema()
    general = memory_data["preferences"].setdefault("general", [])
    if not isinstance(general, list):
        general = [general]
        memory_data["preferences"]["general"] = general
    if text not in general:
        general.append(text)
    save_memory(memory_data)


def set_routine(name, steps):
    _ensure_memory_schema()
    memory_data["routines"][name.strip().lower()] = [s.strip() for s in steps if s.strip()]
    save_memory(memory_data)


def get_routine(name):
    _ensure_memory_schema()
    return memory_data["routines"].get(name.strip().lower())


def get_all_routines():
    _ensure_memory_schema()
    return dict(memory_data["routines"])


PREFERENCE_PATTERN = re.compile(
    r"\b(?:i prefer|remember (?:that )?i prefer|remember (?:that )?i like)\s+(.+)", re.IGNORECASE)
ROUTINE_SET_PATTERN = re.compile(
    r"\b(?:set|save|create)\s+my\s+(.+?)\s+routine\s+(?:to|as)\s+(.+)", re.IGNORECASE)
ROUTINE_GET_PATTERN = re.compile(r"\bwhat(?:'s| is)\s+my\s+(.+?)\s+routine\b", re.IGNORECASE)
ROUTINE_START_PATTERN = re.compile(r"\bstart(?:\s+my)?\s+(.+?)\s+routine\b", re.IGNORECASE)


def handle_preference_and_routine_command(cmd):
    """Returns a spoken reply if `cmd` matched one of these, else None."""
    m = PREFERENCE_PATTERN.search(cmd)
    if m:
        value = m.group(1).strip().rstrip(".!")
        if value:
            add_general_preference(value)
            return f"Noted - I'll remember you prefer {value}."

    m = ROUTINE_SET_PATTERN.search(cmd)
    if m:
        name, steps_text = m.group(1).strip(), m.group(2).strip()
        steps = [s.strip() for s in re.split(r",| and ", steps_text) if s.strip()]
        if not steps:
            return "I didn't catch any steps for that routine."
        set_routine(name, steps)
        return f"Saved your {name} routine with {len(steps)} step{'s' if len(steps) != 1 else ''}."

    m = ROUTINE_GET_PATTERN.search(cmd)
    if m:
        name = m.group(1).strip()
        steps = get_routine(name)
        if not steps:
            return f"You don't have a {name} routine saved yet. Say 'set my {name} routine to ...' to add one."
        return f"Your {name} routine: " + "; ".join(steps) + "."

    m = ROUTINE_START_PATTERN.search(cmd)
    if m:
        name = m.group(1).strip()
        steps = get_routine(name)
        if not steps:
            return f"You don't have a {name} routine saved yet."
        # Speak it as a paced list with real pauses, then hand control back -
        # Nova doesn't try to guess which steps are app-launch commands vs.
        # just reminders, since guessing wrong and silently skipping a step
        # would be worse than just reading the list back clearly.
        spoken = f"Starting your {name} routine.|| " + " || ".join(
            f"Step {i + 1}: {s}" for i, s in enumerate(steps))
        speak_async(spoken)
        return ""  # "" (not None) = already spoken directly above; None means "didn't match at all"
    return None


def what_do_you_remember_reply():
    _ensure_memory_schema()
    parts = []
    name = get_fact("name")
    if name:
        parts.append(f"Your name is {name}")
    likes = get_fact("likes")
    if likes:
        likes_list = likes if isinstance(likes, list) else [likes]
        parts.append(f"you like {', '.join(likes_list)}")
    prefs = get_all_preferences()
    general_prefs = prefs.get("general", [])
    if general_prefs:
        parts.append(f"{len(general_prefs)} preference{'s' if len(general_prefs) != 1 else ''} you've told me")
    routines = get_all_routines()
    if routines:
        parts.append(f"{len(routines)} saved routine{'s' if len(routines) != 1 else ''}: " + ", ".join(routines))
    notes = get_all_notes()
    if notes:
        parts.append(f"{len(notes)} note{'s' if len(notes) != 1 else ''} you've asked me to remember")
    archive_count = count_archive_entries()
    if archive_count:
        parts.append(f"a full archive of every conversation we've ever had ({archive_count} messages, searchable with 'recall ...')")
    if not parts:
        return "I don't have anything saved about you yet."
    return "Here's what I have: " + "; ".join(parts) + "."


# ---------- Adaptive tone ----------
# Detects the REGISTER of what was just said (formal / casual / technical)
# from surface cues in the wording, and nudges Nova's own phrasing to match.
# This is a real heuristic classifier, not a trained model - it will
# misjudge short or ambiguous messages sometimes, same as a human skimming
# for tone would.
_FORMAL_MARKERS = (
    "would you", "could you please", "kindly", "i would like", "please provide",
    "thank you very much", "i am writing to", "sincerely", "regarding",
)
_CASUAL_MARKERS = (
    "lol", "lmao", "haha", "gonna", "wanna", "kinda", "sorta", "yo", "bro",
    "dude", "sup", "nah", "yep", "omg", "btw",
)
_TECHNICAL_MARKERS = (
    "function", "variable", "algorithm", "api", "database", "server", "compile",
    "runtime", "syntax", "regex", "async", "thread", "config", "parameter",
    "endpoint", "framework", "repository", "debug", "latency", "kernel",
)


def detect_register(text):
    """Returns 'formal' | 'casual' | 'technical' | 'neutral'."""
    t = " " + text.lower() + " "
    tech_hits = sum(1 for w in _TECHNICAL_MARKERS if f" {w}" in t or f"{w} " in t)
    if tech_hits >= 2:
        return "technical"
    if any(m in t for m in _FORMAL_MARKERS) and not any(m in t for m in _CASUAL_MARKERS):
        return "formal"
    if any(m in t for m in _CASUAL_MARKERS):
        return "casual"
    has_contraction = bool(re.search(r"\b\w+'(?:t|re|ve|ll|d|m)\b", text.lower()))
    if not has_contraction and len(text.split()) > 12 and text.strip().endswith("."):
        return "formal"
    return "neutral"


_CONTRACTION_EXPANSIONS = {
    "i'm": "I am", "you're": "you are", "it's": "it is", "that's": "that is",
    "don't": "do not", "can't": "cannot", "won't": "will not", "i've": "I have",
    "i'll": "I will", "we're": "we are", "isn't": "is not", "aren't": "are not",
}


def adapt_reply_tone(reply, register):
    """Light phrasing nudge, not a rewrite - the reply's actual content and
    meaning never change, only some word choices around the edges."""
    if register == "formal":
        out = reply
        for contraction, expansion in _CONTRACTION_EXPANSIONS.items():
            out = re.sub(r"\b" + re.escape(contraction) + r"\b", expansion, out, flags=re.IGNORECASE)
        return out
    if register == "casual":
        # Trim an overly stiff sign-off; keep everything else as-is.
        return re.sub(r"^(Understood\.|Noted\.|Very well\.)\s*", "", reply)
    return reply


# ---------- Multi-turn reference resolution ----------
# Tracks the last thing that was clearly a topic (from an explanation or a
# named entity in the conversation), so a follow-up like "tell me more about
# that" or "what about him" can be resolved without the person repeating the
# full subject. Deliberately simple: one slot, short expiry, and it only
# ever SUBSTITUTES a pronoun/placeholder - it never invents context that
# wasn't actually said.
session_context = {"last_topic": None, "updated": 0.0}
CONTEXT_EXPIRY_SECONDS = 300  # 5 minutes of silence on the subject and it's considered stale

_FOLLOWUP_PATTERNS = (
    re.compile(r"^(?:tell me more|go deeper|more detail|elaborate|explain (?:that|it) more)$", re.IGNORECASE),
    re.compile(r"^what about (?:him|her|it|that|them)\??$", re.IGNORECASE),
    re.compile(r"^(?:and|so) what about (?:him|her|it|that|them)\??$", re.IGNORECASE),
)


def remember_topic(topic):
    if topic:
        session_context["last_topic"] = topic.strip()
        session_context["updated"] = time.time()


def resolve_followup(cmd):
    """If `cmd` is a vague follow-up ('tell me more', 'what about him') and
    there's a recent topic, returns the resolved question. Otherwise None."""
    if not session_context["last_topic"]:
        return None
    if time.time() - session_context["updated"] > CONTEXT_EXPIRY_SECONDS:
        return None
    for pat in _FOLLOWUP_PATTERNS:
        if pat.match(cmd.strip()):
            return f"tell me more about {session_context['last_topic']}"
    return None


# =====================================================================
# ---------- Semantic memory search (local embeddings, no API cost) ----------
# =====================================================================
# Lets you ask "what did I say about X" and get a genuinely meaning-based
# match, not just keyword overlap. Uses sentence-transformers, imported
# lazily since it pulls in a real neural network model (and torch, if not
# already installed for the vision features).
SEMANTIC_MODEL_NAME = "all-MiniLM-L6-v2"  # small (~80MB), fast, good enough for personal use

SENTENCE_TRANSFORMERS_AVAILABLE = False
_semantic_import_attempted = False
_semantic_model_cache = None
SentenceTransformer = None


def _ensure_semantic_model():
    global _semantic_import_attempted, SENTENCE_TRANSFORMERS_AVAILABLE, SentenceTransformer
    if _semantic_import_attempted:
        return SENTENCE_TRANSFORMERS_AVAILABLE
    _semantic_import_attempted = True
    try:
        from sentence_transformers import SentenceTransformer as _SentenceTransformer
        SentenceTransformer = _SentenceTransformer
        SENTENCE_TRANSFORMERS_AVAILABLE = True
    except Exception as e:
        print("sentence-transformers import error (pip install sentence-transformers):", e)
    return SENTENCE_TRANSFORMERS_AVAILABLE


def _load_semantic_model():
    global _semantic_model_cache
    if _semantic_model_cache is not None:
        return _semantic_model_cache
    if not _ensure_semantic_model():
        return None
    try:
        print("Loading semantic memory model... this only happens once per run.")
        _semantic_model_cache = SentenceTransformer(SEMANTIC_MODEL_NAME)
    except Exception as e:
        print("Semantic model load error:", e)
        _semantic_model_cache = None
    return _semantic_model_cache


def cosine_similarity(vec_a, vec_b):
    """Pure function, testable without the actual model - returns a value
    from -1 to 1, where 1 means identical direction (same meaning)."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_text(text):
    """Returns an embedding (list of floats) for a piece of text, or None
    if the semantic model isn't available."""
    model = _load_semantic_model()
    if model is None:
        return None
    try:
        vec = model.encode(text)
        return vec.tolist()
    except Exception as e:
        print("embed_text error:", e)
        return None


def index_memory_embeddings():
    """
    Compute (and cache in memory_data) embeddings for any user turns in
    history that don't have one yet. Called lazily the first time a
    semantic search is requested, not on every exchange, to avoid paying
    the embedding cost for conversation that's never searched.
    """
    model = _load_semantic_model()
    if model is None:
        return False

    embeddings = memory_data.setdefault("embeddings", {})
    changed = False
    for entry in memory_data["history"]:
        if entry["role"] != "user":
            continue
        key = str(entry["timestamp"])
        if key not in embeddings:
            vec = embed_text(entry["text"])
            if vec is not None:
                embeddings[key] = vec
                changed = True

    if changed:
        save_memory(memory_data)
    return True


def semantic_search_memory(query, top_k=3, min_similarity=0.35):
    """
    Search past user utterances by meaning, not keywords. Returns a list
    of (similarity, user_text, nova_reply) tuples, best match first.
    """
    if not index_memory_embeddings():
        return []

    query_vec = embed_text(query)
    if query_vec is None:
        return []

    embeddings = memory_data.get("embeddings", {})
    history = memory_data["history"]

    scored = []
    for i, entry in enumerate(history):
        if entry["role"] != "user":
            continue
        key = str(entry["timestamp"])
        vec = embeddings.get(key)
        if vec is None:
            continue
        sim = cosine_similarity(query_vec, vec)
        if sim >= min_similarity:
            nova_reply = history[i + 1]["text"] if i + 1 < len(history) and history[i + 1]["role"] == "nova" else ""
            scored.append((sim, entry["text"], nova_reply))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def describe_semantic_results(results):
    if not results:
        return "I don't remember anything like that."
    best_sim, best_text, best_reply = results[0]
    return f"You mentioned: \"{best_text}\"."


# =====================================================================
# ---------- Speech / TTS ----------
# =====================================================================
recognizer = sr.Recognizer()
recognizer.dynamic_energy_threshold = True
recognizer.pause_threshold = 0.8
# NOTE: there is deliberately NO shared sr.Microphone() here. A single Microphone
# object can only be "entered" once at a time; reusing it across the main
# listener, sleep listener and calibration caused the "already inside a context
# manager" crash. Every listening session now builds its own (see new_mic()).
engine_lock = threading.Lock()
try:
    pygame.mixer.init()
except Exception as _e:
    print("pygame mixer init failed (speech playback may not work):", _e)


def new_mic():
    """Fresh Microphone for each listening session - never share one object."""
    return sr.Microphone()


async def _generate_speech(text, voice, out_path, rate="+0%", volume="+0%", pitch="+0Hz"):
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
    await communicate.save(out_path)


# =====================================================================
# ---------- Live microphone volume monitor (for the GUI visualizer) ----------
# =====================================================================
# speech_recognition's listen_in_background only fires once per complete
# phrase - no good for a smooth real-time visualizer. This opens a SEPARATE
# lightweight audio stream via PyAudio (already installed, since
# speech_recognition's Microphone class depends on it) purely to sample
# volume continuously. Runs alongside the real recognizer without
# interfering with it.
current_volume_level = 0.0   # 0.0-1.0, read by the GUI's render loop
_volume_monitor_active = False
_volume_monitor_thread = None

VOLUME_CHUNK_SIZE = 1024
VOLUME_MIN_RMS = 50     # below this is treated as silence
VOLUME_MAX_RMS = 3000   # at/above this is treated as "full" on the visualizer


def compute_rms(audio_chunk_bytes):
    """RMS amplitude from raw 16-bit PCM bytes. Avoids the audioop module,
    which is deprecated and removed in newer Python versions."""
    count = len(audio_chunk_bytes) // 2
    if count == 0:
        return 0.0
    import struct
    samples = struct.unpack(f"<{count}h", audio_chunk_bytes[:count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def rms_to_volume_level(rms, min_rms=VOLUME_MIN_RMS, max_rms=VOLUME_MAX_RMS):
    """Normalize raw RMS into a clean 0.0-1.0 range for drawing."""
    if rms <= min_rms:
        return 0.0
    if rms >= max_rms:
        return 1.0
    return (rms - min_rms) / (max_rms - min_rms)


def _volume_monitor_loop():
    global current_volume_level, _volume_monitor_active
    try:
        import pyaudio
    except Exception as e:
        print("PyAudio import error - volume visualizer disabled:", e)
        _volume_monitor_active = False
        return

    pa = pyaudio.PyAudio()
    stream = None
    try:
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                          input=True, frames_per_buffer=VOLUME_CHUNK_SIZE)
        while _volume_monitor_active:
            try:
                chunk = stream.read(VOLUME_CHUNK_SIZE, exception_on_overflow=False)
                rms = compute_rms(chunk)
                current_volume_level = rms_to_volume_level(rms)
            except Exception as e:
                print("Volume monitor read error:", e)
                time.sleep(0.1)
    except Exception as e:
        print("Volume monitor stream error (visualizer will stay flat - this is not fatal):", e)
        _volume_monitor_active = False
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception as e:
            print("Volume monitor stream cleanup error (non-fatal):", e)
        try:
            pa.terminate()
        except Exception as e:
            print("PyAudio terminate error (non-fatal):", e)
        current_volume_level = 0.0


def start_volume_monitor():
    global _volume_monitor_active, _volume_monitor_thread
    if _volume_monitor_active:
        return
    _volume_monitor_active = True
    _volume_monitor_thread = threading.Thread(target=_volume_monitor_loop, daemon=True)
    _volume_monitor_thread.start()


def stop_volume_monitor():
    global _volume_monitor_active, current_volume_level
    _volume_monitor_active = False
    current_volume_level = 0.0


app_state = "idle"  # "idle" / "listening" / "speaking" - read by the GUI to show status text
_gui_root_ref = None  # set by NovaGUI.__init__; lets background threads schedule GUI work via root.after()

# ---------- Emotional prosody ----------
# edge-tts's Communicate genuinely supports rate/volume/pitch (they're passed
# straight to Microsoft's neural TTS service), so this is real prosody control,
# not just cosmetic. It's still a synthesized voice, not a human one - the
# emotional range is "audibly warmer/quicker/softer", not full expressive
# acting. Keys match the labels detect_emotion() below can return.
EMOTION_VOICE_PARAMS = {
    "happy":      {"rate": "+12%", "volume": "+0%",  "pitch": "+25Hz"},
    "excited":    {"rate": "+18%", "volume": "+8%",   "pitch": "+35Hz"},
    "sad":        {"rate": "-12%", "volume": "-6%",   "pitch": "-25Hz"},
    "empathetic": {"rate": "-8%",  "volume": "-4%",   "pitch": "-10Hz"},  # for comforting replies
    "angry_calm": {"rate": "-10%", "volume": "-4%",   "pitch": "-15Hz"},  # de-escalating tone, used when the USER sounds angry
    "urgent":     {"rate": "+15%", "volume": "+10%",  "pitch": "+10Hz"},
    "tired":      {"rate": "-10%", "volume": "-8%",   "pitch": "-15Hz"},
    "neutral":    {"rate": "+0%",  "volume": "+0%",   "pitch": "+0Hz"},
}

# A real, audible pause: rather than sprinkling in SSML tags edge-tts would
# just read aloud as literal text, "||" (short, ~350ms) and "|||" (long,
# ~800ms) split the text into separate TTS renders played back to back with
# actual silence between them. Use sparingly - for dramatic or list-reading
# moments, not every sentence.
_PAUSE_SHORT = 0.35
_PAUSE_LONG = 0.80


def _split_on_pauses(text):
    """('a||b|||c') -> [('a', 0), ('b', 0.35), ('c', 0.80)] - each chunk paired
    with the silence to play BEFORE it (0 for the first chunk)."""
    parts = re.split(r"(\|{2,3})", text)
    chunks = []
    pending_pause = 0.0
    for part in parts:
        if part == "||":
            pending_pause = _PAUSE_SHORT
        elif part == "|||":
            pending_pause = _PAUSE_LONG
        elif part.strip():
            chunks.append((part.strip(), pending_pause))
            pending_pause = 0.0
    return chunks or [(text, 0.0)]


_speech_stop_event = threading.Event()


def stop_speaking():
    """Immediately halts whatever Nova is currently saying - used for both
    an explicit 'stop talking' and automatic barge-in (the instant you say
    or type something new, Nova should stop talking over you, not finish
    its sentence first)."""
    _speech_stop_event.set()
    try:
        pygame.mixer.music.stop()
    except Exception:
        pass


def is_speaking():
    return app_state == "speaking"


def speak(text, retries=1, emotion="neutral", voice=None):
    """Blocking speech - use when the caller needs to know speech finished
    before continuing (e.g. the startup greeting, before mic calibration).
    `emotion` picks a rate/pitch/volume preset from EMOTION_VOICE_PARAMS;
    unknown labels just fall back to neutral rather than erroring.
    `voice` overrides the default English voice - used to speak Hindi/
    Malayalam replies with their own neural voice (see LANGUAGES below).
    Checks _speech_stop_event between every chunk/pause/play so stop_speaking()
    (above) can cut this off within a beat rather than waiting it out."""
    global app_state
    previous_state = app_state
    app_state = "speaking"
    _speech_stop_event.clear()
    params = EMOTION_VOICE_PARAMS.get(emotion, EMOTION_VOICE_PARAMS["neutral"])
    chunks = _split_on_pauses(text)
    voice = voice or TTS_VOICE

    for attempt in range(retries + 1):
        try:
            with engine_lock:
                for chunk_text, pause_before in chunks:
                    if _speech_stop_event.is_set():
                        break
                    remaining = pause_before
                    while remaining > 0 and not _speech_stop_event.is_set():
                        time.sleep(min(0.05, remaining))
                        remaining -= 0.05
                    if _speech_stop_event.is_set():
                        break
                    fd, temp_path = tempfile.mkstemp(suffix=".mp3")
                    os.close(fd)
                    asyncio.run(_generate_speech(chunk_text, voice, temp_path,
                                                  rate=params["rate"], volume=params["volume"],
                                                  pitch=params["pitch"]))
                    pygame.mixer.music.load(temp_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy() and not _speech_stop_event.is_set():
                        time.sleep(0.05)
                    if _speech_stop_event.is_set():
                        try:
                            pygame.mixer.music.stop()
                        except Exception:
                            pass
                    pygame.mixer.music.unload()
                    os.remove(temp_path)
            app_state = previous_state
            return
        except Exception as e:
            print(f"TTS error (attempt {attempt + 1}):", e)
            time.sleep(0.5)
    app_state = previous_state


def speak_async(text, emotion="neutral", voice=None):
    if not text:
        return
    t = threading.Thread(target=speak, args=(text,), kwargs={"emotion": emotion, "voice": voice}, daemon=True)
    t.start()


# =====================================================================
# ---------- Multilingual support ----------
# =====================================================================
# How it works:
#   HEARING  - each phrase goes to Google's recognizer in the chosen language
#              (or in AUTO mode, several at once in parallel - best confidence wins).
#   THINKING - Nova's command engine is written in English, so anything else
#              is translated to English first, and every reply is translated
#              back into the reply language.
#   SPEAKING - edge-tts has a native neural voice for every language below.
#
# Translation uses Google's free web-translate endpoint (urllib only - no
# extra pip packages, nothing new to bundle in nova.spec). Like the speech
# recognizer, it needs an internet connection. If translation is
# unreachable, Nova falls back to speaking English rather than going silent.
#
# AUTO mode (simultaneous recognition with no language picked) only tries
# AUTO_LANGS below, not every language in this table - each language in
# AUTO means one extra real-time call to Google's speech API per phrase you
# say, so it's kept to a handful of languages with their own distinct
# script (so a quick look at the text reliably tells them apart). Every
# other language still works perfectly - just say "speak Arabic" (or press
# it in the LANGUAGE panel) to listen and reply in it directly, which only
# needs ONE recognition call since the language is no longer a guess.
#
# To add another language later, add one entry here (plus its script range
# in _SCRIPT_RANGES if it isn't written in Latin letters, and a pattern in
# _LANG_NAME_PATTERNS so "speak <language>" recognizes its name) - the
# LANGUAGE panel's "More" dropdown picks it up automatically.
LANGUAGES = {
    "en": {"name": "English", "native": "English", "stt": "en-IN", "voice": TTS_VOICE,
           "greeting": "Hello, I am Nova. I will speak English."},
    "hi": {"name": "Hindi", "native": "\u0939\u093f\u0928\u094d\u0926\u0940", "stt": "hi-IN",
           "voice": "hi-IN-MadhurNeural",      # female alternative: hi-IN-SwaraNeural
           "greeting": "\u0928\u092e\u0938\u094d\u0924\u0947, \u092e\u0948\u0902 \u0928\u094b\u0935\u093e \u0939\u0942\u0901\u0964 "
                       "\u092e\u0948\u0902 \u0905\u092c \u0939\u093f\u0928\u094d\u0926\u0940 \u092e\u0947\u0902 \u092c\u093e\u0924 \u0915\u0930 \u0938\u0915\u0924\u093e \u0939\u0942\u0901\u0964"},
    "ml": {"name": "Malayalam", "native": "\u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d02", "stt": "ml-IN",
           "voice": "ml-IN-MidhunNeural",      # female alternative: ml-IN-SobhanaNeural
           "greeting": "\u0d28\u0d2e\u0d38\u0d4d\u0d15\u0d3e\u0d30\u0d02, \u0d1e\u0d3e\u0d7b \u0d28\u0d4b\u0d35\u0d2f\u0d3e\u0d23\u0d4d. "
                       "\u0d1e\u0d3e\u0d7b \u0d07\u0d2a\u0d4d\u0d2a\u0d4b\u0d7e \u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d24\u0d4d\u0d24\u0d3f\u0d7d \u0d38\u0d02\u0d38\u0d3e\u0d30\u0d3f\u0d15\u0d4d\u0d15\u0d41\u0d02."},
    "ar": {"name": "Arabic", "native": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "stt": "ar-SA", "voice": "ar-SA-HamedNeural",
           # female alternative: ar-SA-ZariyahNeural
           "greeting": "\u0645\u0631\u062d\u0628\u0627\u064b\u060c \u0623\u0646\u0627 \u0646\u0648\u0641\u0627. \u0633\u0623\u062a\u062d\u062f\u062b \u0627\u0644\u0622\u0646 \u0628\u0627\u0644\u0639\u0631\u0628\u064a\u0629."},
    "ta": {"name": "Tamil", "native": "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd", "stt": "ta-IN", "voice": "ta-IN-ValluvarNeural",
           # female alternative: ta-IN-PallaviNeural
           "greeting": "\u0bb5\u0ba3\u0b95\u0bcd\u0b95\u0bae\u0bcd, \u0ba8\u0bbe\u0ba9\u0bcd \u0ba8\u0bcb\u0bb5\u0bbe. \u0b87\u0baa\u0bcd\u0baa\u0bcb\u0ba4\u0bc1 \u0ba4\u0bae\u0bbf\u0bb4\u0bbf\u0bb2\u0bcd \u0baa\u0bc7\u0b9a\u0bc1\u0b95\u0bbf\u0bb1\u0bc7\u0ba9\u0bcd."},
    "ur": {"name": "Urdu", "native": "\u0627\u0631\u062f\u0648", "stt": "ur-PK", "voice": "ur-PK-AsadNeural",
           # female alternative: ur-PK-UzmaNeural
           "greeting": "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u06cc\u06a9\u0645\u060c \u0645\u06cc\u06ba \u0646\u0648\u0648\u0627 \u06c1\u0648\u06ba\u06d4 \u0627\u0628 \u0645\u06cc\u06ba \u0627\u0631\u062f\u0648 \u0645\u06cc\u06ba \u0628\u0627\u062a \u06a9\u0631\u0648\u06ba \u06af\u06cc\u06d4"},
    "kn": {"name": "Kannada", "native": "\u0c95\u0ca8\u0ccd\u0ca8\u0ca1", "stt": "kn-IN", "voice": "kn-IN-GaganNeural",
           # female alternative: kn-IN-SapnaNeural
           "greeting": "\u0ca8\u0cae\u0cb8\u0ccd\u0c95\u0cbe\u0cb0, \u0ca8\u0cbe\u0ca8\u0cc1 \u0ca8\u0ccb\u0cb5\u0cbe. \u0c88\u0c97 \u0c95\u0ca8\u0ccd\u0ca8\u0ca1\u0ca6\u0cb2\u0ccd\u0cb2\u0cbf \u0cae\u0cbe\u0ca4\u0ca8\u0cbe\u0ca1\u0cc1\u0ca4\u0ccd\u0ca4\u0cc7\u0ca8\u0cc6."},
    "bn": {"name": "Bengali", "native": "\u09ac\u09be\u0982\u09b2\u09be", "stt": "bn-IN", "voice": "bn-IN-BashkarNeural",
           # female alternative: bn-IN-TanishaaNeural
           "greeting": "\u09a8\u09ae\u09b8\u09cd\u0995\u09be\u09b0, \u0986\u09ae\u09bf \u09a8\u09cb\u09ad\u09be\u0964 \u098f\u0996\u09a8 \u0986\u09ae\u09bf \u09ac\u09be\u0982\u09b2\u09be\u09af\u09bc \u0995\u09a5\u09be \u09ac\u09b2\u09ac\u0964"},
    "es": {"name": "Spanish", "native": "Espa\u00f1ol", "stt": "es-ES", "voice": "es-ES-AlvaroNeural",
           # female alternative: es-ES-ElviraNeural
           "greeting": "Hola, soy Nova. Ahora hablar\u00e9 en espa\u00f1ol."},
    "it": {"name": "Italian", "native": "Italiano", "stt": "it-IT", "voice": "it-IT-DiegoNeural",
           # female alternative: it-IT-ElsaNeural
           "greeting": "Ciao, sono Nova. Ora parler\u00f2 in italiano."},
    "fr": {"name": "French", "native": "Fran\u00e7ais", "stt": "fr-FR", "voice": "fr-FR-HenriNeural",
           # female alternative: fr-FR-DeniseNeural
           "greeting": "Bonjour, je suis Nova. Je vais maintenant parler fran\u00e7ais."},
    "de": {"name": "German", "native": "Deutsch", "stt": "de-DE", "voice": "de-DE-ConradNeural",
           # female alternative: de-DE-KatjaNeural
           "greeting": "Hallo, ich bin Nova. Ich spreche jetzt Deutsch."},
}
AUTO_LANGS = ("en", "hi", "ml")   # which languages AUTO listening tries

LANG_FILE = os.path.join(DATA_DIR, "nova_language.json")
LANG_STATE = {
    "listen": "auto",       # "auto" or a key of LANGUAGES
    "reply": "match",       # "match" (reply in whatever you last spoke) or a key of LANGUAGES
    "last_heard": "en",     # language of the most recent thing you said/typed
    "heard_text": "",       # what you said, in its own script (shown in the LANGUAGE panel)
    "heard_en": "",         # the English meaning Nova acted on
    "reply_text": "",       # what Nova last said, in the language it was spoken in
    "translator_ok": None,  # None = not used yet, True/False = last translation succeeded/failed
}


def _load_language_settings():
    try:
        with open(LANG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("listen") in ("auto", *LANGUAGES):
            LANG_STATE["listen"] = saved["listen"]
        if saved.get("reply") in ("match", *LANGUAGES):
            LANG_STATE["reply"] = saved["reply"]
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Couldn't read language settings (using defaults):", e)
    if LANG_STATE["listen"] != "auto":
        LANG_STATE["last_heard"] = LANG_STATE["listen"]


def _save_language_settings():
    try:
        with open(LANG_FILE, "w", encoding="utf-8") as f:
            json.dump({"listen": LANG_STATE["listen"], "reply": LANG_STATE["reply"]}, f)
    except Exception as e:
        print("Couldn't save language settings:", e)


def set_languages(listen=None, reply=None):
    """Change what Nova listens for and/or replies in. Used by both the
    LANGUAGE panel buttons and the spoken 'speak Malayalam' commands."""
    if listen in ("auto", *LANGUAGES):
        LANG_STATE["listen"] = listen
        if listen != "auto":
            LANG_STATE["last_heard"] = listen   # so 'match me' replies switch immediately
    if reply in ("match", *LANGUAGES):
        LANG_STATE["reply"] = reply
    _save_language_settings()


def set_last_heard(lang, heard_text, heard_english):
    LANG_STATE["last_heard"] = lang if lang in LANGUAGES else "en"
    LANG_STATE["heard_text"] = heard_text
    LANG_STATE["heard_en"] = heard_english


def resolve_reply_lang():
    """The language the next spoken reply should be in."""
    choice = LANG_STATE["reply"]
    if choice == "match":
        choice = LANG_STATE["last_heard"]
    return choice if choice in LANGUAGES else "en"


# ---------- Script detection (which language is this text written in?) ----------
# Only languages with their OWN distinct Unicode block can be told apart this
# way. Arabic and Urdu share the Arabic script (ambiguous - defaults to
# Arabic); Spanish/Italian/French/German/English all share Latin letters
# (ambiguous - defaults to English). Explicit "speak <language>" selection
# sidesteps this entirely, since the language is then already known.
_SCRIPT_RANGES = {
    "hi": (0x0900, 0x097F), "ml": (0x0D00, 0x0D7F), "ta": (0x0B80, 0x0BFF),
    "kn": (0x0C80, 0x0CFF), "bn": (0x0980, 0x09FF), "ar": (0x0600, 0x06FF),
}


def _count_script(text, lang):
    lo, hi = _SCRIPT_RANGES[lang]
    return sum(1 for ch in text if lo <= ord(ch) <= hi)


def detect_script_language(text):
    """Best-guess language of TYPED text, by which script it's written in.
    Instant and offline - used for typed input and to label what the
    recognizer returned. See the _SCRIPT_RANGES note above for its limits."""
    counts = {lang: _count_script(text, lang) for lang in _SCRIPT_RANGES}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else "en"



# ---------- Translation ----------
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_translate_cache = {}


def _chunk_text(text, limit=400):
    """Split on sentence ends so each request stays a sensible size."""
    pieces = re.split(r"(?<=[.!?\u0964])\s+", text)
    chunks, current = [], ""
    for piece in pieces:
        if len(current) + len(piece) + 1 <= limit:
            current = (current + " " + piece).strip()
            continue
        if current:
            chunks.append(current)
        while len(piece) > limit:
            chunks.append(piece[:limit])
            piece = piece[limit:]
        current = piece
    if current:
        chunks.append(current)
    return chunks


def _gt_request(text, source, target):
    params = urllib.parse.urlencode({"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text})
    req = urllib.request.Request(_TRANSLATE_URL + "?" + params, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def translate_text(text, source, target):
    """Translate text; returns None (never raises) if the service is
    unreachable, so callers can fall back gracefully."""
    text = (text or "").strip()
    if not text or source == target:
        return text
    key = (text, source, target)
    if key in _translate_cache:
        return _translate_cache[key]
    try:
        result = " ".join(_gt_request(chunk, source, target) for chunk in _chunk_text(text)).strip()
    except Exception as e:
        print(f"Translate error ({source}->{target}):", e)
        LANG_STATE["translator_ok"] = False
        return None
    LANG_STATE["translator_ok"] = True
    if len(_translate_cache) > 300:
        _translate_cache.clear()
    _translate_cache[key] = result
    return result


def localize_reply(text, lang):
    """English reply -> reply language, keeping the '||' / '|||' spoken-pause
    markers in place. Returns None if translation failed."""
    if lang == "en" or not text:
        return text
    out = []
    for part in re.split(r"(\|{2,3})", text):
        if not part.strip() or re.fullmatch(r"\|{2,3}", part):
            out.append(part)
            continue
        translated = translate_text(part.strip(), "en", lang)
        if translated is None:
            return None
        out.append(translated)
    return "".join(out)


def display_reply(reply, lang):
    """The reply as it should appear on screen (no pause markers), in the
    language it will be spoken in."""
    localized = localize_reply(reply, lang) if reply else reply
    return re.sub(r"\s*\|{2,3}\s*", " ", (localized if localized is not None else reply) or "").strip()


def understand_text(text, lang=None):
    """(english_text, source_language) for typed input. english_text is
    None if it needed translating and the service was unreachable."""
    lang = lang or detect_script_language(text)
    if lang == "en":
        return text, "en"
    return translate_text(text, lang, "en"), lang


# ---------- Speech recognition (per-language, or all three in AUTO) ----------
def _google_transcribe(recognizer_obj, audio, lang):
    """One recognition attempt -> (text, confidence) or None."""
    raw = recognizer_obj.recognize_google(audio, language=LANGUAGES[lang]["stt"], show_all=True)
    if not isinstance(raw, dict):
        return None
    alternatives = raw.get("alternative") or []
    if not alternatives:
        return None
    best = alternatives[0]
    text = (best.get("transcript") or "").strip()
    if not text:
        return None
    return text, best.get("confidence")


def _auto_score(lang, text, confidence):
    score = confidence if confidence is not None else 0.5
    # A Hindi/Malayalam result that came back in Latin letters is usually
    # the recognizer forcing English speech into the wrong model.
    if lang in _SCRIPT_RANGES and _count_script(text, lang) == 0:
        score -= 0.25
    if lang == LANG_STATE["last_heard"]:
        score += 0.05   # conversations tend to stay in one language
    return score


def recognize_multilingual(recognizer_obj, audio):
    """(text, language) or None if nothing was understood. Raises
    sr.RequestError if the recognition service is unreachable."""
    mode = LANG_STATE["listen"]
    if mode != "auto":
        # The language is already known here - it's whichever locale we just
        # asked Google's recognizer to transcribe against - so trust that
        # directly rather than re-guessing it from the resulting text (a
        # bug: script-guessing would silently mislabel any language that
        # doesn't have its own Unicode block, e.g. Spanish, French, German,
        # Urdu, breaking translation for every one of them).
        result = _google_transcribe(recognizer_obj, audio, mode)
        return (result[0], mode) if result else None

    results, errors = {}, []

    def _worker(lang):
        try:
            found = _google_transcribe(recognizer_obj, audio, lang)
            if found:
                results[lang] = found
        except sr.RequestError as e:
            errors.append(e)
        except Exception as e:
            print(f"Recognition error ({lang}):", e)

    threads = [threading.Thread(target=_worker, args=(lang,), daemon=True) for lang in AUTO_LANGS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    if not results:
        if errors:
            raise errors[0]
        return None
    best_lang = max(results, key=lambda lang: _auto_score(lang, results[lang][0], results[lang][1]))
    # best_lang is which recognizer model actually produced this transcript -
    # a far more reliable label than re-guessing from the text (see above).
    return results[best_lang][0], best_lang


# ---------- Spoken / typed language commands ----------
_LANG_NAME_PATTERNS = {
    "en": r"english|\u0d07\u0d02\u0d17\u0d4d\u0d32\u0d40\u0d37\u0d4d|\u0905\u0902\u0917\u094d\u0930\u0947\u091c\u093c\u0940|\u0905\u0902\u0917\u094d\u0930\u0947\u091c\u0940",
    "hi": r"hindi|\u0d39\u0d3f\u0d28\u0d4d\u0d26\u0d3f|\u0939\u093f\u0928\u094d\u0926\u0940|\u0939\u093f\u0902\u0926\u0940",
    "ml": r"malayalam|\u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d02|\u092e\u0932\u092f\u093e\u0932\u092e",
    "ar": r"arabic|\u0627\u0644\u0639\u0631\u0628\u064a\u0629",
    "ta": r"tamil|\u0ba4\u0bae\u0bbf\u0bb4\u0bcd",
    "ur": r"urdu|\u0627\u0631\u062f\u0648",
    "kn": r"kannada|\u0c95\u0ca8\u0ccd\u0ca8\u0ca1",
    "bn": r"bengali|bangla|\u09ac\u09be\u0982\u09b2\u09be",
    "es": r"spanish|espa\u00f1ol|espanol",
    "it": r"italian|italiano",
    "fr": r"french|fran\u00e7ais|francais",
    "de": r"german|deutsch",
}


def handle_language_command(cmd):
    """'speak Malayalam', 'reply in Hindi', 'listen in English', 'auto
    language' ... Returns an English confirmation (it gets translated into
    the new reply language automatically), or None if this isn't one."""
    c = cmd.lower()

    if re.search(r"\b(what|which) languages\b|\blanguages (do|can) you (speak|understand|know)\b", c):
        names = ", ".join(LANGUAGES[code]["name"] for code in LANGUAGES)
        return (f"I can understand and speak {names}. Say 'speak Malayalam' or "
                "'reply in Hindi' to switch, or 'auto language' to let me follow whichever you speak.")

    # 'how do you say X in Hindi' / 'translate X' are questions, not switches.
    if re.search(r"\b(translate|how (do|would|can) (you|i) say|meaning of|what('s| is| does))\b", c):
        return None

    if (re.search(r"\b(auto|automatic|automatically|any language|all languages|multilingual|match my language|"
                  r"same language as (me|i)|language i speak)\b", c)
            and re.search(r"language|reply|respond|answer|speak|talk|listen|mode", c)):
        set_languages(listen="auto", reply="match")
        return ("Understood. I'll understand English, Hindi and Malayalam, "
                "and reply in whichever one you speak.")

    # A language only counts as a switch when it's the target of "in/to X" or
    # "speak/use X" - so "talk about hindi movies" or "play hindi songs" are
    # left alone for the normal command engine.
    targeted = [code for code, pattern in _LANG_NAME_PATTERNS.items()
                if re.search(rf"(\b(in|to|into)\s+(the\s+)?|\b(speak|use)\s+(in\s+)?)({pattern})", c)]
    if len(targeted) != 1:
        return None
    code = targeted[0]
    name = LANGUAGES[code]["name"]

    if re.search(r"\b(reply|respond|answer)\b", c):
        set_languages(reply=code)
        return f"Okay, I'll reply in {name}."
    if re.search(r"\b(listen|understand)\b", c):
        set_languages(listen=code)
        return f"Okay, I'll listen for {name}."
    if re.search(r"\b(speak|talk|switch|change|converse|use)\b", c):
        set_languages(listen=code, reply=code)
        return f"Okay, I'll speak and listen in {name} now."
    return None


def translate_command_to_english(heard_text, heard_lang):
    """(english_text, ok). If heard_lang isn't English and translation
    fails, ok is False and english_text is just the original heard_text
    (never silently misroutes a translation failure into a real command)."""
    if heard_lang == "en":
        return heard_text, True
    translated = translate_text(heard_text, heard_lang, "en")
    if translated is None:
        return heard_text, False
    return translated, True


def localize_for_speech(reply, reply_lang):
    """(text_to_speak, language_actually_spoken) - falls back to speaking
    the English reply if translation is unreachable, rather than staying
    silent. Also updates the LANGUAGE panel's 'last said' readout, in
    whichever language actually ends up spoken."""
    if not reply:
        LANG_STATE["reply_text"] = ""
        return reply, "en"
    localized = localize_reply(reply, reply_lang)
    if localized is None:
        LANG_STATE["reply_text"] = re.sub(r"\s*\|{2,3}\s*", " ", reply).strip()
        return reply, "en"
    LANG_STATE["reply_text"] = re.sub(r"\s*\|{2,3}\s*", " ", localized).strip()
    return localized, reply_lang


_load_language_settings()


# =====================================================================
# ---------- Local speech recognition (Whisper - imported lazily) ----------
# =====================================================================
# Runs entirely on your machine - no internet, no Google API dependency.
# Falls back to Google's free recognizer automatically if whisper isn't
# installed or fails, so nothing breaks if you haven't set this up yet.
# Whisper trades speed/accuracy for offline privacy. For everyday casual
# chat (short phrases like "hi", "how are you"), Google's cloud recognizer
# is noticeably faster and more accurate, so it's the default. Flip this
# to True if you specifically want offline transcription and don't mind
# the extra latency - "tiny" is the fastest local model if you do.
USE_LOCAL_WHISPER = False
WHISPER_MODEL_SIZE = "tiny"  # "tiny" = fastest/least accurate, "small"/"medium" = slower/more accurate

WHISPER_AVAILABLE = False
_whisper_import_attempted = False
_whisper_model_cache = None
whisper = None


def _ensure_whisper():
    global _whisper_import_attempted, WHISPER_AVAILABLE, whisper
    if _whisper_import_attempted:
        return WHISPER_AVAILABLE
    _whisper_import_attempted = True
    try:
        import whisper as _whisper
        whisper = _whisper
        WHISPER_AVAILABLE = True
    except Exception as e:
        print("Whisper import error (pip install openai-whisper, plus ffmpeg on your system):", e)
    return WHISPER_AVAILABLE


def _load_whisper_model():
    global _whisper_model_cache
    if _whisper_model_cache is not None:
        return _whisper_model_cache
    if not _ensure_whisper():
        return None
    try:
        print(f"Loading Whisper model ({WHISPER_MODEL_SIZE})... this only happens once per run.")
        _whisper_model_cache = whisper.load_model(WHISPER_MODEL_SIZE)
    except Exception as e:
        print("Whisper model load error:", e)
        _whisper_model_cache = None
    return _whisper_model_cache


def _audiodata_to_whisper_array(audio_data):
    """Convert speech_recognition's AudioData into the 16kHz mono float32
    numpy array Whisper expects, using only stdlib (wave) + numpy - no
    extra audio-processing dependency needed beyond whisper itself."""
    import numpy as np
    wav_bytes = audio_data.get_wav_data(convert_rate=16000, convert_width=2)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio_np = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return audio_np


def transcribe_locally(audio_data):
    """Returns transcribed text, or None if Whisper isn't available/fails
    (caller should fall back to recognize_google in that case)."""
    model = _load_whisper_model()
    if model is None:
        return None
    try:
        audio_np = _audiodata_to_whisper_array(audio_data)
        result = model.transcribe(audio_np, fp16=False, language="en")
        text = result.get("text", "").strip()
        return text if text else None
    except Exception as e:
        print("Whisper transcription error:", e)
        return None


# =====================================================================
# ---------- Conversation memory (short-term, session-only) ----------
# =====================================================================
conversation_memory = []
pending_search_query = None
pending_vision_mode_choice = False


def remember(text):
    if not text:
        return
    conversation_memory.append(text)
    if len(conversation_memory) > MAX_MEMORY:
        conversation_memory.pop(0)


# =====================================================================
# ---------- Human-sounding reply system (category-matched) ----------
# =====================================================================
REPLY_CATEGORIES = {
    "greeting": {
        "triggers": ["hello", "hi", "hey", "yo", "good morning", "good evening",
                     "good afternoon", "good night", "what's up", "whats up", "sup",
                     "howdy", "hiya", "greetings", "hey nova", "hi nova", "morning",
                     "evening nova", "good to see you"],
        "replies": [
            "Good to hear from you, sir.",
            "At your service. What shall we tackle?",
            "Systems are green. What do you need?",
            "Ready when you are, sir.",
            "Online and listening.",
            "Hello there. What can I do for you?",
            "Standing by.",
            "Good to have you back.",
        ],
    },
    "gratitude": {
        "triggers": ["thank you", "thanks", "appreciate it", "thx", "cheers",
                     "much appreciated", "you're a lifesaver", "youre a lifesaver",
                     "nice one", "thanks a lot", "thank you so much"],
        "replies": [
            "Always a pleasure, sir.",
            "Think nothing of it.",
            "That's what I'm here for.",
            "Naturally.",
            "Anytime.",
            "Glad to help.",
            "You're very welcome.",
        ],
    },
    "negative_emotion": {
        "triggers": ["sad", "tired", "exhausted", "stressed", "depressed", "upset",
                     "angry", "annoyed", "frustrated", "lonely", "anxious", "worried",
                     "hate", "bad day", "not okay", "not ok", "not great", "terrible day",
                     "miserable", "overwhelmed", "drained", "heartbroken", "feeling down",
                     "burnt out", "burned out", "rough day", "awful day", "hard day",
                     "not in a good place", "struggling"],
        "replies": [
            "That sounds like a genuinely difficult day. I'm here, for what it's worth.",
            "I'm sorry to hear that. Take your time - I'm not going anywhere.",
            "That's a great deal to carry. Shall we deal with one thing at a time?",
            "Understood. Sometimes it simply needs saying aloud.",
            "Noted, sir. Let me know if there's anything within my ability to fix.",
            "That sounds genuinely draining. I'm listening.",
            "I hear you. No need to put a brave face on it with me.",
        ],
    },
    "positive_emotion": {
        "triggers": ["happy", "great", "awesome", "excited", "amazing", "love",
                     "fantastic", "wonderful", "glad", "good day", "feeling good",
                     "thrilled", "pumped", "stoked", "on top of the world",
                     "feeling great", "in a good mood", "best day"],
        "replies": [
            "Excellent. That's the sort of report I like to hear.",
            "Splendid. What's brought this on?",
            "Good to hear it, sir.",
            "Noted, and duly pleased on your behalf.",
            "Wonderful - long may it last.",
            "That's the spirit.",
        ],
    },
    "boredom": {
        "triggers": ["bored", "nothing to do", "so bored", "boring", "nothing going on",
                     "dull", "nothing happening"],
        "replies": [
            "A rare and curable condition. Shall I find you something to do?",
            "Say the word and I'll queue up a distraction.",
            "Leave it with me, sir.",
            "I can think of a few remedies - want to hear them?",
        ],
    },
    "self_reference": {
        "triggers": ["who are you", "what are you", "what can you do", "what do you do",
                     "tell me about yourself", "introduce yourself", "what are your features",
                     "what are you capable of", "how do you work", "are you an ai",
                     "are you human", "are you real"],
        "replies": [
            "I'm Nova - your assistant for search, reminders, WhatsApp, weather, and a fair bit more. Ask away.",
            "Nova, at your service. I handle scheduling, messages, maths, searches - try me.",
            "I'm your assistant. Try asking me to check the weather, set a reminder, or look something up.",
            "I'm software, not human, but I'll do my best to be useful regardless.",
        ],
    },
    "apology_to_nova": {
        "triggers": ["sorry nova", "my bad", "apologies", "didn't mean that",
                     "didnt mean that", "my mistake", "sorry about that"],
        "replies": ["No harm done.", "Not a problem at all.", "Think nothing of it.", "All is well."],
    },
    "compliment_to_nova": {
        "triggers": ["good job", "well done", "you're smart", "youre smart", "you're amazing",
                     "youre amazing", "nice work", "you're the best", "youre the best",
                     "good bot", "great job nova", "you're great", "youre great",
                     "impressive", "nicely done"],
        "replies": ["I do try.", "You flatter me, sir.", "High praise - thank you.",
                   "I aim to please.", "Kind of you to say."],
    },
    "confusion": {
        "triggers": ["i don't understand", "i dont understand", "that doesn't make sense",
                     "that doesnt make sense", "what do you mean", "i'm confused",
                     "im confused", "come again", "say that again", "i don't get it",
                     "i dont get it"],
        "replies": ["Let me rephrase that.", "My apologies - let's try that differently.",
                   "I'll put that more plainly.", "Fair - let me try again."],
    },
    "see_you_later": {
        "triggers": ["see you later", "see you soon", "catch you later", "talk later",
                     "talk to you later", "i'm heading out", "im heading out",
                     "gotta go", "got to go", "i'm off now", "im off now"],
        "replies": ["Until next time.", "Take care, sir.", "I'll be here.", "Catch you later."],
    },
    "agreement": {
        "triggers": ["yes", "yeah", "yep", "yup", "sure", "okay", "ok", "alright",
                     "sounds good", "fine by me", "correct", "that's right", "thats right",
                     "exactly", "absolutely", "definitely", "of course", "go for it"],
        "replies": ["Understood.", "Noted.", "Consider it done.", "Very well.", "Right you are."],
    },
    "disagreement": {
        "triggers": ["no", "nope", "not really", "nah", "don't want", "dont want",
                     "no way", "not interested", "forget it", "never mind", "nevermind"],
        "replies": ["Understood, sir.", "As you wish.", "Noted - we'll leave it.", "Fair enough."],
    },
    "question": {
        "triggers": [],
        "replies": [
            "I don't have that to hand. Shall I look into it?",
            "Not within my immediate knowledge, sir - shall I search?",
            "Let me find out for you, if you'd like.",
            "I'm not certain - want me to search the web for that?",
            "That's beyond what I know offhand. Should I look it up?",
        ],
    },
    "neutral": {
        "triggers": [],
        "replies": [
            "Go on, sir, I'm listening.",
            "Noted. Anything further?",
            "Understood.",
            "Duly logged. What else?",
            "I see.",
            "Go on.",
            "Noted, sir. Anything else?",
            "Alright, noted.",
            "I'm with you - what's next?",
        ],
    },
}


def _contains_trigger(cmd, trigger):
    pattern = r"\b" + re.escape(trigger) + r"\b"
    return re.search(pattern, cmd) is not None


def _score_categories(cmd):
    scores = {}
    for name, data in REPLY_CATEGORIES.items():
        count = sum(1 for trig in data["triggers"] if _contains_trigger(cmd, trig))
        if count > 0:
            scores[name] = count
    return scores


def is_question(cmd):
    qwords = ("who", "what", "why", "how", "when", "where", "which", "whose", "whom")
    # Auxiliary-verb-led questions ("do you know...", "can you tell me...",
    # "is it going to rain") don't start with a wh-word but are still
    # questions - catching these means far more phrasings get a real
    # "shall I search?" offer instead of a generic, non-committal reply.
    aux_words = ("do", "does", "did", "can", "could", "would", "will", "should", "shall",
                 "is", "are", "was", "were", "am", "has", "have", "had", "may", "might")
    tokens = cmd.strip().split()
    if not tokens:
        return False
    if cmd.strip().endswith("?"):
        return True
    first = tokens[0].strip(",.!")
    if first in qwords or first in aux_words:
        return True
    return False


def get_human_reply(cmd):
    cmd = cmd.lower().strip()
    scores = _score_categories(cmd)

    if scores:
        best_category = max(scores, key=scores.get)
    elif is_question(cmd):
        best_category = "question"
    else:
        best_category = "neutral"

    reply = random.choice(REPLY_CATEGORIES[best_category]["replies"])

    if best_category not in ("negative_emotion",) and random.random() < 0.25:
        reply = f"{reply} {random.choice(['Anything else, sir?', 'What else can I do?', 'Go on.'])}"

    name = get_fact("name")
    if name:
        if re.search(r"\bsir\b", reply):
            # Swap the default formal address for their actual name rather
            # than stacking both ("sir, Aibel!" reads oddly).
            reply = re.sub(r"\bsir\b", name, reply)
        elif best_category == "greeting" and not reply.rstrip().endswith("?"):
            reply = reply.rstrip(".!") + f", {name}!"

    # The "question" category's canned replies all promise to search/look
    # into it ("shall I search?") - so that promise needs to be real: arm
    # pending_search_query with this same question, so that saying "yes"
    # next actually runs the search instead of just matching "agreement"'s
    # generic "Understood." and quietly dropping the offer on the floor.
    offered_search = (best_category == "question")
    return reply, offered_search


# =====================================================================
# ---------- Emotion detection (text lexicon + basic vocal prosody) ----------
# =====================================================================
# Two honest, fully offline signals, combined:
#  - TEXT: a lexicon scorer with negation and intensifier handling. Real
#    pattern-matching, not a trained classifier - it will miss sarcasm,
#    subtlety, and anything phrased without an emotion word in it.
#  - VOICE: loudness (RMS) and a rough autocorrelation pitch estimate from
#    the same audio clip speech_recognition already captured. This is a
#    coarse "how aroused does this sound" signal, not a real emotion
#    classifier - loud+high-pitched could be excitement OR anger, and this
#    can't reliably tell those apart from acoustics alone. Text wins
#    whenever it finds something definite; acoustics only nudge otherwise-
#    neutral text.
EMOTION_LEXICON = {
    "happy": ["happy", "glad", "joyful", "pleased", "delighted", "cheerful", "content", "great mood"],
    "excited": ["excited", "thrilled", "pumped", "can't wait", "stoked", "hyped", "so cool"],
    "sad": ["sad", "down", "depressed", "unhappy", "heartbroken", "miserable", "feeling low", "blue today"],
    "angry": ["angry", "furious", "pissed", "mad at", "irritated", "annoyed", "frustrated", "fed up"],
    "anxious": ["anxious", "worried", "nervous", "stressed", "overwhelmed", "scared", "afraid", "panicking"],
    "tired": ["tired", "exhausted", "drained", "sleepy", "burnt out", "burned out", "fatigued"],
}
_INTENSIFIERS = {"very": 1.5, "extremely": 2.0, "really": 1.4, "so": 1.3,
                  "incredibly": 1.8, "slightly": 0.5, "a bit": 0.6, "kind of": 0.6, "kinda": 0.6}
_NEGATION_TAIL = re.compile(r"\b(not|never|no|without|cannot|n't)\s+[\w\s]{0,12}$")


def detect_text_emotion(text):
    """Returns (label, score). label is 'neutral' if nothing scored high
    enough to be worth acting on."""
    t = " " + text.lower() + " "
    scores = {k: 0.0 for k in EMOTION_LEXICON}
    for label, phrases in EMOTION_LEXICON.items():
        for phrase in phrases:
            for m in re.finditer(re.escape(phrase), t):
                window = t[max(0, m.start() - 25):m.start()]
                weight = 1.0
                for word, mult in _INTENSIFIERS.items():
                    if word in window:
                        weight *= mult
                        break
                if _NEGATION_TAIL.search(window):
                    # "not happy" shouldn't score as strongly happy - treat it
                    # as a mild, ambiguous signal rather than double-guessing
                    # which OTHER emotion a negated word implies.
                    weight *= -0.4
                scores[label] += weight
    best = max(scores, key=scores.get)
    if scores[best] <= 0.15:
        return "neutral", 0.0
    return best, round(scores[best], 2)


def _extract_prosody(audio_data):
    """RMS loudness + a rough autocorrelation pitch estimate from the same
    AudioData speech_recognition already captured for this utterance."""
    try:
        import numpy as np
        wav_bytes = audio_data.get_wav_data(convert_rate=16000, convert_width=2)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            sr_rate = wf.getframerate()
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == 0:
            return {"rms": 0.0, "pitch_hz": None}
        rms = float(np.sqrt(np.mean(samples ** 2)))
        pitch_hz = None
        frame = samples[:sr_rate] - np.mean(samples[:sr_rate]) if len(samples) >= sr_rate else None
        if frame is not None and len(frame) > 0:
            corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
            min_lag, max_lag = int(sr_rate / 350), int(sr_rate / 80)  # human voice ~80-350Hz
            if 0 < min_lag < max_lag < len(corr):
                segment = corr[min_lag:max_lag]
                peak_lag = min_lag + int(np.argmax(segment))
                if peak_lag > 0:
                    pitch_hz = sr_rate / peak_lag
        return {"rms": rms, "pitch_hz": pitch_hz}
    except Exception as e:
        print("Prosody extraction error (non-fatal):", e)
        return {"rms": 0.0, "pitch_hz": None}


def detect_vocal_arousal(prosody):
    """Coarse 'high'/'normal'/'low' energy read from loudness+pitch. No
    per-user baseline, so this is a rough average-voice calibration."""
    rms, pitch = prosody.get("rms", 0.0), prosody.get("pitch_hz")
    if rms > 0.12 or (pitch and pitch > 220):
        return "high"
    if rms < 0.02:
        return "low"
    return "normal"


# What Nova should sound like IN RESPONSE to each detected user emotion -
# mirrors positive energy, and leans calmer/softer for anything negative
# (de-escalating for anger, gentle for sadness/anxiety, rather than
# matching those energies back).
_RESPONSE_EMOTION_FOR = {
    "happy": "happy", "excited": "excited", "sad": "empathetic",
    "angry": "angry_calm", "anxious": "empathetic", "tired": "tired", "neutral": "neutral",
}

last_detected_emotion = "neutral"  # read by the GUI for a small transparency readout


def detect_emotion(text, audio_data=None):
    """Combines text + (optional) acoustic signal into one label from
    EMOTION_LEXICON's keys plus 'neutral'. Updates last_detected_emotion
    as a side effect for the GUI to display."""
    global last_detected_emotion
    label, _score = detect_text_emotion(text)
    if label == "neutral" and audio_data is not None:
        arousal = detect_vocal_arousal(_extract_prosody(audio_data))
        if arousal == "high":
            label = "excited"  # ambiguous - could be anger; text found nothing definite either way
        elif arousal == "low":
            label = "tired"
    last_detected_emotion = label
    return label


def response_emotion_for(user_emotion):
    return _RESPONSE_EMOTION_FOR.get(user_emotion, "neutral")


# =====================================================================
# ---------- Conversational memory tricks (analogies, mnemonics, diagrams) ----------
# =====================================================================
# Honest scope: this is a curated template library, not a model generating
# novel analogies on the fly. It only adds an analogy when the topic
# actually matches one Nova has a genuinely good one for - a mediocre
# analogy forced onto every topic would hurt understanding, not help it.
ANALOGY_TEMPLATES = {
    ("electricity", "voltage", "current", "circuit"):
        "think of it like water in pipes - voltage is the water pressure, current is how much water is flowing, and resistance is how narrow the pipe is.",
    ("internet", "network", "router", "packet", "ip address"):
        "think of it like the postal system - your data gets split into packets, each one is an envelope with an address on it, and routers are the sorting offices passing it along.",
    ("database", "sql", "table", "query"):
        "think of it like a giant filing cabinet - a table is a drawer, a row is a folder in that drawer, and a query is telling an assistant exactly which folders to pull out.",
    ("dna", "gene", "chromosome"):
        "think of it like a cookbook - the DNA is the whole book, a chromosome is one chapter, and a gene is a single recipe in it.",
    ("photosynthesis",):
        "think of it like a solar-powered kitchen - the plant's leaves are solar panels catching sunlight, water and carbon dioxide are the ingredients, and sugar is the meal it cooks up.",
    ("compound interest", "investing", "interest rate"):
        "think of it like a snowball rolling downhill - it starts small, but the longer it rolls, the more it picks up, and the bigger each new layer gets.",
    ("cpu", "processor", "ram", "memory (computer)"):
        "think of it like a chef in a kitchen - the CPU is the chef doing the work, RAM is the counter space for what they're using right now, and storage is the pantry for everything else.",
    ("encryption", "cryptography", "cipher"):
        "think of it like a lockbox - anyone can see the box, but only someone with the right key can open it and read what's inside.",
    ("api", "endpoint", "rest api"):
        "think of it like a restaurant menu - you don't go into the kitchen yourself, you tell the waiter (the API) what you want, and it brings back the result.",
}


def find_analogy(topic_text):
    t = topic_text.lower()
    for keywords, analogy in ANALOGY_TEMPLATES.items():
        if any(kw in t for kw in keywords):
            return analogy
    return None


def generate_list_mnemonic(items):
    """3+ short items -> a simple first-letter memory aid. Returns None for
    anything too long/short to make a clean mnemonic from (being honest
    that not everything mnemonics well beats forcing a bad one)."""
    words = [i.strip() for i in items if i.strip()]
    if len(words) < 3 or len(words) > 8:
        return None
    initials = [w[0].upper() for w in words if w]
    if len(set(initials)) < len(initials) * 0.6:  # too many repeats to be a useful memory aid
        return None
    return "an easy way to remember these: " + "-".join(initials) + " (" + ", ".join(words) + ")"


def _extract_list_items(text):
    """Best-effort pull of a comma/'and'-separated list out of a sentence,
    e.g. '...produces oxygen, glucose, and water' -> ['oxygen','glucose','water'].
    Conservative on purpose: returns [] rather than guessing at a list that
    might not really be one."""
    m = re.search(r":\s*([^.]+)\.", text) or re.search(r"(?:are|include[s]?|:)\s+([^.]+)\.", text)
    if not m:
        return []
    chunk = m.group(1)
    items = [x.strip() for x in re.split(r",|\band\b", chunk) if x.strip()]
    return items if 3 <= len(items) <= 8 else []


def enrich_explanation(topic, explanation):
    """Appends an analogy and/or mnemonic to an explanation WHEN Nova has a
    genuinely good one - never fabricates a forced comparison for topics
    outside the curated list above."""
    if not explanation:
        return explanation
    extra = []
    analogy = find_analogy(topic + " " + explanation)
    if analogy:
        extra.append(analogy)
    mnemonic = generate_list_mnemonic(_extract_list_items(explanation))
    if mnemonic:
        extra.append(mnemonic)
    if not extra:
        return explanation
    return explanation + "|| " + " || ".join(extra)


# ---------- Simple auto-diagram (real tkinter canvas, not a mockup) ----------
DIAGRAM_TRIGGER_RE = re.compile(
    r"\b(?:show me a diagram of|show a diagram of|draw a diagram of|diagram of|"
    r"diagram for|visualize|visualise)\s+(.+)", re.IGNORECASE)


def _split_into_diagram_steps(text, topic):
    """Turns an explanation into 2-6 short boxes for a flow diagram. Prefers
    an explicit list if one is found; otherwise falls back to splitting on
    sentences, since a plain paragraph has no real 'steps' to diagram."""
    items = _extract_list_items(text)
    if items:
        return items[:6]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    steps = [s if len(s) <= 70 else s[:67] + "..." for s in sentences[:6]]
    return steps


def _create_diagram_window(root, title, steps):
    win = tk.Toplevel(root)
    win.title(f"Nova - Diagram: {title}")
    win.configure(bg=COLOR_BG)
    box_w, box_h, gap, pad = 320, 70, 36, 24
    canvas_h = pad * 2 + len(steps) * box_h + max(0, len(steps) - 1) * gap
    canvas_w = box_w + pad * 2
    win.geometry(f"{canvas_w}x{canvas_h}")
    win.resizable(False, False)
    canvas = tk.Canvas(win, width=canvas_w, height=canvas_h, bg=COLOR_BG, highlightthickness=0)
    canvas.pack()
    y = pad
    for i, step in enumerate(steps):
        canvas.create_rectangle(pad, y, pad + box_w, y + box_h, outline=COLOR_CYAN, width=2, fill=COLOR_PANEL)
        canvas.create_text(pad + box_w / 2, y + box_h / 2, text=step, fill=COLOR_TEXT,
                            width=box_w - 20, font=("Segoe UI", 10), justify="center")
        if i < len(steps) - 1:
            canvas.create_line(pad + box_w / 2, y + box_h, pad + box_w / 2, y + box_h + gap,
                                fill=COLOR_CYAN, width=2, arrow=tk.LAST)
        y += box_h + gap


def render_flow_diagram(title, steps):
    """Schedules the Toplevel to be built on the GUI's own thread (Tkinter
    isn't thread-safe, and this is normally called from the background
    listener thread). Returns False - no window shown - if the GUI isn't
    running (e.g. the unused tray-only entry point)."""
    if _gui_root_ref is None:
        return False
    try:
        _gui_root_ref.after(0, lambda: _create_diagram_window(_gui_root_ref, title, steps))
        return True
    except Exception as e:
        print("Diagram render error:", e)
        return False


def show_topic_diagram(topic):
    explanation = explain_topic(topic)
    if not explanation:
        return f"I couldn't find enough on {topic} to put together a diagram."
    steps = _split_into_diagram_steps(explanation, topic)
    if len(steps) < 2:
        return f"That doesn't break into clear steps, but here's what I found: {explanation}"
    if render_flow_diagram(topic, steps):
        return f"I've put a {len(steps)}-step diagram of {topic} on screen."
    return f"I can't open a diagram window right now, but here's what I found: {explanation}"


# ---------- Search confirmation helpers ----------
AFFIRM_WORDS = ("yes", "yeah", "yep", "yup", "yea", "sure", "ok", "okay", "go ahead", "please do",
                "do it", "affirmative", "correct", "absolutely", "definitely", "of course",
                "do that", "sounds good", "that works", "please do that", "go for it")
DECLINE_WORDS = ("no", "nope", "nah", "not now", "don't want", "dont want", "negative", "no way",
                 "never mind", "nevermind", "don't bother", "dont bother", "not interested",
                 "forget it", "no thanks", "no thank you")


def is_affirmative(cmd):
    return any(_contains_trigger(cmd, w) for w in AFFIRM_WORDS)


def is_decline(cmd):
    return any(_contains_trigger(cmd, w) for w in DECLINE_WORDS)


# =====================================================================
# ---------- App launching ----------
# =====================================================================
APP_PATHS = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "youtube": "https://www.youtube.com",
    "bing": "https://www.bing.com",
    "google": "https://www.google.com",
    "explorer": "explorer",
    "settings": "ms-settings:",
    "minecraft": r"C:\Program Files (x86)\Minecraft Launcher\MinecraftLauncher.exe",
    "copilot": rf"C:\Users\{USER}\AppData\Local\Programs\Copilot\Copilot.exe",
    "spotify": rf"C:\Users\{USER}\AppData\Roaming\Spotify\Spotify.exe",
}


def _expand(path):
    if not path:
        return path
    return os.path.expandvars(os.path.expanduser(path))


def open_with_edge_or_default(url):
    """Open a URL: try the local Edge install first (Edge ships on every
    Windows machine by default and its native search engine is Bing),
    then fall back to whatever the system's actual default browser is."""
    edge_path = _expand(APP_PATHS.get("edge", ""))
    if edge_path and os.path.exists(edge_path):
        try:
            subprocess.Popen([edge_path, url], shell=False)
            return True
        except Exception as e:
            print("Failed to open URL with Edge:", e)
    try:
        webbrowser.open(url)
        return True
    except Exception as e:
        print("Fallback browser open failed:", e)
        return False


def open_kochi_map():
    """Opens the bundled Kochi Intelligence Map - the interactive zone-by-
    zone weather / seismic / air-quality / volcanic-risk explorer - in the
    browser. Works both as a plain script (file sits next to nova.py) and
    packaged (file sits next to Nova.exe, via resource_path)."""
    if not os.path.exists(KOCHI_MAP_FILE):
        return False, "I can't find the Kochi map file - kochi_intelligence_map.html should be next to Nova.exe."
    url = "file:///" + os.path.abspath(KOCHI_MAP_FILE).replace("\\", "/")
    ok = open_with_edge_or_default(url)
    return ok, ("Opening the Kochi Intelligence Map." if ok else "I couldn't open the Kochi map.")


def open_app(name):
    key = name.lower().strip()
    if key in ("google chrome", "chrome browser"):
        key = "chrome"
    if key in ("file explorer", "fileexplorer"):
        key = "explorer"
    if key == "youtube":
        return open_with_edge_or_default(APP_PATHS.get("youtube"))
    if key in APP_PATHS:
        target = _expand(APP_PATHS[key])
        if target == "explorer":
            try:
                subprocess.Popen(["explorer"], shell=False)
                return True
            except Exception as e:
                print("Explorer open error:", e)
                return False
        if target == "ms-settings:" or target.startswith("ms-settings"):
            try:
                subprocess.Popen(["start", "ms-settings:"], shell=True)
                return True
            except Exception as e:
                print("Settings open error:", e)
                return False
        if isinstance(target, str) and target.startswith("http"):
            return open_with_edge_or_default(target)
        if os.path.exists(target):
            try:
                subprocess.Popen([target], shell=False)
                return True
            except Exception as e:
                print("Executable open error:", e)
                try:
                    subprocess.Popen(target, shell=True)
                    return True
                except Exception as e2:
                    print("Shell fallback failed:", e2)
                    return False
        else:
            try:
                subprocess.Popen(target, shell=True)
                return True
            except Exception as e:
                print(f"App path not found and shell launch failed for '{target}':", e)
                return False
    print("App key not recognized:", key)
    return False


# =====================================================================
# ---------- Search + explain (Wikipedia summary via stdlib only) ----------
# =====================================================================
def explain_topic(query):
    """Find the best-matching Wikipedia article for the query, then return its
    summary. (The summary endpoint needs an exact title, so raw spoken queries
    used to 404.)"""
    try:
        hdr = {"User-Agent": "NovaAssistant/1.0"}
        q = urllib.parse.quote(query.strip().rstrip("?"))
        search_url = ("https://en.wikipedia.org/w/api.php?action=opensearch"
                      f"&search={q}&limit=1&format=json")
        with urllib.request.urlopen(urllib.request.Request(search_url, headers=hdr), timeout=5) as resp:
            titles = json.loads(resp.read().decode("utf-8"))[1]
        if not titles:
            return None
        title = urllib.parse.quote(titles[0].replace(" ", "_"))
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("extract")
    except Exception as e:
        print("explain_topic error:", e)
        return None


def search_and_explain(query):
    clean_query = query.strip().rstrip("?")

    open_result = {"success": False}

    def _open_browser():
        open_result["success"] = open_bing_search_fast(clean_query)

    open_thread = threading.Thread(target=_open_browser, daemon=True)
    open_thread.start()

    explanation = explain_topic(clean_query)  # runs concurrently with the browser opening

    open_thread.join(timeout=5)  # don't let a slow browser launch hold up the reply forever

    if explanation:
        remember_topic(clean_query)
        return f"Here's what I found: {enrich_explanation(clean_query, explanation)}"
    if open_result["success"]:
        return f"I've opened a search for '{clean_query}' so you can check the details."
    return f"I tried to search for '{clean_query}', but I couldn't open a browser."


# =====================================================================
# ---------- Productive Mode ----------
# =====================================================================
# Maths (below) is always on regardless of this setting - this mode only
# changes what happens to a general QUESTION: instead of reading back a
# list of search results and waiting for "click the first one", it skips
# straight to the single most relevant page.
PRODUCTIVE_MODE_ACTIVE = False


def set_productive_mode(on):
    global PRODUCTIVE_MODE_ACTIVE
    PRODUCTIVE_MODE_ACTIVE = bool(on)


def productive_web_redirect(query):
    """Search the web and open the single most relevant (top) result,
    instead of reading back a results list and waiting for a follow-up."""
    clean_query = query.strip().rstrip("?")
    results, error = run_search("bing", clean_query)  # Bing, not Google - see parse_search_command
    if error or not results:
        # Still give a real answer rather than nothing: fall back to the
        # normal Wikipedia-summary-plus-best-effort-browser flow.
        return search_and_explain(clean_query)
    ok, message = click_last_result(1)
    if ok:
        remember_topic(clean_query)
        return f"Productive mode: {message}"
    return search_and_explain(clean_query)


# ---------- Maths solving (sympy, lazily imported) ----------
SYMPY_AVAILABLE = False
_sympy_import_attempted = False
sp = None
_math_parse_expr = None
_math_transformations = None


def _ensure_sympy():
    global _sympy_import_attempted, SYMPY_AVAILABLE, sp, _math_parse_expr, _math_transformations
    if _sympy_import_attempted:
        return SYMPY_AVAILABLE
    _sympy_import_attempted = True
    try:
        import sympy as _sp
        from sympy.parsing.sympy_parser import (
            parse_expr, standard_transformations,
            implicit_multiplication_application, convert_xor,
        )
        sp = _sp
        _math_parse_expr = parse_expr
        _math_transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
        SYMPY_AVAILABLE = True
    except Exception as e:
        print("sympy import error (pip install sympy):", e)
    return SYMPY_AVAILABLE


MATH_KEYWORDS = (
    "solve", "simplify", "factor", "expand", "derivative", "differentiate",
    "integral", "integrate", "limit of", "square root", "cube root", "sqrt",
    "logarithm", "log of", "ln of", "gcd of", "lcm of", "factorial",
    "quadratic", "equation", "calculate", "compute", "percent of", "percentage of",
)


def looks_like_math(cmd):
    """Heuristic gate for Productive Mode: does this look like a maths
    problem rather than a general question? Errs toward 'yes' on anything
    with digits and an operator, since a false positive just means
    solve_math_question hands back its 'try phrasing it like...' message."""
    low = cmd.lower()
    if any(k in low for k in MATH_KEYWORDS):
        return True
    if re.search(r"\d", low) and re.search(r"[+\-*/^=]", low):
        return True
    if re.search(r"\d", low) and re.search(r"\b(plus|minus|times|divided|squared|cubed)\b", low):
        return True
    return False


_MATH_FILLER_PATTERNS = [
    r"^\s*(please\s+)?(can you\s+)?(solve|calculate|compute|find|work out|figure out)\s+",
    r"^\s*what(?:'s| is)\s+",
    r"\s*for me\s*$",
    r"\?\s*$",
]
_SPOKEN_OPS = [
    (r"\bplus\b", " + "),
    (r"\bminus\b", " - "),
    (r"\bmultiplied by\b", " * "),
    (r"\btimes\b", " * "),
    (r"\bdivided by\b", " / "),
    (r"\bover\b", " / "),
    (r"\bto the power of\b", " ** "),
    (r"\braised to\b", " ** "),
    (r"\bsquared\b", " ** 2"),
    (r"\bcubed\b", " ** 3"),
    (r"\bequals\b", " = "),
    (r"\bis equal to\b", " = "),
    (r"\bequal to\b", " = "),
]


def _strip_filler(text):
    t = text.strip()
    for pat in _MATH_FILLER_PATTERNS:
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()
    return t


def _spoken_to_expr(text):
    t = " " + text.lower() + " "
    # Simple "square/cube root of <number>" - anything more nested should
    # be typed directly as sqrt(...)/cbrt(...), which the parser handles.
    t = re.sub(r"square root of\s+([\d.]+)", r"sqrt(\1)", t)
    t = re.sub(r"cube root of\s+([\d.]+)", r"cbrt(\1)", t)
    for pattern, repl in _SPOKEN_OPS:
        t = re.sub(pattern, repl, t)
    t = t.replace("^", "**")
    return re.sub(r"\s+", " ", t).strip()


def _pretty_math(text):
    """sympy prints powers as ** and Eq(...) - show it the way people write it."""
    text = re.sub(r"Eq\((.+?), (.+?)\)", r"\1 = \2", text)
    return text.replace("**", "^").replace("*", " \u00d7 ")


def _solve_math_raw(raw_text):
    """Solves arithmetic, algebra (including systems), calculus (derivatives,
    integrals - indefinite and definite, limits), factoring/expanding/
    simplifying, gcd/lcm, factorials and percentages, with an explanation
    of the method used. Backed by sympy, a real symbolic maths engine, so
    it isn't limited to a fixed set of problem templates."""
    if not _ensure_sympy():
        return "I need the 'sympy' library for maths - run: pip install sympy"

    text = raw_text.strip().rstrip("?.! ")
    low = text.lower()

    def P(expr_str):
        expr_str = expr_str.replace("^", "**")
        return _math_parse_expr(
            expr_str, transformations=_math_transformations,
            local_dict={"sqrt": sp.sqrt, "cbrt": sp.cbrt, "ln": sp.log,
                        "log": sp.log, "pi": sp.pi, "e": sp.E},
        )

    try:
        # ---- Derivatives ----
        m = re.search(r"(?:derivative of|differentiate)\s+(.+)", low)
        if m:
            body = m.group(1)
            wrt = "x"
            wm = re.search(r"with respect to (\w+)", body)
            if wm:
                wrt = wm.group(1)
                body = body[:wm.start()]
            expr = P(_spoken_to_expr(_strip_filler(body)))
            var = sp.Symbol(wrt)
            result = sp.simplify(sp.diff(expr, var))
            return (
                f"f({wrt}) = {expr}\n"
                f"Differentiate term by term with respect to {wrt}, applying the power, "
                f"product, sum and chain rules as needed.\n"
                f"f'({wrt}) = {result}"
            )

        # ---- Integrals ----
        m = re.search(r"(?:integral of|integrate)\s+(.+)", low)
        if m:
            body = m.group(1)
            wrt = "x"
            lower_b = upper_b = None
            bm = re.search(r"from ([\-\w.]+) to ([\-\w.]+)", body)
            if bm:
                lower_b, upper_b = bm.group(1), bm.group(2)
                body = body[:bm.start()]
            wm = re.search(r"with respect to (\w+)", body)
            if wm:
                wrt = wm.group(1)
                body = body[:wm.start()]
            body = re.sub(r"\bd" + re.escape(wrt) + r"\b", "", body)
            var = sp.Symbol(wrt)
            expr = P(_spoken_to_expr(_strip_filler(body)))
            if lower_b is not None:
                result = sp.integrate(expr, (var, P(lower_b), P(upper_b)))
                return (
                    f"Integral of {expr} d{wrt}, from {lower_b} to {upper_b}\n"
                    f"Find an antiderivative, then apply the Fundamental Theorem of Calculus: "
                    f"evaluate it at the upper bound minus the lower bound.\n"
                    f"Result: {result}"
                )
            result = sp.integrate(expr, var)
            return (
                f"Integral of {expr} d{wrt}\n"
                f"Find an antiderivative using the standard integration rules "
                f"(power rule, substitution, etc. as needed).\n"
                f"Result: {result} + C"
            )

        # ---- Limits ----
        m = re.search(r"limit of (.+?) as (\w+) (?:approaches|goes to|tends to|->)\s*(.+)", low)
        if m:
            body, var_name, point = m.group(1), m.group(2), m.group(3).strip()
            expr = P(_spoken_to_expr(_strip_filler(body)))
            var = sp.Symbol(var_name)
            if "infinity" in point:
                point_expr = sp.oo if not point.startswith("-") else -sp.oo
            else:
                point_expr = P(point)
            result = sp.limit(expr, var, point_expr)
            return (
                f"limit as {var_name} -> {point} of {expr}\n"
                f"Substitute the approach value; if that's indeterminate, simplify the expression "
                f"or apply L'Hopital's rule as needed.\n"
                f"Result: {result}"
            )

        # ---- Factor / Expand / Simplify ----
        for kw, fn, verb in (("factor", sp.factor, "Factoring"),
                              ("expand", sp.expand, "Expanding"),
                              ("simplify", sp.simplify, "Simplifying")):
            if re.match(r"^" + kw + r"\b", low):
                body = re.sub(r"^" + kw + r"\b", "", low).strip()
                expr = P(_spoken_to_expr(_strip_filler(body)))
                result = fn(expr)
                return f"{verb}: {expr}\nResult: {result}"

        # ---- GCD / LCM ----
        m = re.search(r"(gcd|lcm|greatest common divisor|least common multiple) of ([\d\s,and]+)", low)
        if m:
            is_gcd = "gcd" in m.group(1) or "greatest" in m.group(1)
            nums = [int(n) for n in re.findall(r"\d+", m.group(2))]
            if len(nums) >= 2:
                result = nums[0]
                for nxt in nums[1:]:
                    result = sp.gcd(result, nxt) if is_gcd else sp.lcm(result, nxt)
                kind = "GCD" if is_gcd else "LCM"
                return f"{kind} of {', '.join(map(str, nums))} = {result}"

        # ---- Factorial ----
        m = re.search(r"(\d+)\s*factorial|factorial of\s*(\d+)", low)
        if m:
            n_val = int(m.group(1) or m.group(2))
            result = sp.factorial(n_val)
            return f"{n_val}! = {n_val} \u00d7 {n_val - 1} \u00d7 ... \u00d7 1 = {result}"

        # ---- Percentage ("20 percent of 150") ----
        m = re.search(r"([\d.]+)\s*percent(?:age)?\s*of\s*([\d.]+)", low)
        if m:
            pct, whole = float(m.group(1)), float(m.group(2))
            result = pct / 100 * whole
            return f"{pct}% of {whole}\n= ({pct} \u00f7 100) \u00d7 {whole}\n= {result:g}"

        # ---- Equation(s): anything with "=", or an explicit "solve" ----
        if "=" in text or low.startswith("solve"):
            body = re.sub(r"^(please\s+)?(can you\s+)?solve( for [a-z])?\s*", "", text, flags=re.IGNORECASE)
            eqs, symbols_found = [], set()
            for part in re.split(r"\band\b|;", body):
                part = _spoken_to_expr(part.strip())
                if not part:
                    continue
                if "=" in part:
                    lhs, rhs = part.split("=", 1)
                    eq = sp.Eq(P(lhs), P(rhs))
                else:
                    eq = sp.Eq(P(part), 0)
                eqs.append(eq)
                symbols_found |= eq.free_symbols
            if eqs:
                symbols_found = sorted(symbols_found, key=lambda s: s.name)
                lines = [f"Equation{'s' if len(eqs) > 1 else ''}: " + "; ".join(sp.sstr(e) for e in eqs)]
                if len(eqs) == 1 and len(symbols_found) == 1:
                    var = symbols_found[0]
                    diff = sp.expand(eqs[0].lhs - eqs[0].rhs)
                    if diff.is_polynomial(var):
                        poly = sp.Poly(diff, var)
                        if poly.degree() == 1:
                            a, b = poly.all_coeffs()
                            lines.append(f"Move the constant to the other side: {a}{var} = {-b}")
                            lines.append(f"Divide both sides by {a}: {var} = {sp.nsimplify(-b / a)}")
                        elif poly.degree() == 2:
                            a, b, c = poly.all_coeffs()
                            disc = sp.expand(b ** 2 - 4 * a * c)
                            lines.append(f"Quadratic form: ({a}){var}\u00b2 + ({b}){var} + ({c}) = 0")
                            lines.append(f"Quadratic formula: {var} = (-b \u00b1 \u221a(b\u00b2-4ac)) / 2a")
                            lines.append(f"Discriminant = ({b})\u00b2 - 4({a})({c}) = {disc}")
                    else:
                        lines.append(f"Solving for {var} using standard algebraic methods.")
                elif len(eqs) > 1:
                    lines.append(f"Solving the system for {', '.join(str(s) for s in symbols_found)}.")
                solutions = sp.solve(eqs, symbols_found, dict=True) if symbols_found else sp.solve(eqs)
                if solutions:
                    sol_text = " or ".join(", ".join(f"{k} = {v}" for k, v in d.items()) for d in solutions)
                    lines.append(f"Solution: {sol_text}")
                else:
                    lines.append("No solution found (or infinitely many / no real solutions).")
                return _pretty_math("\n".join(lines))

        # ---- Fallback: plain expression -> evaluate or symbolically simplify ----
        cleaned = _spoken_to_expr(_strip_filler(text))
        expr = P(cleaned)
        if expr.free_symbols:
            return _pretty_math(f"Expression: {expr}\nSimplified: {sp.simplify(expr)}")
        shown = _math_parse_expr(cleaned.replace("^", "**"), transformations=_math_transformations,
                                 local_dict={"sqrt": sp.sqrt, "cbrt": sp.cbrt, "ln": sp.log,
                                             "log": sp.log, "pi": sp.pi, "e": sp.E}, evaluate=False)
        exact = sp.nsimplify(sp.simplify(expr))
        note = ("Order of operations (PEMDAS): brackets and powers first, then multiplication "
                "and division, then addition and subtraction.\n")
        if exact.is_Integer or exact.is_Rational:
            return _pretty_math(f"{shown}\n{note}Answer: {exact}")
        return _pretty_math(f"{shown}\n{note}Exact: {exact}\nApproximately: {sp.N(expr, 10)}")

    except Exception as e:
        print("solve_math_question error:", e)
        return ("I couldn't parse that as a maths problem. Try something like "
                "'solve 2x + 3 = 7', 'derivative of x^3 + 2x', "
                "'integrate x^2 from 0 to 1', or just an expression like '(4+5)*3/2'.")


def solve_math_question(raw_text):
    result = _solve_math_raw(raw_text)
    return result if result.startswith("I ") else _pretty_math(result)


# =====================================================================
# ---------- Kochi Area Weather (Open-Meteo, no API key needed) ----------
# =====================================================================
KOCHI_AREAS = {
    "Fort Kochi": (9.9639, 76.2427), "Mattancherry": (9.9580, 76.2590),
    "Willingdon Island": (9.9560, 76.2696), "Ernakulam": (9.9816, 76.2999),
    "Marine Drive": (9.9769, 76.2789), "Kaloor": (9.9950, 76.3040),
    "Panampilly Nagar": (9.9560, 76.3010), "Vyttila": (9.9680, 76.3190),
    "Thoppumpady": (9.9370, 76.2660), "Kumbalanghi": (9.9000, 76.2900),
    "Maradu": (9.9330, 76.3230), "Thrippunithura": (9.9450, 76.3510),
    "Mulanthuruthy": (9.9000, 76.3800), "Palarivattom": (10.0060, 76.3080),
    "Edappally": (10.0261, 76.3084), "Kakkanad": (10.0159, 76.3419),
    "Infopark": (10.0100, 76.3630), "Kalamassery": (10.0530, 76.3160),
    "Eloor": (10.0700, 76.2940), "Vypin": (10.0000, 76.2170),
    "Cherai": (10.1408, 76.1778), "North Paravur": (10.1360, 76.2200),
    "Aluva": (10.1004, 76.3570), "Angamaly": (10.1960, 76.3860),
    "Perumbavoor": (10.1140, 76.4780), "Piravom": (9.8730, 76.4980),
    "Muvattupuzha": (9.9894, 76.5790), "Kothamangalam": (10.0600, 76.6320),
}
KOCHI_DEFAULT_AREA = "Ernakulam"

_WMO_CODES = {
    0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 80: "Rain showers",
    81: "Heavy showers", 82: "Violent showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}

# Shared between the voice command and the dashboard panel; the GUI polls it.
WEATHER_STATE = {"area": KOCHI_DEFAULT_AREA, "data": None, "error": None,
                 "updated": None, "loading": False}


def find_kochi_area(text):
    """Longest area name found in the text wins ('marine drive' before 'drive')."""
    low = text.lower()
    for name in sorted(KOCHI_AREAS, key=len, reverse=True):
        if name.lower() in low:
            return name
    if "tripunithura" in low:
        return "Thrippunithura"
    return None


def fetch_area_weather(area):
    lat, lon = KOCHI_AREAS[area]
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
           "precipitation,weather_code,wind_speed_10m"
           "&daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min"
           "&timezone=Asia%2FKolkata&forecast_days=1")
    req = urllib.request.Request(url, headers={"User-Agent": "NovaAssistant/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    cur, daily = raw["current"], raw["daily"]
    return {
        "temp": cur["temperature_2m"], "feels": cur["apparent_temperature"],
        "humidity": cur["relative_humidity_2m"], "wind": cur["wind_speed_10m"],
        "rain_now": cur["precipitation"],
        "condition": _WMO_CODES.get(cur["weather_code"], "Unsettled"),
        "rain_chance": (daily["precipitation_probability_max"] or [0])[0],
        "high": daily["temperature_2m_max"][0], "low": daily["temperature_2m_min"][0],
    }


def refresh_weather_async(area=None):
    """Fetch in a worker thread so neither the GUI nor the voice loop blocks."""
    if area:
        WEATHER_STATE["area"] = area
    if WEATHER_STATE["loading"]:
        return
    WEATHER_STATE["loading"] = True

    def _work():
        try:
            # loop so an area picked mid-fetch isn't left showing the old one
            while True:
                target = WEATHER_STATE["area"]
                WEATHER_STATE["data"] = fetch_area_weather(target)
                WEATHER_STATE["error"] = None
                WEATHER_STATE["updated"] = time.strftime("%H:%M")
                if WEATHER_STATE["area"] == target:
                    break
        except Exception as e:
            print("weather fetch error:", e)
            WEATHER_STATE["error"] = "Couldn't reach the weather service."
        finally:
            WEATHER_STATE["loading"] = False

    threading.Thread(target=_work, daemon=True).start()


def kochi_weather_reply(area):
    WEATHER_STATE["area"] = area
    try:
        d = fetch_area_weather(area)
    except Exception as e:
        print("weather fetch error:", e)
        return f"I couldn't reach the weather service for {area} right now."
    WEATHER_STATE["data"], WEATHER_STATE["error"] = d, None
    WEATHER_STATE["updated"] = time.strftime("%H:%M")
    reply = (f"{area}: {d['condition']}, {d['temp']:.0f} degrees, feels like {d['feels']:.0f}. "
             f"Humidity {d['humidity']:.0f} percent, wind {d['wind']:.0f} kilometres per hour. "
             f"Today's high is {d['high']:.0f}, low {d['low']:.0f}, "
             f"with a {d['rain_chance']:.0f} percent chance of rain.")
    if d["rain_chance"] >= 60:
        reply += " You'll want an umbrella."
    return reply


# =====================================================================
# ---------- Screen-Aware Help (local AI via Ollama) ----------
# =====================================================================
# Nova takes a screenshot, sends it to a local vision-language model running
# in Ollama (free, private, on your own PC - nothing leaves your machine),
# and gets back an explanation / fix / solution. The same brain also answers
# plain typed or spoken questions ("ask ai ..."), which is what lets Nova
# handle worded maths problems, code errors and general questions that
# rule-based commands and the sympy solver can't.
#
# One-time setup:  install Ollama (https://ollama.com), then run
#     ollama pull llama3.2-vision
OLLAMA_URL = "http://localhost:11434"
OLLAMA_VISION_HINTS = ("vision", "llava", "qwen2.5vl", "qwen2-vl", "minicpm-v", "gemma3", "moondream", "bakllava")
SCREEN_HELP_FILE = os.path.join(DATA_DIR, "nova_screen_help.txt")
_screen_history = []  # last few exchanges, so "that didn't work" has context

SCREEN_SYSTEM_PROMPT = (
    "You are Nova's screen-help brain. You are shown a screenshot of the user's screen "
    "and their request. Work like an expert engineer and teacher.\n"
    "- Begin with ONE plain-language sentence summarising the answer (it will be read aloud).\n"
    "- If there is an error or traceback: quote the key error line, explain the ROOT CAUSE, "
    "then give numbered fix steps with exact commands or corrected code. Say what to check "
    "if the first fix does not work.\n"
    "- If there is a maths/science/homework problem: solve it step by step, explain each "
    "step and why, and state the final answer clearly.\n"
    "- If it is code, a document, a form or a web page: explain what it is and what to do next.\n"
    "- Read the text on screen carefully; do not invent text you cannot see. If something is "
    "unreadable or you are not sure, say so plainly and say what you would need.\n"
    "- Be concise and concrete. Use plain text, no markdown headings."
)


def _ollama_request(path, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_ollama_model(need_vision):
    """Returns an installed model name, or (None, reason)."""
    try:
        names = [m["name"] for m in _ollama_request("/api/tags", timeout=4).get("models", [])]
    except Exception:
        return None, ("I can't reach Ollama. Install it from ollama.com, start it, then run "
                      "'ollama pull llama3.2-vision' once.")
    if not names:
        return None, "Ollama is running but has no models. Run 'ollama pull llama3.2-vision' once."
    vision = [n for n in names if any(h in n.lower() for h in OLLAMA_VISION_HINTS)]
    if need_vision:
        if not vision:
            return None, "I need a vision model to see your screen. Run 'ollama pull llama3.2-vision' once."
        return vision[0], None
    return (vision[0] if vision else names[0]), None


def capture_screen_b64(max_width=1600, box=None):
    """box=(x0,y0,x1,y1) in physical screen pixels captures just that
    region (used by the Screen Picker below) - omit it to grab the whole
    screen, as before."""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=box).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ask_local_ai(question, with_screen=True, box=None):
    """Returns the model's full answer text (or a plain error message)."""
    model, err = _pick_ollama_model(need_vision=with_screen)
    if err:
        return err
    user_msg = {"role": "user", "content": question}
    if with_screen:
        try:
            user_msg["images"] = [capture_screen_b64(box=box)]
        except Exception as e:
            print("screen capture error:", e)
            return "I couldn't capture the screen."
    messages = [{"role": "system", "content": SCREEN_SYSTEM_PROMPT}] + _screen_history[-4:] + [user_msg]
    try:
        out = _ollama_request("/api/chat", {"model": model, "messages": messages, "stream": False,
                                            "options": {"temperature": 0.2, "num_ctx": 8192}})
        answer = out["message"]["content"].strip()
    except Exception as e:
        print("ollama chat error:", e)
        return "The local AI took too long or failed. Try again, or ask about a smaller part of the screen."
    # Keep history text-only (screenshots are big and stale after one turn)
    _screen_history.append({"role": "user", "content": question})
    _screen_history.append({"role": "assistant", "content": answer})
    del _screen_history[:-8]
    return answer


def _spoken_summary(answer):
    first = re.sub(r"[`*#]", "", answer).strip().split("\n")[0]
    sentences = re.split(r"(?<=[.!?])\s+", first)
    return " ".join(sentences[:2])[:320]


def screen_help(question, with_screen=True, box=None):
    """Full flow: ask the AI, save + open the full answer, speak the summary.
    box scopes the screenshot to one region (see the Screen Picker below) -
    omit it to look at the whole screen, as before."""
    answer = ask_local_ai(question, with_screen, box=box)
    if answer.startswith(("I can't", "I need", "Ollama", "The local AI", "I couldn't")):
        return answer
    try:
        with open(SCREEN_HELP_FILE, "w", encoding="utf-8") as f:
            f.write(f"Q: {question}\n\n{answer}\n")
        if OS_NAME.startswith("windows"):
            os.startfile(SCREEN_HELP_FILE)
        else:
            webbrowser.open("file://" + SCREEN_HELP_FILE)
    except Exception as e:
        print("couldn't open screen help file:", e)
    log_activity("Screen help: " + _spoken_summary(answer)[:120])
    return _spoken_summary(answer) + " I've opened the full steps for you."


# Whole-screen questions get the old instant behaviour (grab everything,
# answer right away). "Pointed" questions - the ones that say "this"/
# "that" - are ambiguous about WHERE on screen, so instead of guessing,
# they open the Screen Picker below and let you drag a box around the
# actual thing you mean.
SCREEN_FULL_TRIGGERS = ("my screen", "the screen", "what am i looking at")
SCREEN_POINTED_TRIGGERS = (
    "this error", "that error", "this problem", "this code", "this question",
    "still not working", "didn't work", "did not work", "still getting",
    "explain this", "explain that", "fix this", "solve this",
    "what does this mean", "what does that mean", "what's this", "what is this",
    "what is that", "what's that", "what does this do", "what does that do",
)
SCREEN_TRIGGERS = SCREEN_FULL_TRIGGERS + SCREEN_POINTED_TRIGGERS  # kept for anything checking membership
SCREEN_PICKER_PROMPT = "Sure - drag a box around what you'd like me to explain."


def _run_screen_help_async(question, with_screen=True, box=None, context_label="[screen help]"):
    """Runs the (potentially slow - a local vision model can genuinely
    take upwards of a minute) screen_help() call on its own thread and
    speaks/opens the real answer once it's ready. Every caller returns
    its own immediate spoken acknowledgment synchronously BEFORE
    starting this, so a slow answer never reads as Nova having ignored
    the request - and critically, this keeps the whole command pipeline
    free in the meantime instead of blocking every other command behind
    it, which is what made Screen Help feel unresponsive before."""
    reply = screen_help(question, with_screen=with_screen, box=box)
    add_exchange(context_label, strip_pause_markers(reply))
    reply_lang = resolve_reply_lang()
    spoken, spoken_lang = localize_for_speech(reply, reply_lang)
    log_activity(f"Nova ({LANGUAGES[spoken_lang]['name']}): {strip_pause_markers(spoken)}")
    speak_async(spoken, voice=LANGUAGES[spoken_lang]["voice"])


def _start_screen_help_async(question, with_screen=True, box=None, context_label="[screen help]"):
    threading.Thread(target=_run_screen_help_async,
                     args=(question, with_screen, box, context_label), daemon=True).start()


def handle_screen_command(cmd):
    """Returns a reply, or None if this isn't a screen-help / ask-AI request.
    The actual AI call always runs on its own thread (_start_screen_help_async)
    - this function only ever returns a quick, immediate acknowledgment, so
    a slow local model never blocks Nova from handling anything else."""
    low = cmd.lower().strip()
    if low.startswith(("ask ai ", "ask the ai ", "ask nova ai ")):
        question = re.sub(r"^ask (the |nova )?ai\s+", "", low)
        _start_screen_help_async(question, with_screen=False, context_label="[asked the local AI]")
        return "Let me think about that."

    if any(t in low for t in ("show screen picker", "show the picker", "show screen handle")):
        show_screen_picker()
        return ("There's a small magnifier on screen now - click it any time, then drag a box "
                "around whatever you want explained.")
    if any(t in low for t in ("hide screen picker", "hide the picker", "hide screen handle")):
        hide_screen_picker()
        return "Hidden. Say 'show screen picker' to bring it back."
    if any(t in low for t in ("select area", "pick area", "pick something to explain",
                               "let me point", "screen picker", "drag to explain", "draw a box")):
        activate_screen_picker()
        return SCREEN_PICKER_PROMPT

    is_pointed = any(t in low for t in SCREEN_POINTED_TRIGGERS)
    is_full = any(t in low for t in SCREEN_FULL_TRIGGERS)
    if not (is_pointed or is_full):
        return None
    # "solve this: 2x+3=7" carries its own content - leave it for the maths solver
    if looks_like_math(low) and "screen" not in low:
        return None
    if is_pointed:
        activate_screen_picker()
        return SCREEN_PICKER_PROMPT
    _start_screen_help_async(cmd, with_screen=True, context_label="[screen help]")
    return "Let me take a look at your screen."


# =====================================================================
# ---------- Screen Picker (drag-select an area for Nova to explain) ----------
# =====================================================================
# A small floating handle you can leave anywhere on screen (shown by
# default - see show_screen_picker() called from NovaGUI.__init__).
# Click it (a quick click, not a drag) to open a full-screen capture
# overlay: drag a box around whatever you want explained - an error
# dialog, an icon, a paragraph, anything - and release. Nova explains just
# that region using the same local-AI pipeline as regular Screen Help
# (screen_help() above), and speaks + opens the full answer the same way.
#
# Dragging the HANDLE itself (instead of clicking it) just moves it out of
# the way - press-and-hold-and-move vs. a plain click are told apart by
# whether the cursor actually travelled past PICKER_MOVE_THRESHOLD before
# release.
#
# Limitation: the selection overlay covers the PRIMARY monitor only
# (tkinter's winfo_screenwidth/height doesn't report the full virtual
# desktop on a multi-monitor setup). Drag within your primary display.
_picker_handle = None       # the small floating Toplevel
_picker_overlay = None      # the full-screen selection Toplevel (only while dragging a box)
_picker_drag = {"moved": False, "start_x": 0, "start_y": 0}
_picker_select = {"x0": 0, "y0": 0, "rect_id": None}
PICKER_MOVE_THRESHOLD = 6   # px of handle movement before a press counts as a drag, not a click


def _picker_default_position(root):
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    return sw - 90, sh - 140


def _create_picker_handle(root):
    global _picker_handle
    if _picker_handle is not None:
        try:
            _picker_handle.deiconify()
            _picker_handle.lift()
            return
        except Exception:
            _picker_handle = None

    x, y = _picker_default_position(root)
    win = tk.Toplevel(root)
    _picker_handle = win
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    try:
        win.attributes("-alpha", 0.92)
    except Exception:
        pass
    win.configure(bg=COLOR_CYAN)
    win.geometry(f"52x52+{x}+{y}")

    canvas = tk.Canvas(win, width=52, height=52, bg=COLOR_CYAN, highlightthickness=0, cursor="hand2")
    canvas.pack(fill="both", expand=True)
    canvas.create_oval(2, 2, 50, 50, fill=COLOR_CYAN, outline=COLOR_BG, width=2)
    canvas.create_text(26, 26, text="\U0001F50D", font=("Segoe UI Emoji", 20))

    def _on_press(event):
        _picker_drag["moved"] = False
        _picker_drag["start_x"] = event.x_root
        _picker_drag["start_y"] = event.y_root

    def _on_motion(event):
        dx = event.x_root - _picker_drag["start_x"]
        dy = event.y_root - _picker_drag["start_y"]
        if abs(dx) > PICKER_MOVE_THRESHOLD or abs(dy) > PICKER_MOVE_THRESHOLD:
            _picker_drag["moved"] = True
        if _picker_drag["moved"]:
            win.geometry(f"+{win.winfo_x() + event.x - 26}+{win.winfo_y() + event.y - 26}")

    def _on_release(_event):
        if not _picker_drag["moved"]:
            _start_picker_selection(root)

    canvas.bind("<ButtonPress-1>", _on_press)
    canvas.bind("<B1-Motion>", _on_motion)
    canvas.bind("<ButtonRelease-1>", _on_release)
    # Right-click to hide it entirely (voice: "show screen picker" brings it back)
    canvas.bind("<Button-3>", lambda e: hide_screen_picker())


def show_screen_picker():
    """Shows the floating handle. Safe to call from any thread - the
    actual Tkinter work always runs on the GUI thread via root.after()."""
    if _gui_root_ref is None:
        return False
    _gui_root_ref.after(0, lambda: _create_picker_handle(_gui_root_ref))
    return True


def hide_screen_picker():
    global _picker_handle
    _cancel_picker_selection()
    if _picker_handle is not None:
        try:
            _picker_handle.destroy()
        except Exception:
            pass
        _picker_handle = None


def activate_screen_picker():
    """Shows the handle (if hidden) and immediately opens the drag-select
    overlay - used when a spoken/typed question already implies 'this'/
    'that' rather than the whole screen, so there's no need to make the
    person click the handle first."""
    if _gui_root_ref is None:
        return False

    def _go():
        _create_picker_handle(_gui_root_ref)
        _start_picker_selection(_gui_root_ref)

    _gui_root_ref.after(0, _go)
    return True


def _cancel_picker_selection(_event=None):
    global _picker_overlay
    if _picker_overlay is not None:
        try:
            _picker_overlay.destroy()
        except Exception:
            pass
        _picker_overlay = None


def _start_picker_selection(root):
    global _picker_overlay
    if _picker_overlay is not None:
        return
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    overlay = tk.Toplevel(root)
    _picker_overlay = overlay
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    try:
        overlay.attributes("-alpha", 0.25)
    except Exception:
        pass
    overlay.configure(bg="#000000", cursor="crosshair")
    overlay.geometry(f"{sw}x{sh}+0+0")

    canvas = tk.Canvas(overlay, width=sw, height=sh, bg="#000000", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(sw // 2, 36, text="Drag a box around what you want explained \u2014 Esc to cancel",
                        fill="#ffffff", font=("Segoe UI", 13, "bold"))

    def _down(event):
        _picker_select["x0"], _picker_select["y0"] = event.x, event.y
        _picker_select["rect_id"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=COLOR_CYAN, width=2)

    def _drag(event):
        if _picker_select["rect_id"] is not None:
            canvas.coords(_picker_select["rect_id"], _picker_select["x0"], _picker_select["y0"], event.x, event.y)

    def _up(event):
        x0, y0 = _picker_select["x0"], _picker_select["y0"]
        x1, y1 = event.x, event.y
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        _cancel_picker_selection()
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return  # too small to be a real selection - probably just a click, ignore
        log_activity(f"Screen picker: explaining a selected region {box}")
        # Immediate spoken "got it" before the (potentially slow) AI call -
        # same reasoning as handle_screen_command's acknowledgment: without
        # this, a slow local model reads as Nova having ignored the drag.
        reply_lang = resolve_reply_lang()
        ack, ack_lang = localize_for_speech("Got it \u2014 let me take a look.", reply_lang)
        speak_async(ack, voice=LANGUAGES[ack_lang]["voice"])
        _start_screen_help_async(
            "Explain what is shown in this part of my screen, in plain terms. "
            "If it's an error, explain it and how to fix it. If it's a button, "
            "icon, or control, say what it does.",
            with_screen=True, box=box, context_label="[dragged a screen region for Nova to explain]")

    overlay.bind("<Escape>", _cancel_picker_selection)
    canvas.bind("<ButtonPress-1>", _down)
    canvas.bind("<B1-Motion>", _drag)
    canvas.bind("<ButtonRelease-1>", _up)
    overlay.focus_force()


# =====================================================================
# ---------- Flood Watch (Kochi monsoon early warning) ----------
# =====================================================================
# Uses Open-Meteo's 3-day rain forecast for the selected Kochi area and
# grades it with the India Met Department's daily-rainfall bands
# (heavy 64.5mm, very heavy 115.6mm). Three wet days in a row escalate the
# level, since saturated ground floods far more easily than a single storm.
# This is a rainfall-based heads-up, NOT an official flood forecast - always
# follow KSDMA / district administration warnings.
FLOOD_LEVELS = ["GREEN", "YELLOW", "ORANGE", "RED"]
FLOOD_COLORS = {"GREEN": "#39e6a6", "YELLOW": "#ffcf6b", "ORANGE": "#ff9f43", "RED": "#ff6b6b"}
FLOOD_MEANING = {
    "GREEN": "No significant rain expected.",
    "YELLOW": "Moderate to heavy rain possible - watch for waterlogging on low roads.",
    "ORANGE": "Heavy rain expected - avoid low-lying areas and keep essentials ready.",
    "RED": "Very heavy rain expected - high flood risk. Follow official warnings and be ready to move.",
}
FLOOD_STATE = {"data": None, "error": None, "loading": False, "updated": None, "alerted_level": "GREEN"}

EMERGENCY_CHECKLIST = (
    "Flood checklist. Charge phones and power banks. Keep documents and medicines in a waterproof bag. "
    "Store drinking water and dry food for three days. Move valuables and electronics upstairs. "
    "Know your nearest relief camp and higher ground. Switch off mains power if water enters. "
    "Never walk or drive through moving water. Emergency numbers: 112 for emergencies, "
    "108 for an ambulance, and 1077 for the district disaster control room."
)


def fetch_flood_risk(area):
    lat, lon = KOCHI_AREAS[area]
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           "&daily=precipitation_sum,precipitation_probability_max"
           "&timezone=Asia%2FKolkata&forecast_days=3")
    req = urllib.request.Request(url, headers={"User-Agent": "NovaAssistant/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        daily = json.loads(resp.read().decode("utf-8"))["daily"]
    mm = [float(v or 0) for v in daily["precipitation_sum"]]
    prob = [float(v or 0) for v in daily["precipitation_probability_max"]]
    peak = max(mm)
    level = 0
    if peak >= 115.6:
        level = 3
    elif peak >= 64.5:
        level = 2
    elif peak >= 15.6:
        level = 1
    if sum(1 for v in mm if v >= 15.6) == 3 and level < 3:
        level += 1
    return {"area": area, "days": list(zip(daily["time"], mm, prob)), "peak": peak,
            "total": sum(mm), "level": FLOOD_LEVELS[level]}


def flood_reply(area):
    try:
        d = fetch_flood_risk(area)
    except Exception as e:
        print("flood fetch error:", e)
        return f"I couldn't reach the forecast service to check flood risk for {area}."
    FLOOD_STATE["data"], FLOOD_STATE["error"] = d, None
    FLOOD_STATE["updated"] = time.strftime("%H:%M")
    reply = (f"Flood watch for {area}: {d['level']}. {FLOOD_MEANING[d['level']]} "
             f"Expected rain over three days is {d['total']:.0f} millimetres, "
             f"with a peak of {d['peak']:.0f} in a single day.")
    if d["level"] in ("ORANGE", "RED"):
        reply += " Say 'flood checklist' for what to prepare."
    reply += " This is a rainfall-based heads-up, not an official warning."
    return reply


def refresh_flood_async(area):
    if FLOOD_STATE["loading"]:
        return
    FLOOD_STATE["loading"] = True

    def _work():
        try:
            FLOOD_STATE["data"] = fetch_flood_risk(area)
            FLOOD_STATE["error"] = None
            FLOOD_STATE["updated"] = time.strftime("%H:%M")
        except Exception as e:
            print("flood fetch error:", e)
            FLOOD_STATE["error"] = "Forecast unavailable"
        finally:
            FLOOD_STATE["loading"] = False

    threading.Thread(target=_work, daemon=True).start()


def handle_flood_command(cmd):
    low = cmd.lower()
    if any(k in low for k in ("flood checklist", "emergency checklist", "flood kit", "flood emergency")):
        return EMERGENCY_CHECKLIST
    if any(k in low for k in ("flood", "monsoon alert", "will it flood", "waterlogging")):
        area = find_kochi_area(low) or WEATHER_STATE["area"]
        return flood_reply(area)
    return None


# =====================================================================
# ---------- Daily Briefing ----------
# =====================================================================
def daily_briefing():
    """One spoken rundown: time, weather, flood watch, battery."""
    now = time.localtime()
    part = "morning" if now.tm_hour < 12 else "afternoon" if now.tm_hour < 17 else "evening"
    area = WEATHER_STATE["area"]
    parts = [f"Good {part}. It's {time.strftime('%A, %d %B, %I:%M %p', now).replace(' 0', ' ')}."]
    try:
        w = fetch_area_weather(area)
        parts.append(f"In {area} it's {w['condition'].lower()}, {w['temp']:.0f} degrees, "
                     f"with a high of {w['high']:.0f} and a {w['rain_chance']:.0f} percent chance of rain.")
    except Exception as e:
        print("briefing weather error:", e)
        parts.append("I couldn't fetch the weather right now.")
    today_items = sched_today_summary()
    if today_items:
        parts.append(today_items)
    try:
        f = fetch_flood_risk(area)
        parts.append("No flood concerns." if f["level"] == "GREEN"
                     else f"Flood watch is {f['level']}. {FLOOD_MEANING[f['level']]}")
    except Exception:
        pass
    try:
        batt = psutil.sensors_battery()
        if batt is not None:
            parts.append(f"Battery is at {batt.percent:.0f} percent"
                         + (" and charging." if batt.power_plugged else ". You may want to plug in."
                            if batt.percent < 30 else "."))
    except Exception:
        pass
    return " ".join(parts)


def handle_briefing_command(cmd):
    low = cmd.lower()
    if any(k in low for k in ("briefing", "brief me", "what's my day", "whats my day", "catch me up")):
        return daily_briefing()
    return None


# =====================================================================
# ---------- Self-Healing System (digital immune system) ----------
# =====================================================================
# A background thread that watches the PC and repairs what it safely can:
#   * CPU pinned by one app      -> lowers that app's priority so the PC stays responsive
#   * RAM nearly full            -> trims idle memory from every process (safe, Windows)
#   * Memory leaks               -> spots processes whose memory climbs steadily for
#                                   minutes; cleans Nova's own leaks itself
#   * Disk nearly full           -> deletes old files from the temp folder
#   * Broken drivers (Windows)   -> rescans devices, then names anything still failing
#   * Apps "Not Responding"      -> flagged for closing
# Safety rules: it never touches system-critical processes or Nova itself, and it
# never force-closes an app without asking ("say 'fix it'") unless you set
# SELF_HEAL_AUTO_KILL = True - because closing an app can lose unsaved work.
import gc
import shutil

SELF_HEAL_ENABLED = True
SELF_HEAL_AUTO_KILL = False
HEAL_SAMPLE_SEC = 10
HEAL_CPU_LIMIT = 90          # % average over ~1 minute counts as "pinned"
HEAL_RAM_LIMIT = 88          # % RAM used before memory is trimmed
HEAL_LEAK_MB = 300           # steady growth over the window that counts as a leak
HEAL_LEAK_WINDOW_SEC = 300
HEAL_PROTECTED = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe",
    "fontdrvhost.exe", "audiodg.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
    "msmpeng.exe", "searchindexer.exe", "init", "systemd", "kernel_task",
}
HEAL_STATE = {"status": "HEALTHY", "events": deque(maxlen=40), "fixes": 0, "pending": None,
              "cpu": 0.0, "ram": 0.0, "healing_until": 0.0}
_heal_stop = threading.Event()
_heal_thread = None
_heal_cpu_samples = deque(maxlen=6)
_heal_rss = {}          # pid -> deque[(time, rss_bytes)]
_heal_cooldowns = {}    # key -> earliest next action time
_heal_known_driver_ids = set()


def _heal_event(kind, msg, fixed=False, speak=False):
    HEAL_STATE["events"].append((time.strftime("%H:%M"), kind, msg))
    log_activity(f"Self-heal [{kind}]: {msg}")
    if fixed:
        HEAL_STATE["fixes"] += 1
        HEAL_STATE["healing_until"] = time.time() + 60
    if speak:
        speak_async(msg)


def _heal_ready(key, cooldown_sec):
    now = time.time()
    if _heal_cooldowns.get(key, 0) > now:
        return False
    _heal_cooldowns[key] = now + cooldown_sec
    return True


def _heal_is_protected(pid, name):
    if pid <= 4 or pid == os.getpid() or (name or "").lower() in HEAL_PROTECTED:
        return True
    try:
        if pid in [c.pid for c in psutil.Process(os.getpid()).children(recursive=True)]:
            return True
    except Exception:
        pass
    return False


def _heal_propose_kill(pid, name, reason):
    if SELF_HEAL_AUTO_KILL:
        _heal_event("kill", _heal_kill(pid, name), fixed=True, speak=True)
        return
    pend = HEAL_STATE["pending"]
    if pend and pend["expires"] > time.time():
        return
    HEAL_STATE["pending"] = {"pid": pid, "name": name, "reason": reason, "expires": time.time() + 180}
    _heal_event("ask", f"{name} {reason}. Say 'fix it' to close it, or 'ignore it'.", speak=True)


def _heal_kill(pid, expected_name):
    try:
        p = psutil.Process(pid)
        if p.name() != expected_name or _heal_is_protected(pid, p.name()):
            return f"I didn't close {expected_name} - it changed or is protected."
        p.terminate()
        try:
            p.wait(5)
        except psutil.TimeoutExpired:
            p.kill()
        return f"Closed {expected_name}."
    except psutil.NoSuchProcess:
        return f"{expected_name} had already closed."
    except Exception as e:
        print("heal kill error:", e)
        return f"I couldn't close {expected_name} - it may need administrator rights."


def _heal_trim_memory():
    """Windows: ask every process to give back idle memory (EmptyWorkingSet). Safe -
    apps simply page it back in if they need it."""
    if not OS_NAME.startswith("windows"):
        return 0
    import ctypes
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    trimmed = 0
    for p in psutil.process_iter(["pid"]):
        h = k32.OpenProcess(0x0400 | 0x0100, False, p.info["pid"])  # query + set quota
        if h:
            try:
                if psapi.EmptyWorkingSet(h):
                    trimmed += 1
            finally:
                k32.CloseHandle(h)
    return trimmed


def _heal_check_cpu():
    _heal_cpu_samples.append(psutil.cpu_percent(interval=None))
    if len(_heal_cpu_samples) < _heal_cpu_samples.maxlen:
        return
    if sum(_heal_cpu_samples) / len(_heal_cpu_samples) < HEAL_CPU_LIMIT:
        return
    if not _heal_ready("cpu", 300):
        return
    cores = psutil.cpu_count() or 1
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:
            pass
    time.sleep(1.5)
    ranked = []
    for p in procs:
        try:
            ranked.append((p.cpu_percent(None) / cores, p))
        except Exception:
            pass
    ranked.sort(key=lambda t: t[0], reverse=True)
    for share, p in ranked[:3]:
        try:
            name, pid = p.name(), p.pid
            if share < 30 or _heal_is_protected(pid, name):
                continue
            low = psutil.BELOW_NORMAL_PRIORITY_CLASS if OS_NAME.startswith("windows") else 10
            if p.nice() == low or (not OS_NAME.startswith("windows") and p.nice() >= 10):
                _heal_propose_kill(pid, name, f"is still using {share:.0f} percent of your CPU")
            else:
                p.nice(low)
                _heal_event("cpu", f"{name} was hogging {share:.0f}% of the CPU. I lowered its priority "
                            "so your PC stays responsive.", fixed=True, speak=True)
            return
        except Exception as e:
            print("heal cpu error:", e)
    _heal_event("cpu", "CPU is busy, but no single app is responsible - nothing to fix.")


def _heal_check_memory(force=False):
    vm = psutil.virtual_memory()
    if not force and vm.percent < HEAL_RAM_LIMIT:
        return
    if not _heal_ready("ram", 300) and not force:
        return
    before = vm.available
    trimmed = _heal_trim_memory()
    time.sleep(1)
    vm = psutil.virtual_memory()
    freed = max(0, vm.available - before) / (1024 ** 2)
    if trimmed:
        _heal_event("memory", f"RAM was at high usage. I trimmed idle memory from {trimmed} processes "
                    f"and freed about {freed:.0f} MB.", fixed=freed > 50, speak=freed > 200)
    if vm.percent >= 92:
        big = None
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                if _heal_is_protected(p.info["pid"], p.info["name"]):
                    continue
                if big is None or p.info["memory_info"].rss > big.info["memory_info"].rss:
                    big = p
            except Exception:
                pass
        if big is not None:
            gb = big.info["memory_info"].rss / (1024 ** 3)
            _heal_propose_kill(big.info["pid"], big.info["name"], f"is using {gb:.1f} GB of memory")


def _heal_check_leaks():
    now = time.time()
    seen = set()
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            pid = p.info["pid"]
            seen.add(pid)
            _heal_rss.setdefault(pid, deque(maxlen=80)).append((now, p.info["memory_info"].rss))
        except Exception:
            continue
    for pid in list(_heal_rss):
        if pid not in seen:
            del _heal_rss[pid]
            continue
        hist = list(_heal_rss[pid])
        if len(hist) < 8 or hist[-1][0] - hist[0][0] < HEAL_LEAK_WINDOW_SEC:
            continue
        growth_mb = (hist[-1][1] - hist[0][1]) / (1024 ** 2)
        ups = sum(1 for a, b in zip(hist, hist[1:]) if b[1] >= a[1])
        if growth_mb < HEAL_LEAK_MB or ups / (len(hist) - 1) < 0.85:
            continue
        if not _heal_ready(f"leak{pid}", 1800):
            continue
        try:
            name = psutil.Process(pid).name()
        except Exception:
            continue
        if pid == os.getpid():
            del _screen_history[:]
            gc.collect()
            _heal_event("leak", f"Nova's own memory grew {growth_mb:.0f} MB. I cleaned up its caches.",
                        fixed=True)
        elif not _heal_is_protected(pid, name):
            _heal_propose_kill(pid, name, f"looks like it has a memory leak (grew {growth_mb:.0f} MB in "
                               f"{(hist[-1][0] - hist[0][0]) / 60:.0f} minutes)")
            _heal_rss[pid].clear()


def _heal_clean_temp():
    freed, cutoff = 0, time.time() - 2 * 86400
    for root, _dirs, files in os.walk(tempfile.gettempdir()):
        for f in files:
            path = os.path.join(root, f)
            try:
                if os.path.getmtime(path) < cutoff:
                    size = os.path.getsize(path)
                    os.remove(path)
                    freed += size
            except Exception:
                pass  # in use / no permission - leave it alone
    return freed / (1024 ** 2)


def _heal_check_disk(force=False):
    drive = (os.environ.get("SystemDrive", "C:") + "\\") if OS_NAME.startswith("windows") else "/"
    usage = shutil.disk_usage(drive)
    free_pct, free_gb = usage.free / usage.total * 100, usage.free / (1024 ** 3)
    if not force and free_pct >= 10 and free_gb >= 5:
        return
    if not _heal_ready("disk", 3600) and not force:
        return
    freed = _heal_clean_temp()
    free_gb += freed / 1024
    msg = f"Disk was running low. I cleared {freed:.0f} MB of old temp files."
    if free_gb < 5:
        msg += f" Only {free_gb:.1f} GB is free - consider uninstalling apps you don't use."
    _heal_event("disk", msg, fixed=freed > 50, speak=free_gb < 5)


def _heal_run_ps(command, timeout=30):
    flags = 0x08000000 if OS_NAME.startswith("windows") else 0  # CREATE_NO_WINDOW
    out = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                         text=True, timeout=timeout, creationflags=flags)
    return out.stdout.strip()


def _heal_driver_errors():
    raw = _heal_run_ps("Get-PnpDevice -PresentOnly -Status ERROR | "
                       "Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress")
    if not raw:
        return []
    data = json.loads(raw)
    return [data] if isinstance(data, dict) else data


def _heal_check_drivers():
    if not OS_NAME.startswith("windows"):
        return
    errors = _heal_driver_errors()
    new = [d for d in errors if d.get("InstanceId") not in _heal_known_driver_ids]
    if not new:
        return
    flags = 0x08000000
    subprocess.run(["pnputil", "/scan-devices"], capture_output=True, timeout=40, creationflags=flags)
    time.sleep(3)
    remaining = _heal_driver_errors()
    fixed = len(new) - len([d for d in remaining if d.get("InstanceId") in {n.get("InstanceId") for n in new}])
    if fixed > 0:
        _heal_event("driver", f"{fixed} device driver problem(s) cleared after a device rescan.",
                    fixed=True, speak=True)
    for d in remaining:
        _heal_known_driver_ids.add(d.get("InstanceId"))
    if remaining:
        names = ", ".join((d.get("FriendlyName") or "an unknown device") for d in remaining[:3])
        _heal_event("driver", f"Driver problem I can't fix automatically: {names}. Open Device Manager, "
                    "right-click it and choose Update driver, or run Windows Update.", speak=True)


def _heal_check_hung():
    if not OS_NAME.startswith("windows"):
        return
    out = subprocess.run(["tasklist", "/FI", "STATUS eq NOT RESPONDING", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        name, pid = parts[0].strip('"'), int(parts[1])
        if not _heal_is_protected(pid, name) and _heal_ready(f"hung{pid}", 1800):
            _heal_propose_kill(pid, name, "has stopped responding")
            return


def _heal_tick(tick):
    HEAL_STATE["cpu"] = psutil.cpu_percent(interval=None)
    HEAL_STATE["ram"] = psutil.virtual_memory().percent
    checks = [(1, _heal_check_cpu), (1, _heal_check_memory), (3, _heal_check_leaks),
              (6, _heal_check_hung), (30, _heal_check_disk), (90, _heal_check_drivers)]
    for every, fn in checks:
        if tick % every == (3 if every == 90 else 0):
            try:
                fn()
            except Exception as e:
                print(f"self-heal {fn.__name__} error:", e)
    pend = HEAL_STATE["pending"]
    if pend and pend["expires"] < time.time():
        HEAL_STATE["pending"] = None
    if time.time() < HEAL_STATE["healing_until"]:
        HEAL_STATE["status"] = "HEALING"
    elif HEAL_STATE["pending"] or HEAL_STATE["cpu"] > 85 or HEAL_STATE["ram"] > HEAL_RAM_LIMIT:
        HEAL_STATE["status"] = "WATCHING"
    else:
        HEAL_STATE["status"] = "HEALTHY"


def _self_heal_loop():
    tick = 0
    psutil.cpu_percent(interval=None)  # prime the counter
    while not _heal_stop.is_set():
        if SELF_HEAL_ENABLED:
            try:
                _heal_tick(tick)
            except Exception as e:
                print("self-heal tick error:", e)
        tick += 1
        _heal_stop.wait(HEAL_SAMPLE_SEC)


def start_self_heal():
    global _heal_thread
    if _heal_thread is None or not _heal_thread.is_alive():
        _heal_stop.clear()
        _heal_thread = threading.Thread(target=_self_heal_loop, daemon=True)
        _heal_thread.start()


def health_report(scan=False):
    if scan:
        for fn in (lambda: _heal_check_memory(force=True), lambda: _heal_check_disk(force=True),
                   _heal_check_leaks, _heal_check_hung, _heal_check_drivers):
            try:
                fn()
            except Exception as e:
                print("health scan error:", e)
    drive = (os.environ.get("SystemDrive", "C:") + "\\") if OS_NAME.startswith("windows") else "/"
    du = shutil.disk_usage(drive)
    vm = psutil.virtual_memory()
    report = (f"System health is {HEAL_STATE['status'].lower()}. CPU {psutil.cpu_percent(interval=0.5):.0f} percent, "
              f"memory {vm.percent:.0f} percent, disk {du.free / du.total * 100:.0f} percent free. "
              f"I've made {HEAL_STATE['fixes']} automatic fix{'es' if HEAL_STATE['fixes'] != 1 else ''} this session.")
    if HEAL_STATE["events"]:
        report += " Latest: " + HEAL_STATE["events"][-1][2]
    return report


def handle_heal_command(cmd):
    global SELF_HEAL_ENABLED
    low = cmd.lower().strip().rstrip(".!")
    pend = HEAL_STATE["pending"]
    if pend and pend["expires"] > time.time():
        if low in ("fix it", "close it", "heal it", "do it", "yes fix it", "yes close it", "kill it"):
            HEAL_STATE["pending"] = None
            msg = _heal_kill(pend["pid"], pend["name"])
            _heal_event("kill", msg, fixed=msg.startswith("Closed"))
            return msg
        if low in ("ignore it", "leave it", "don't close it", "dont close it", "no leave it"):
            HEAL_STATE["pending"] = None
            _heal_cooldowns[f"hung{pend['pid']}"] = time.time() + 7200
            return f"Okay, I'll leave {pend['name']} alone."
    if any(k in low for k in ("turn off self healing", "disable self healing", "stop self healing")):
        SELF_HEAL_ENABLED = False
        return "Self-healing is paused."
    if any(k in low for k in ("turn on self healing", "enable self healing", "start self healing")):
        SELF_HEAL_ENABLED = True
        start_self_heal()
        return "Self-healing is on. I'm watching your PC in the background."
    if any(k in low for k in ("heal my pc", "scan my pc", "fix my pc", "health check", "clean up my pc",
                              "self heal", "self healing")):
        return health_report(scan=True)
    if any(k in low for k in ("system health", "pc health", "how is my pc", "how's my pc", "hows my pc",
                              "why is my pc slow", "why is my computer slow")):
        return health_report(scan=False)
    return None


# =====================================================================
# ---------- Schedule, Reminders, Stopwatch & Countdown ----------
# =====================================================================
# Everything here lives inside Nova: say or type "remind me to call mom at
# 6pm", "add standup every weekday at 9am to my schedule", "set a timer for
# 10 minutes", "start the stopwatch" - or use the SCHEDULE and TIME TOOLS
# panels. Reminders/events are saved to disk (they survive restarts), and a
# background thread speaks them the moment they're due. Anything that came
# due while Nova was closed is read out the next time it starts.
import datetime as _dt

SCHED_FILE = os.path.join(DATA_DIR, "nova_schedule.json")
SCHED_LOCK = threading.RLock()
SCHED_ITEMS = []
SCHED_STATE = {"last_alert": "", "last_alert_time": 0.0, "last_fired_id": None, "version": 0}
STOPWATCH = {"running": False, "start": 0.0, "elapsed": 0.0, "laps": []}
COUNTDOWNS = []
_sched_started = False
_sched_counter = 0
_sched_missed_report = []

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_NUMW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
         "forty five": 45, "sixty": 60, "half a": 0.5, "half an": 0.5}
_UNIT_SECS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
              "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
              "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
              "day": 86400, "days": 86400}
_DUR_RE = re.compile(
    r"\b(\d+(?:\.\d+)?|half an?|forty[- ]five|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"fifteen|twenty|thirty|forty|sixty)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b"
    r"|(\d+(?:\.\d+)?)\s?(s|m|h)\b(?![a-z])", re.I)
_CLOCK_RE = re.compile(
    r"\b(?:at\s+|@\s*)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])"
    r"|\bat\s+(\d{1,2})(?::(\d{2}))?(?![\d:]|\s*(?:seconds?|minutes?|mins?|hours?|hrs?))"
    r"|\b(\d{1,2}):(\d{2})\b"
    r"|\b(noon|midnight)\b", re.I)
_WD_PAT = "|".join(_WEEKDAYS)
_MON_PAT = "|".join(m[:3] + r"(?:" + m[3:] + r")?" if m != "may" else "may" for m in _MONTHS)


def _sched_bump():
    SCHED_STATE["version"] += 1


def _sched_save():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SCHED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SCHED_ITEMS, f, indent=1)
        os.replace(tmp, SCHED_FILE)
    except Exception as e:
        print("schedule save error:", e)


def _sched_load():
    try:
        with open(SCHED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            SCHED_ITEMS[:] = [d for d in data if isinstance(d, dict) and "when" in d and "title" in d]
    except FileNotFoundError:
        pass
    except Exception as e:
        print("schedule load error:", e)


# ---------- formatting ----------
def fmt_duration(secs):
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s or not parts:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts)


def fmt_clock(secs, tenths=True):
    secs = max(0.0, secs)
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return base + (f".{int((secs % 1) * 10)}" if tenths else "")


def describe_when(epoch):
    dt = _dt.datetime.fromtimestamp(epoch)
    today, d = _dt.date.today(), dt.date()
    t = dt.strftime("%I:%M %p").lstrip("0")
    if d == today:
        day = "today"
    elif d == today + _dt.timedelta(days=1):
        day = "tomorrow"
    elif 0 < (d - today).days < 7:
        day = dt.strftime("%A")
    else:
        day = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"
    return f"{day} at {t}"


def _short_when(epoch):
    return describe_when(epoch).replace(" at ", " ").replace("today", "Today").replace("tomorrow", "Tmrw")


_REPEAT_TEXT = {"daily": "every day", "weekdays": "every weekday", "weekly": "every week"}


# ---------- natural-language time parsing ----------
def _num_val(tok):
    tok = re.sub(r"[- ]+", " ", tok.lower()).strip()
    return _NUMW[tok] if tok in _NUMW else float(tok)


def parse_duration(text):
    """'1 hour 30 minutes', '90s', '2h30m', 'half an hour' -> (seconds, [spans])."""
    total, spans = 0.0, []
    for m in _DUR_RE.finditer(text.lower()):
        if m.group(1):
            val, unit = _num_val(m.group(1)), m.group(2)
        else:
            val, unit = float(m.group(3)), m.group(4)
        total += val * _UNIT_SECS[unit.lower()]
        spans.append(m.span())
    return total, spans


def parse_countdown_input(text):
    """Accepts '5m', '90 seconds', '1h30m', '1:30' (m:ss) or a bare number (minutes)."""
    text = text.strip().lower()
    secs, _ = parse_duration(text)
    if secs > 0:
        return secs
    m = re.fullmatch(r"(\d{1,3}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text) * 60
    return None


def _parse_clock(low):
    m = _CLOCK_RE.search(low)
    if not m:
        return None
    if m.group(8):
        return (12 if m.group(8).lower() == "noon" else 0), 0, True, m.span()
    if m.group(1):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if not (1 <= h <= 12) or mi > 59:
            return None
        return h % 12 + (12 if ap.startswith("p") else 0), mi, True, m.span()
    if m.group(4):
        h, mi = int(m.group(4)), int(m.group(5) or 0)
        if h > 23 or mi > 59:
            return None
        return h, mi, (h >= 13 or h == 0), m.span()
    h, mi = int(m.group(6)), int(m.group(7))
    if h > 23 or mi > 59:
        return None
    return h, mi, (h >= 13 or h == 0), m.span()


def _parse_day(low, today):
    """-> (date, span, is_weekday_name) or (None, None, False)."""
    m = re.search(r"\bday after tomorrow\b", low)
    if m:
        return today + _dt.timedelta(days=2), m.span(), False
    m = re.search(r"\btomorrow\b", low)
    if m:
        return today + _dt.timedelta(days=1), m.span(), False
    m = re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", low)
    if m:
        return today, m.span(), False
    m = re.search(r"\b(?:(?:on|next|this)\s+)?(" + _WD_PAT + r")\b", low)
    if m:
        ahead = (_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7
        if "next" in m.group(0) and ahead == 0:
            ahead = 7
        return today + _dt.timedelta(days=ahead), m.span(), True
    m = re.search(r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MON_PAT + r")\b", low) or \
        re.search(r"\b(" + _MON_PAT + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m:
        a, b = m.group(1), m.group(2)
        day_num, mon = (int(a), b) if a.isdigit() else (int(b), a)
        mon_idx = next(i for i, name in enumerate(_MONTHS) if name.startswith(mon[:3]))
        try:
            d = _dt.date(today.year, mon_idx + 1, day_num)
            if d < today:
                d = _dt.date(today.year + 1, mon_idx + 1, day_num)
            return d, m.span(), False
        except ValueError:
            return None, None, False
    return None, None, False


def _parse_repeat(low):
    """-> (repeat, weekday_index_or_None, span)."""
    m = re.search(r"\bevery\s+weekdays?\b|\bweekdays\b", low)
    if m:
        return "weekdays", None, m.span()
    m = re.search(r"\bevery\s*(?:day|morning|afternoon|evening|night)\b|\bdaily\b", low)
    if m:
        return "daily", None, m.span()
    m = re.search(r"\bevery\s+(" + _WD_PAT + r")\b", low)
    if m:
        return "weekly", _WEEKDAYS.index(m.group(1)), m.span()
    m = re.search(r"\bevery\s+week\b|\bweekly\b", low)
    if m:
        return "weekly", None, m.span()
    return "none", None, None


def _cut_spans(text, spans):
    # merge overlapping spans first ("every monday" and "monday" both match the same words)
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    for a, b in reversed(merged):
        text = text[:a] + " " + text[b:]
    return text


def _clean_title(text, spans, default="Reminder"):
    t = _cut_spans(text, spans)
    t = re.sub(r"\b(?:to|on|in|at|for|from)\s+(?:my\s+)?(?:schedule|calendar|reminders?)\b", " ", t, flags=re.I)
    t = re.sub(r"^\W*(?:please\s+)?(?:nova[,\s]+)?(?:remind me|set (?:a |an )?reminder|"
               r"(?:schedule|add|put|create|make|book)(?:\s+(?:an?|the))?)\b\W*(?:(?:to|that|about|for)\b)?",
               " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,.;:-")
    t = re.sub(r"^(?:in|at|on|by|for|and|to)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:in|at|on|by|for|and|to|from)$", "", t, flags=re.I).strip(" ,.;:-")
    return (t[0].upper() + t[1:]) if t else default


def parse_when(text, now=None):
    """Turns 'call mom at 6pm tomorrow' / 'in 10 minutes' / 'every monday 9am' into
    {epoch, repeat, title, past}, or None if the text contains no time at all."""
    now = now or time.time()
    now_dt = _dt.datetime.fromtimestamp(now)
    low = text.lower()
    repeat, rep_wd, rspan = _parse_repeat(low)
    spans = [rspan] if rspan else []

    # -- relative: "in 10 minutes", "after half an hour", "2 hours from now" --
    total, dspans = parse_duration(low)
    if total > 0 and repeat == "none":
        prefix = low[:dspans[0][0]]
        tail = low[dspans[-1][1]:]
        m_in = re.search(r"\b(?:in|after|within)\s*$", prefix)
        m_from = re.match(r"\s*(?:from now|later)\b", tail)
        if m_in or m_from:
            a = m_in.start() if m_in else dspans[0][0]
            b = dspans[-1][1] + (m_from.end() if m_from else 0)
            return {"epoch": now + total, "repeat": "none", "past": False,
                    "title": _clean_title(text, [(a, b)])}

    clock = _parse_clock(low)
    day, dspan, is_wd = _parse_day(low, now_dt.date())
    if clock is None and day is None and repeat == "none":
        return None
    if clock:
        spans.append(clock[3])
    if dspan:
        spans.append(dspan)
    if repeat == "weekly" and rep_wd is not None and day is None:
        day, is_wd = now_dt.date() + _dt.timedelta(days=(rep_wd - now_dt.weekday()) % 7), True

    if clock:
        hour, minute, known = clock[0], clock[1], clock[2]
        if not known:  # "at 6" - decide am/pm
            if re.search(r"\b(?:tonight|evening|afternoon)\b", low) and hour < 12:
                hour += 12
            elif re.search(r"\bmorning\b", low):
                pass
            elif day is None or day == now_dt.date():
                cands = [h for h in ([hour, hour + 12] if hour < 12 else [hour])]
                fut = [h for h in cands if _dt.datetime.combine(now_dt.date(), _dt.time(h, minute)) > now_dt]
                hour = fut[0] if fut else hour
            elif 1 <= hour <= 6:
                hour += 12
    else:
        hour, minute = 9, 0  # a day/repeat with no time -> 9am

    date = day or now_dt.date()
    dt = _dt.datetime.combine(date, _dt.time(hour, minute))
    if repeat in ("daily", "weekdays", "weekly") and day is None or (repeat != "none" and dt <= now_dt):
        while dt <= now_dt:
            dt += _dt.timedelta(days=7 if repeat == "weekly" else 1)
        if repeat == "weekdays":
            while dt.weekday() >= 5:
                dt += _dt.timedelta(days=1)
    elif day is None and dt <= now_dt:
        dt += _dt.timedelta(days=1)  # "at 8am" said at 10am -> tomorrow
    elif dt <= now_dt and is_wd:
        dt += _dt.timedelta(days=7)
    return {"epoch": dt.timestamp(), "repeat": repeat, "past": dt.timestamp() <= now,
            "title": _clean_title(text, spans)}


def _next_occurrence(epoch, repeat):
    dt = _dt.datetime.fromtimestamp(epoch)
    dt += _dt.timedelta(days=7 if repeat == "weekly" else 1)
    if repeat == "weekdays":
        while dt.weekday() >= 5:
            dt += _dt.timedelta(days=1)
    return dt.timestamp()


# ---------- reminders / schedule items ----------
def sched_add(title, when, repeat="none", kind="reminder"):
    global _sched_counter
    with SCHED_LOCK:
        _sched_counter += 1
        item = {"id": int(time.time() * 1000) + _sched_counter, "title": title, "when": when,
                "repeat": repeat, "kind": kind, "done": False, "warned": False, "created": time.time()}
        SCHED_ITEMS.append(item)
        _sched_save()
        _sched_bump()
    return item


def sched_upcoming(limit=None, day=None):
    with SCHED_LOCK:
        items = sorted((i for i in SCHED_ITEMS if not i["done"]), key=lambda i: i["when"])
    if day is not None:
        items = [i for i in items if _dt.datetime.fromtimestamp(i["when"]).date() == day]
    return items[:limit] if limit else items


def sched_delete(item_id):
    with SCHED_LOCK:
        SCHED_ITEMS[:] = [i for i in SCHED_ITEMS if i["id"] != item_id]
        _sched_save()
        _sched_bump()


def sched_clear_all():
    with SCHED_LOCK:
        n = len([i for i in SCHED_ITEMS if not i["done"]])
        SCHED_ITEMS[:] = []
        _sched_save()
        _sched_bump()
    return n


def sched_add_from_text(text, force_kind=None):
    """Shared by the voice commands and the SCHEDULE panel's entry box."""
    w = parse_when(text)
    if w is None:
        return False, "I need a time - try 'call mom at 6pm' or 'standup every weekday at 9am'."
    if w["past"]:
        return False, "That time has already passed."
    kind = force_kind or ("event" if re.search(
        r"\b(meeting|appointment|event|class|lecture|interview|exam|session|call|schedule)\b", text, re.I)
        else "reminder")
    item = sched_add(w["title"], w["epoch"], w["repeat"], kind)
    when = describe_when(item["when"])
    rep = f", repeating {_REPEAT_TEXT[w['repeat']]}" if w["repeat"] != "none" else ""
    return True, f"{w['title']} - {when}{rep}."


def snooze_last(minutes=10):
    with SCHED_LOCK:
        last = next((i for i in SCHED_ITEMS if i["id"] == SCHED_STATE["last_fired_id"]), None)
    if last is None:
        return False, "There's nothing to snooze."
    sched_add(last["title"], time.time() + minutes * 60, "none", last.get("kind", "reminder"))
    return True, f"Snoozed. I'll remind you about {last['title']} in {minutes} minutes."


def sched_summary(day=None, label="on your schedule"):
    items = sched_upcoming(day=day) if day else sched_upcoming(limit=6)
    if not items:
        return f"Nothing {label}."
    parts = [f"{i['title']} {describe_when(i['when'])}"
             + (f" ({_REPEAT_TEXT[i['repeat']]})" if i["repeat"] != "none" else "") for i in items[:6]]
    head = f"You have {len(items)} item{'s' if len(items) != 1 else ''} {label}: "
    return head + "; ".join(parts) + "."


# ---------- stopwatch ----------
def sw_elapsed():
    return STOPWATCH["elapsed"] + (time.time() - STOPWATCH["start"] if STOPWATCH["running"] else 0.0)


def sw_start():
    if not STOPWATCH["running"]:
        STOPWATCH["start"], STOPWATCH["running"] = time.time(), True
        _sched_bump()


def sw_stop():
    if STOPWATCH["running"]:
        STOPWATCH["elapsed"] = sw_elapsed()
        STOPWATCH["running"] = False
        _sched_bump()


def sw_reset():
    STOPWATCH.update(running=False, start=0.0, elapsed=0.0, laps=[])
    _sched_bump()


def sw_lap():
    if STOPWATCH["running"] or STOPWATCH["elapsed"] > 0:
        STOPWATCH["laps"].append(sw_elapsed())
        _sched_bump()
        return len(STOPWATCH["laps"]), sw_elapsed()
    return None


# ---------- countdown timers ----------
def cd_add(seconds, label=""):
    global _sched_counter
    with SCHED_LOCK:
        _sched_counter += 1
        COUNTDOWNS.append({"id": _sched_counter, "label": label, "end": time.time() + seconds,
                           "total": seconds, "done": False, "done_at": 0.0})
        _sched_bump()


def cd_active():
    return [c for c in COUNTDOWNS if not c["done"]]


def cd_clear():
    with SCHED_LOCK:
        n = len(cd_active())
        COUNTDOWNS[:] = []
        _sched_bump()
    return n


# ---------- background scheduler ----------
def _beep_alert():
    try:
        import winsound
        for f in (880, 1100, 880):
            winsound.Beep(f, 180)
    except Exception:
        pass


def _sched_alert(msg):
    SCHED_STATE["last_alert"], SCHED_STATE["last_alert_time"] = msg, time.time()
    _sched_bump()
    log_activity(msg)
    speak_async(msg)
    if OS_NAME.startswith("windows"):
        threading.Thread(target=_beep_alert, daemon=True).start()


def _sched_tick():
    now = time.time()
    alerts = []
    with SCHED_LOCK:
        changed = False
        for it in SCHED_ITEMS:
            if it["done"]:
                continue
            lead = it["when"] - now
            # heads-up 10 minutes before events that were booked well in advance
            if (it.get("kind") == "event" and not it.get("warned") and 0 < lead <= 600
                    and it["when"] - it.get("created", 0) > 660):
                it["warned"] = True
                changed = True
                alerts.append(f"Heads up: {it['title']} starts in {max(1, round(lead / 60))} minutes.")
            if lead <= 0:
                SCHED_STATE["last_fired_id"] = it["id"]
                alerts.append(f"Reminder: {it['title']}." if it.get("kind") != "event"
                              else f"{it['title']} is starting now.")
                if it["repeat"] != "none":
                    it["when"] = _next_occurrence(it["when"], it["repeat"])
                    while it["when"] <= now:
                        it["when"] = _next_occurrence(it["when"], it["repeat"])
                    it["warned"] = False
                else:
                    it["done"] = True
                changed = True
        # keep finished one-offs for the "snooze" command but drop them from disk after a day
        SCHED_ITEMS[:] = [i for i in SCHED_ITEMS if not (i["done"] and now - i["when"] > 86400)]
        if changed:
            _sched_save()
            _sched_bump()
        for c in COUNTDOWNS:
            if not c["done"] and now >= c["end"]:
                c["done"], c["done_at"] = True, now
                alerts.append(f"Time's up: {c['label']}." if c["label"] else "Your timer is done.")
                _sched_bump()
        for c in [c for c in COUNTDOWNS if c["done"] and now - c["done_at"] > 30]:
            COUNTDOWNS.remove(c)
            _sched_bump()
    for msg in alerts:
        _sched_alert(msg)


def _sched_loop():
    time.sleep(4)
    if _sched_missed_report:
        _sched_alert("While Nova was closed you missed: " + "; ".join(_sched_missed_report[:5]) + ".")
    while True:
        try:
            _sched_tick()
        except Exception as e:
            print("scheduler error:", e)
        time.sleep(1)


def start_scheduler():
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    _sched_load()
    now = time.time()
    with SCHED_LOCK:
        for it in SCHED_ITEMS:
            if it["done"] or it["when"] > now:
                continue
            if it["repeat"] != "none":
                while it["when"] <= now:
                    it["when"] = _next_occurrence(it["when"], it["repeat"])
            else:
                it["done"] = True
                if now - it["when"] < 86400:
                    _sched_missed_report.append(f"{it['title']} ({describe_when(it['when'])})")
        _sched_save()
    threading.Thread(target=_sched_loop, daemon=True).start()


# ---------- voice / typed commands ----------
def _num_words_to_int(text):
    m = re.search(r"\b(\d+)\b", text)
    return int(m.group(1)) if m else 10


def handle_schedule_command(cmd):
    low = cmd.lower().strip().rstrip("?.!")

    # ---- stopwatch ----
    if re.search(r"\bstop\s?watch\b", low):
        if re.search(r"\b(reset|clear|zero|restart)\b", low):
            sw_reset()
            if "restart" in low or "start" in low:
                sw_start()
                return "Stopwatch restarted."
            return "Stopwatch reset."
        if re.search(r"\b(lap|split)\b", low):
            lap = sw_lap()
            return (f"Lap {lap[0]}: {speak_time(lap[1])}." if lap else "Start the stopwatch first.")
        if re.search(r"\b(stop|pause|halt|freeze)\b", low):
            if not STOPWATCH["running"]:
                return f"The stopwatch isn't running. It reads {speak_time(sw_elapsed())}."
            sw_stop()
            return f"Stopped at {speak_time(sw_elapsed())}."
        if re.search(r"\b(start|begin|run|go|resume|on)\b", low):
            if STOPWATCH["running"]:
                return f"It's already running - {speak_time(sw_elapsed())} so far."
            sw_start()
            return "Stopwatch started."
        return (f"The stopwatch is at {speak_time(sw_elapsed())}"
                + (" and running." if STOPWATCH["running"] else "."))

    # ---- countdown timers ----
    if re.search(r"\b(timer|timers|countdown|count down)\b", low):
        secs, _ = parse_duration(low)
        if re.search(r"\b(cancel|clear|delete|remove|dismiss|stop)\b", low) and secs == 0:
            n = cd_clear()
            return f"Cleared {n} timer{'s' if n != 1 else ''}." if n else "There are no timers running."
        if secs > 0:
            m = re.search(r"\b(?:called|named|labell?ed|for the|to remind me (?:to|about))\s+(.+?)$", low)
            label = ""
            if m:
                label = re.sub(r"\b(?:\d+|an?|half)\b.*$", "", m.group(1)).strip(" ,.").title()
            cd_add(secs, label)
            return f"Timer set for {fmt_duration(secs)}" + (f" - {label}." if label else ".")
        active = cd_active()
        if re.search(r"\b(left|remaining|how much|how long|status|check|running)\b", low):
            if not active:
                return "There are no timers running."
            return "; ".join(f"{(c['label'] or 'Timer')}: {fmt_duration(max(0, c['end'] - time.time()))} left"
                             for c in active) + "."
        return "How long should the timer be? For example, 'set a timer for 10 minutes'."

    # ---- snooze ----
    if re.search(r"\bsnooze\b", low):
        return snooze_last(_num_words_to_int(low) if re.search(r"\d", low) else 10)[1]

    # ---- reading the schedule ----
    if (re.search(r"\b(what'?s|whats|show|list|read|tell me|any|do i have)\b.*\b(schedule|reminders?|agenda|calendar|appointments?|meetings?)\b", low)
            or re.fullmatch(r"(?:my |the )?(?:schedule|agenda|reminders|calendar|upcoming)", low)
            or re.search(r"\bwhat do i have\b|\bwhat'?s (?:on|up)(?: for)? (?:today|tomorrow)\b", low)):
        if any(k in low for k in ("launch", "rocket", "orbital")):
            return None
        today = _dt.date.today()
        if "tomorrow" in low:
            return sched_summary(today + _dt.timedelta(days=1), "for tomorrow")
        if "today" in low or "tonight" in low:
            return sched_summary(today, "for today")
        return sched_summary()

    # ---- deleting ----
    if re.search(r"\b(cancel|delete|remove|clear)\b", low) and re.search(r"\b(reminders?|schedule|events?|meetings?|appointments?)\b", low):
        if re.search(r"\b(all|everything|my schedule|my reminders|the schedule)\b", low) or re.fullmatch(r"clear (?:my )?(?:schedule|reminders)", low):
            n = sched_clear_all()
            return f"Cleared {n} item{'s' if n != 1 else ''} from your schedule."
        needle = re.sub(r"\b(cancel|delete|remove|clear|the|my|reminder|reminders|event|events|meeting|"
                        r"appointment|about|to|for|on|schedule|please)\b", " ", low)
        needle = re.sub(r"\s+", " ", needle).strip()
        hits = [i for i in sched_upcoming() if needle and needle in i["title"].lower()]
        if not hits:
            return f"I couldn't find a reminder matching '{needle}'." if needle else "Which reminder should I remove?"
        for h in hits:
            sched_delete(h["id"])
        return f"Removed {hits[0]['title']}." if len(hits) == 1 else f"Removed {len(hits)} matching items."

    # ---- adding ----
    is_remind = re.search(r"\b(remind me|set (?:a |an )?reminder|reminder (?:to|for|at|in))\b", low)
    is_sched = (re.search(r"\b(?:add|put|create|book|schedule|make)\b", low)
                and re.search(r"\b(schedule|calendar|event|meeting|appointment)\b", low)
                and not any(k in low for k in ("launch", "rocket", "orbital")))
    if is_remind or is_sched:
        ok, msg = sched_add_from_text(cmd, force_kind="reminder" if is_remind else "event")
        if ok:
            return ("Okay, I'll remind you: " if is_remind else "Added to your schedule: ") + msg
        return msg if (is_remind or "to my" in low) else None
    return None


def speak_time(secs):
    return fmt_duration(secs) if secs >= 1 else "less than a second"


def sched_today_summary():
    items = sched_upcoming(day=_dt.date.today())
    if not items:
        return ""
    return ("Today you have " + "; ".join(f"{i['title']} {describe_when(i['when']).replace('today ', '')}"
                                          for i in items[:4]) + ".")



# =====================================================================
# ---------- Multi-Agent Swarm Mode ----------
# =====================================================================
# A handful of named sub-agents, each with its own neural voice and area of
# focus. Nova's actual command pipeline (choose_action_and_reply) still
# handles every request unchanged - what an agent adds on top is:
#   (1) address one directly, by voice or by typing ("Sage, solve x^2=9"),
#       and it answers in its own voice instead of Nova's default one.
#   (2) with Swarm Mode on, Nova auto-picks the most relevant agent for
#       whatever you asked - even when you didn't name one - so different
#       kinds of requests start coming back in different voices on their own.
# Agent voices only override Nova's own voice when the reply is in English:
# an English-only edge-tts voice reading Hindi/Malayalam/etc text would
# mispronounce it badly, so non-English replies quietly keep using that
# language's own native voice instead (see agent_voice_for below).
SWARM_MODE_ACTIVE = False
_agents_introduced = set()   # which agents have already given their one-line intro this session

AGENTS = {
    "atlas": {
        "name": "Atlas", "aliases": ("atlas",), "color": "#3ff2d9",
        "voice": "en-US-GuyNeural",
        "role": "Research & search - web lookups, general questions, screen help",
        "keywords": ("search", "look up", "lookup", "google", "who is", "what is",
                     "explain", "research", "find out", "ask ai", "my screen", "the screen"),
    },
    "sage": {
        "name": "Sage", "aliases": ("sage",), "color": "#c78bff",
        "voice": "en-US-DavisNeural",
        "role": "Maths & science tutor - equations, calculus, step-by-step working",
        "keywords": ("solve", "calculate", "equation", "derivative", "integrate", "integral",
                     "simplify", "factor", "expand", "math", "maths", "square root", "percent of"),
    },
    "vega": {
        "name": "Vega", "aliases": ("vega",), "color": "#39e6a6",
        "voice": "en-AU-NatashaNeural",
        "role": "Weather & environment - forecasts, flood watch, the Kochi map",
        "keywords": ("weather", "flood", "rain", "forecast", "kochi map", "air quality", "seismic"),
    },
    "juno": {
        "name": "Juno", "aliases": ("juno",), "color": "#ffcf6b",
        "voice": "en-US-JennyNeural",
        "role": "Schedule & time - reminders, your calendar, stopwatch, timers",
        "keywords": ("remind", "reminder", "schedule", "timer", "stopwatch", "countdown",
                     "agenda", "appointment", "meeting", "briefing", "snooze"),
    },
    "echo": {
        "name": "Echo", "aliases": ("echo",), "color": "#ff9f43",
        "voice": "en-IN-PrabhatNeural",
        "role": "Messaging - WhatsApp, notifications, phone calls",
        "keywords": ("whatsapp", "notification", "notifications", "call", "phone", "dial",
                     "text message"),
    },
    "guardian": {
        "name": "Guardian", "aliases": ("guardian",), "color": "#ff6b6b",
        "voice": "en-GB-ThomasNeural",
        "role": "System health - self-healing, PC performance, diagnostics",
        "keywords": ("heal", "system health", "pc health", "scan my pc", "fix my pc",
                     "why is my pc", "why is my computer"),
    },
    "muse": {
        "name": "Muse", "aliases": ("muse",), "color": "#8fbfbf",
        "voice": "en-US-AnaNeural",
        "role": "Entertainment - DJ mode, the orbital simulator, music",
        "keywords": ("dj mode", "play some", "play music", "orbital", "launch simulation",
                     "spotify"),
    },
}


def set_swarm_mode(on):
    global SWARM_MODE_ACTIVE
    SWARM_MODE_ACTIVE = bool(on)


def _awaiting_followup():
    """True while Nova is mid-conversation waiting on a specific reply
    (dictating a WhatsApp message, confirming a call, yes/no on something).
    Agent routing is skipped in that case - the raw text must reach the
    pending flow untouched, since it might legitimately start with a word
    that's also an agent's name (dictating 'Echo, don't forget the meeting'
    as an actual WhatsApp message, for instance)."""
    return bool(pending_search_query or pending_vision_mode_choice or pending_call
                or pending_incoming_call or pending_whatsapp_voice
                or pending_notification_readout or HEAL_STATE["pending"])


def detect_addressed_agent(cmd):
    """-> (agent_key, remaining_text). remaining_text is '' if the agent was
    addressed with nothing after its name ('Atlas' on its own - a wake)."""
    text = cmd.strip()
    for key, agent in AGENTS.items():
        for alias in agent["aliases"]:
            m = re.match(r"^(?:hey|ok|okay)?\s*" + re.escape(alias) + r"\b[,:]?\s*(.*)$", text, re.I)
            if m:
                return key, m.group(1).strip()
    return None, cmd


def guess_agent_for(cmd):
    """Swarm Mode's auto hand-off: which agent best fits this request, by
    the same keyword-matching style the rest of Nova's commands use. None
    if nothing fits (control commands like 'mute' stay with Nova itself)."""
    low = cmd.lower()
    best_key, best_score = None, 0
    for key, agent in AGENTS.items():
        score = sum(1 for kw in agent["keywords"] if kw in low)
        if score > best_score:
            best_key, best_score = key, score
    if best_key:
        return best_key
    return "atlas" if is_question(low) else None


def agent_voice_for(agent_key, spoken_lang):
    if agent_key and spoken_lang == "en":
        return AGENTS[agent_key]["voice"]
    return LANGUAGES[spoken_lang]["voice"]


def agent_intro_prefix(agent_key):
    """A one-line self-intro the FIRST time an agent is addressed each
    session, so the handoff is noticeable - not repeated every single time."""
    if agent_key and agent_key not in _agents_introduced:
        _agents_introduced.add(agent_key)
        return f"{AGENTS[agent_key]['name']} here. "
    return ""


def handle_swarm_command(cmd):
    low = cmd.lower().strip().rstrip(".!?")
    if any(k in low for k in ("activate swarm mode", "turn on swarm mode",
                              "enable swarm mode", "swarm mode on", "start swarm mode")):
        set_swarm_mode(True)
        return ("Swarm mode is on. I'll hand requests off to whichever agent fits best, "
                "even when you don't name one - or address one directly any time.")
    if any(k in low for k in ("deactivate swarm mode", "turn off swarm mode",
                              "disable swarm mode", "swarm mode off", "stop swarm mode")):
        set_swarm_mode(False)
        return "Swarm mode is off. You can still address an agent directly whenever you like."
    if any(k in low for k in ("list agents", "who's in the swarm", "whos in the swarm",
                              "swarm status", "what agents", "meet the agents")):
        return " ".join(f"{a['name']}: {a['role']}." for a in AGENTS.values())
    return None


# =====================================================================
# ---------- India & World News (Google News RSS, no API key) ----------
# =====================================================================
# Real headlines, refreshed automatically every hour (and once right at
# startup), from Google News' public RSS feeds - no account, no API key,
# nothing extra to install. On top of the raw headlines, if Ollama (the
# same local AI that powers Screen-Aware Help) is available, Nova also asks
# it to weave the top stories into a short spoken-style briefing instead of
# just reading a list. Without Ollama, the headline list alone still works
# fine - it just won't be narrated as a single flowing summary.
NEWS_FEEDS = {
    "india": "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "world": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
}
NEWS_REFRESH_SEC = 3600  # hourly
NEWS_STATE = {
    "india": {"items": [], "updated": None, "error": None},
    "world": {"items": [], "updated": None, "error": None},
    "digest": None, "digest_updated": None, "loading": False,
}
_news_watch_active = False
_news_watch_thread = None


def fetch_news_feed(url, limit=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NovaAssistant)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        # Google News titles are usually "Headline - Publisher" - drop the
        # trailing publisher name since it's shown separately already.
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        desc_raw = item.findtext("description") or ""
        desc = re.sub(r"<[^>]+>", " ", desc_raw)
        desc = _html_module.unescape(re.sub(r"\s+", " ", desc)).strip()
        items.append({
            "title": _html_module.unescape(title),
            "link": (item.findtext("link") or "").strip(),
            "source": source,
            "pub": (item.findtext("pubDate") or "").strip(),
            "desc": desc,
        })
        if len(items) >= limit:
            break
    return items


def summarize_headlines_with_ai(india_items, world_items):
    """-> a short spoken-style briefing from the local AI, or None if it's
    not available (checked the same way screen_help checks it)."""
    if not india_items and not world_items:
        return None
    lines = ["Top India headlines:"] + [f"- {i['title']} ({i['source']})" for i in india_items[:8]]
    lines += ["", "Top world headlines:"] + [f"- {i['title']} ({i['source']})" for i in world_items[:6]]
    prompt = (
        "Here are real, current news headlines. Write a short spoken-style briefing "
        "(4-6 sentences) covering the most important and striking stories, grouped "
        "naturally rather than read out as a list. Don't invent any detail beyond "
        "what the headlines themselves say.\n\n" + "\n".join(lines)
    )
    answer = ask_local_ai(prompt, with_screen=False)
    if answer.startswith(("I can't", "I need", "Ollama", "The local AI", "I couldn't")):
        return None
    return answer.strip()


def refresh_news_async():
    if NEWS_STATE["loading"]:
        return
    NEWS_STATE["loading"] = True

    def _work():
        try:
            items = fetch_news_feed(NEWS_FEEDS["india"])
            NEWS_STATE["india"] = {"items": items, "updated": time.strftime("%H:%M"), "error": None}
        except Exception as e:
            print("India news fetch error:", e)
            NEWS_STATE["india"]["error"] = "Couldn't reach the news feed."
        try:
            items = fetch_news_feed(NEWS_FEEDS["world"])
            NEWS_STATE["world"] = {"items": items, "updated": time.strftime("%H:%M"), "error": None}
        except Exception as e:
            print("World news fetch error:", e)
            NEWS_STATE["world"]["error"] = "Couldn't reach the news feed."
        try:
            digest = summarize_headlines_with_ai(NEWS_STATE["india"]["items"], NEWS_STATE["world"]["items"])
            if digest:
                NEWS_STATE["digest"], NEWS_STATE["digest_updated"] = digest, time.strftime("%H:%M")
        except Exception as e:
            print("News digest error (non-fatal):", e)
        NEWS_STATE["loading"] = False

    threading.Thread(target=_work, daemon=True).start()


def _news_watch_loop():
    refresh_news_async()
    while _news_watch_active:
        time.sleep(NEWS_REFRESH_SEC)
        if _news_watch_active:
            refresh_news_async()


def start_news_watch():
    global _news_watch_active, _news_watch_thread
    if _news_watch_active:
        return
    _news_watch_active = True
    _news_watch_thread = threading.Thread(target=_news_watch_loop, daemon=True)
    _news_watch_thread.start()


def handle_news_command(cmd):
    low = cmd.lower().strip().rstrip(".!?")

    if any(k in low for k in ("refresh the news", "update the news", "refresh news", "get the latest news")):
        refresh_news_async()
        return "Refreshing the news now."

    if any(k in low for k in ("news digest", "brief me on the news", "summarize the news", "summarise the news",
                              "any big news", "any crazy news", "any insane news", "news briefing")):
        if NEWS_STATE["digest"]:
            return NEWS_STATE["digest"]
        items = NEWS_STATE["india"]["items"][:5]
        if not items:
            return "I don't have any news fetched yet - give it a moment and ask again."
        return "Here's what's making headlines: " + " ".join(f"{i['title']}." for i in items)

    if re.search(r"\b(india|indian) news\b", low) or re.search(r"\bnews (?:from|in) india\b", low):
        items = NEWS_STATE["india"]["items"][:6]
        if not items:
            return NEWS_STATE["india"]["error"] or "I don't have India news fetched yet - give it a moment."
        return "Top India headlines: " + " ".join(f"{i['title']}, from {i['source']}." for i in items)

    if re.search(r"\bworld news\b", low) or re.search(r"\bglobal news\b", low) or \
       re.search(r"\bnews (?:from|around) the world\b", low):
        items = NEWS_STATE["world"]["items"][:6]
        if not items:
            return NEWS_STATE["world"]["error"] or "I don't have world news fetched yet - give it a moment."
        return "Top world headlines: " + " ".join(f"{i['title']}, from {i['source']}." for i in items)

    if re.search(r"\b(what'?s|whats) (?:the |in the )?news\b", low) or \
       low in ("news", "the news", "headlines", "top headlines", "latest news"):
        items = NEWS_STATE["india"]["items"][:6]
        if not items:
            return "I don't have any news fetched yet - give it a moment and ask again."
        return "Top headlines: " + " ".join(f"{i['title']}." for i in items)

    return None


# =====================================================================
# ---------- Volume control (Windows pycaw, optional) ----------
# =====================================================================
VOLUME_AVAILABLE = False
if OS_NAME.startswith("windows"):
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        VOLUME_AVAILABLE = True
    except Exception as e:
        print("pycaw import error:", e)
        VOLUME_AVAILABLE = False


def _with_com(fn):
    """pycaw uses COM, which must be initialised on EVERY thread that calls it.
    Nova's commands run on worker threads, hence the CoInitialize error."""
    import comtypes
    comtypes.CoInitialize()
    try:
        return fn()
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _endpoint_volume():
    speakers = AudioUtilities.GetSpeakers()
    return cast(speakers.EndpointVolume, POINTER(IAudioEndpointVolume))


def set_volume(percent):
    if not VOLUME_AVAILABLE:
        print("pycaw not installed.")
        return False
    try:
        p = max(0, min(100, percent))
        _with_com(lambda: _endpoint_volume().SetMasterVolumeLevelScalar(p / 100.0, None))
        return True
    except Exception as e:
        print("set_volume error:", e)
        return False


def change_volume(delta_percent):
    if not VOLUME_AVAILABLE:
        print("pycaw not installed.")
        return False
    try:
        def _do():
            vol = _endpoint_volume()
            current = vol.GetMasterVolumeLevelScalar()
            new = max(0.0, min(1.0, current + delta_percent / 100.0))
            vol.SetMasterVolumeLevelScalar(new, None)
        _with_com(_do)
        return True
    except Exception as e:
        print("change_volume error:", e)
        return False


def mute_volume(on=True):
    if not VOLUME_AVAILABLE:
        print("pycaw not installed.")
        return False
    try:
        _with_com(lambda: _endpoint_volume().SetMute(1 if on else 0, None))
        return True
    except Exception as e:
        print("mute_volume error:", e)
        return False


# =====================================================================
# ---------- System control ----------
# =====================================================================
def shutdown_system():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call(["shutdown", "/s", "/t", "1"], shell=False)
        else:
            subprocess.call(["shutdown", "-h", "now"])
        return True
    except Exception as e:
        print("Shutdown error:", e)
        return False


def restart_system():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call(["shutdown", "/r", "/t", "1"], shell=False)
        else:
            subprocess.call(["shutdown", "-r", "now"])
        return True
    except Exception as e:
        print("Restart error:", e)
        return False


def wifi_on():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call('netsh interface set interface "Wi-Fi" enabled', shell=True)
            return True
        print("Wi-Fi toggle not implemented for this OS.")
        return False
    except Exception as e:
        print("Wi-Fi on error:", e)
        return False


def wifi_off():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call('netsh interface set interface "Wi-Fi" disabled', shell=True)
            return True
        print("Wi-Fi toggle not implemented for this OS.")
        return False
    except Exception as e:
        print("Wi-Fi off error:", e)
        return False


def bluetooth_on():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call('powershell -Command "Start-Service bthserv"', shell=True)
            return True
        print("Bluetooth toggle not implemented for this OS.")
        return False
    except Exception as e:
        print("Bluetooth on error:", e)
        return False


def bluetooth_off():
    try:
        if OS_NAME.startswith("windows"):
            subprocess.call('powershell -Command "Stop-Service bthserv"', shell=True)
            return True
        print("Bluetooth toggle not implemented for this OS.")
        return False
    except Exception as e:
        print("Bluetooth off error:", e)
        return False


def extract_percent(cmd):
    m = re.search(r"(\d{1,3})\s*%|\b(\d{1,3})\s*(percent|percentage)\b", cmd)
    if not m:
        return None
    for g in m.groups():
        if g and g.isdigit():
            return max(0, min(100, int(g)))
    return None


# =====================================================================
# ---------- Browser automation (Selenium - imported lazily) ----------
# =====================================================================
# These start as None/False and only get filled in by _ensure_selenium(),
# which runs the first time a browser feature is actually used. This is
# what keeps startup fast - selenium itself is light, but skipping it
# until needed avoids any surprise delay from driver-manager version checks.
SELENIUM_AVAILABLE = False
WEBDRIVER_MANAGER_AVAILABLE = False
_selenium_import_attempted = False
webdriver = By = Service = WebDriverWait = EC = None
TimeoutException = NoSuchElementException = ElementClickInterceptedException = Exception
EdgeDriverManager = None


def _ensure_selenium():
    """Microsoft Edge, not Chrome: Edge ships on every Windows machine by
    default (no separate install needed, unlike Chrome), and its native
    search engine is Bing - which lines up with parse_search_command's
    default below. Edge is Chromium-based, so Selenium support is
    first-class; this is otherwise the exact same driver-manager pattern
    Chrome automation would use."""
    global _selenium_import_attempted, SELENIUM_AVAILABLE, WEBDRIVER_MANAGER_AVAILABLE
    global webdriver, By, Service, WebDriverWait, EC
    global TimeoutException, NoSuchElementException, ElementClickInterceptedException
    global EdgeDriverManager

    if _selenium_import_attempted:
        return SELENIUM_AVAILABLE
    _selenium_import_attempted = True

    try:
        from selenium import webdriver as _webdriver
        from selenium.webdriver.common.by import By as _By
        from selenium.webdriver.edge.service import Service as _Service
        from selenium.webdriver.support.ui import WebDriverWait as _WebDriverWait
        from selenium.webdriver.support import expected_conditions as _EC
        from selenium.common.exceptions import (
            TimeoutException as _TimeoutException,
            NoSuchElementException as _NoSuchElementException,
            ElementClickInterceptedException as _ElementClickInterceptedException,
        )
        webdriver, By, Service, WebDriverWait, EC = _webdriver, _By, _Service, _WebDriverWait, _EC
        TimeoutException = _TimeoutException
        NoSuchElementException = _NoSuchElementException
        ElementClickInterceptedException = _ElementClickInterceptedException
        SELENIUM_AVAILABLE = True
    except Exception as e:
        print("Selenium import error (pip install selenium webdriver-manager):", e)

    try:
        from webdriver_manager.microsoft import EdgeChromiumDriverManager as _EdgeChromiumDriverManager
        EdgeDriverManager = _EdgeChromiumDriverManager
        WEBDRIVER_MANAGER_AVAILABLE = True
    except Exception as e:
        print("webdriver-manager import error (pip install webdriver-manager):", e)

    return SELENIUM_AVAILABLE


_driver = None
last_results = []
last_platform = None

SEARCH_TRIGGER_PHRASES = ("search for", "search", "look up", "find", "play")
PLATFORM_ALIASES = {
    "youtube": "youtube", "yt": "youtube",
    "spotify": "spotify", "bing": "bing",
    "google": "google", "chrome": "google",
}
MEDIA_KEYWORDS = ("song", "music", "video", "track", "lyrics", "album", "mv")
ORDINAL_PRIORITY = [
    ("fifth", 5), ("5th", 5), ("five", 5),
    ("fourth", 4), ("4th", 4), ("four", 4),
    ("third", 3), ("3rd", 3), ("three", 3),
    ("second", 2), ("2nd", 2), ("two", 2),
    ("first", 1), ("1st", 1),
]


def parse_search_command(cmd):
    cmd = cmd.lower().strip()
    has_trigger = any(t in cmd for t in SEARCH_TRIGGER_PHRASES)
    if not has_trigger:
        return None, None

    platform_name = None
    for alias, canonical in PLATFORM_ALIASES.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", cmd):
            platform_name = canonical
            break

    if platform_name is None:
        looks_like_media = (
            any(re.search(r"\b" + kw + r"\b", cmd) for kw in MEDIA_KEYWORDS)
            or cmd.startswith("play ")
        )
        # Bing is the default search engine (not Google): it's Edge's own
        # native engine (see _get_driver below - automation runs through
        # Edge, not Chrome), and it's also much less aggressive than
        # Google about detecting an automated browser and throwing
        # CAPTCHAs at it. Explicitly saying "search on google" still
        # works via PLATFORM_ALIASES.
        platform_name = "youtube" if looks_like_media else "bing"

    query = cmd
    for alias in PLATFORM_ALIASES:
        query = re.sub(r"\b" + re.escape(alias) + r"\b", " ", query)
    for trig in SEARCH_TRIGGER_PHRASES:
        query = query.replace(trig, " ")
    query = query.replace(" on ", " ")
    query = re.sub(r"\s+", " ", query).strip()

    return platform_name, query


def parse_click_index(cmd):
    cmd = cmd.lower().strip()
    for word, idx in ORDINAL_PRIORITY:
        if re.search(r"\b" + re.escape(word) + r"\b", cmd):
            return idx
    return 1


def describe_results(results, max_items=3):
    if not results:
        return "I didn't find anything."
    ordinals = ["first", "second", "third", "fourth", "fifth"]
    parts = []
    for i, r in enumerate(results[:max_items]):
        label = ordinals[i] if i < len(ordinals) else f"{i + 1}th"
        parts.append(f"the {label} is {r['title']}")
    return "I found a few results. " + "; ".join(parts) + ". Say 'click the first one' or similar to open it."


def _is_driver_alive(driver):
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def _build_browser_options():
    """Reduce Selenium fingerprinting so sites are less likely to flag this
    as an automated browser and throw CAPTCHAs. This helps but isn't
    bulletproof - Bing being the default (see parse_search_command) is
    still the main fix for Google specifically."""
    options = webdriver.EdgeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # "eager" = consider the page loaded once the DOM is ready, don't wait
    # for every image/script/ad to finish - meaningfully faster for every
    # browser operation, and explicit WebDriverWait calls elsewhere already
    # handle waiting for the specific elements that actually matter.
    options.page_load_strategy = "eager"
    return options


def _get_driver():
    global _driver
    if not _ensure_selenium():
        return None

    if _driver is not None:
        if _is_driver_alive(_driver):
            return _driver
        _driver = None

    try:
        options = _build_browser_options()
        if WEBDRIVER_MANAGER_AVAILABLE:
            service = Service(EdgeDriverManager().install())
            _driver = webdriver.Edge(service=service, options=options)
        else:
            _driver = webdriver.Edge(options=options)
        _driver.maximize_window()
        # Belt-and-suspenders: also hide the navigator.webdriver flag that
        # sites commonly check for directly.
        try:
            _driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception as e:
            print("Couldn't patch navigator.webdriver (non-fatal):", e)
    except Exception as e:
        print("Failed to start Edge via Selenium:", e)
        _driver = None

    return _driver


def _safe_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.3)
        element.click()
        return True
    except ElementClickInterceptedException:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception as e:
            print("Click fallback failed:", e)
            return False
    except Exception as e:
        print("Click error:", e)
        return False


def open_bing_search_fast(query):
    """
    Open a Bing search as fast as possible - no WebDriverWait, no reading
    results back. Used for the question-answering flow, where the actual
    answer comes from Wikipedia separately; the browser is just there for
    you to glance at, so there's no reason to block on it loading fully.
    """
    driver = _get_driver()
    if driver is None:
        return False
    try:
        url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
        driver.get(url)  # returns once the DOM is ready (page_load_strategy="eager")
        return True
    except Exception as e:
        print("open_bing_search_fast error:", e)
        return False


def search_youtube(query):
    global last_results, last_platform
    driver = _get_driver()
    if driver is None:
        return None, "I couldn't start the browser."

    url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.ID, "video-title")))
    except TimeoutException:
        return None, "YouTube didn't return results in time."

    elements = driver.find_elements(By.ID, "video-title")[:5]
    results = [{"title": el.get_attribute("title") or el.text, "element": el} for el in elements if el.text or el.get_attribute("title")]
    last_results, last_platform = results, "youtube"
    return results, None


def search_spotify(query):
    global last_results, last_platform
    driver = _get_driver()
    if driver is None:
        return None, "I couldn't start the browser."

    url = f"https://open.spotify.com/search/{query.replace(' ', '%20')}/tracks"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, '[data-testid="tracklist-row"]')))
    except TimeoutException:
        return None, "Spotify didn't return results in time. You may need to be logged in."

    rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="tracklist-row"]')[:5]
    results = []
    for row in rows:
        try:
            title_el = row.find_element(By.CSS_SELECTOR, '[data-testid="internal-track-link"] div')
            title = title_el.text
        except NoSuchElementException:
            title = row.text.split("\n")[0] if row.text else "Unknown track"
        results.append({"title": title, "element": row})

    last_results, last_platform = results, "spotify"
    return results, None


def search_google(query):
    global last_results, last_platform
    driver = _get_driver()
    if driver is None:
        return None, "I couldn't start the browser."

    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "h3")))
    except TimeoutException:
        return None, "Google didn't return results in time."

    headings = driver.find_elements(By.CSS_SELECTOR, "h3")[:5]
    results = []
    for h in headings:
        try:
            link = h.find_element(By.XPATH, "./ancestor::a")
            results.append({"title": h.text or link.get_attribute("href"), "element": link})
        except NoSuchElementException:
            continue

    last_results, last_platform = results, "google"
    return results, None


def search_bing(query):
    global last_results, last_platform
    driver = _get_driver()
    if driver is None:
        return None, "I couldn't start the browser."

    url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "li.b_algo h2 a")))
    except TimeoutException:
        return None, "Bing didn't return results in time."

    links = driver.find_elements(By.CSS_SELECTOR, "li.b_algo h2 a")[:5]
    results = [{"title": a.text, "element": a} for a in links if a.text]
    last_results, last_platform = results, "bing"
    return results, None


SEARCH_FUNCTIONS = {"youtube": search_youtube, "spotify": search_spotify, "google": search_google, "bing": search_bing}


def run_search(platform_name, query):
    func = SEARCH_FUNCTIONS.get(platform_name)
    if func is None:
        return None, f"I don't know how to search {platform_name}."
    try:
        return func(query)
    except Exception as e:
        print(f"run_search error ({platform_name}):", e)
        return None, "Something went wrong with the browser. Let's try that again."


def click_last_result(index):
    global last_results
    if not last_results:
        return False, "I don't have any recent search results to click."
    if index < 1 or index > len(last_results):
        return False, f"I only found {len(last_results)} results, so I can't click number {index}."

    driver = _get_driver()
    if driver is None:
        return False, "The browser isn't open."

    entry = last_results[index - 1]
    element = entry["element"]

    if last_platform == "spotify":
        try:
            element = element.find_element(By.CSS_SELECTOR, '[aria-label^="Play"]')
        except NoSuchElementException:
            pass

    ok = _safe_click(driver, element)
    if ok and last_platform == "youtube":
        start_ad_skipper()
    if ok:
        return True, f"Opening {entry['title']}."
    return False, f"I couldn't click {entry['title']}."


AD_SKIP_SELECTORS = [".ytp-ad-skip-button-modern", ".ytp-skip-ad-button", ".ytp-ad-skip-button"]
_ad_skipper_active = False
_ad_skipper_thread = None


def _ad_skipper_loop():
    global _ad_skipper_active
    driver = _get_driver()
    if driver is None:
        _ad_skipper_active = False
        return
    while _ad_skipper_active:
        try:
            for selector in AD_SKIP_SELECTORS:
                for b in driver.find_elements(By.CSS_SELECTOR, selector):
                    if b.is_displayed():
                        _safe_click(driver, b)
        except Exception:
            pass
        time.sleep(1)


def start_ad_skipper():
    global _ad_skipper_active, _ad_skipper_thread
    if _ad_skipper_active:
        return False
    _ad_skipper_active = True
    _ad_skipper_thread = threading.Thread(target=_ad_skipper_loop, daemon=True)
    _ad_skipper_thread.start()
    return True


def stop_ad_skipper():
    global _ad_skipper_active
    if not _ad_skipper_active:
        return False
    _ad_skipper_active = False
    return True


# =====================================================================
# ---------- AI DJ Mode ----------
# =====================================================================
# Honest scope: there's no Spotify/YouTube API key wired in here, so this
# isn't pulling from a real recommendation engine - it's a curated list of
# proven long-form mix searches per activity, rotated through automatically
# using the browser automation Nova already has (the same run_search /
# click_last_result plumbing "play X on youtube" uses). Each "track" is
# really a long continuous mix video, which is what makes this behave like
# a DJ queue rather than needing to manage individual song skips inside a
# video player Nova doesn't control frame-by-frame.
DJ_PLAYLISTS = {
    "studying": [
        "lofi hip hop radio beats to study to",
        "deep focus instrumental music for studying",
        "calm piano music for studying and concentration",
        "ambient study music no lyrics 2 hour mix",
        "brain food music for studying and reading",
    ],
    "gaming": [
        "epic gaming music mix no copyright",
        "high energy edm gaming playlist",
        "phonk gaming music mix",
        "dubstep gaming background music mix",
        "synthwave gaming music playlist",
    ],
    "chilling": [
        "chill lofi vibes playlist",
        "relaxing acoustic chill music mix",
        "smooth jazz chill playlist",
        "chillhop radio beats to relax to",
        "sunset chill r&b playlist",
    ],
    "workout": [
        "best workout music mix motivation",
        "high energy gym workout playlist",
        "hip hop workout mix",
        "cardio workout music mix",
    ],
    "party": [
        "party dance music mix 2020s",
        "top hits party playlist mix",
        "edm party mix",
        "hip hop party playlist",
    ],
}
DJ_ACTIVITY_ALIASES = {
    "study": "studying", "studying": "studying", "homework": "studying", "reading": "studying",
    "work": "studying", "working": "studying", "focus": "studying",
    "game": "gaming", "gaming": "gaming", "games": "gaming",
    "chill": "chilling", "chilling": "chilling", "relax": "chilling", "relaxing": "chilling",
    "sleep": "chilling", "vibing": "chilling",
    "workout": "workout", "gym": "workout", "exercise": "workout", "exercising": "workout",
    "party": "party", "partying": "party",
}

dj_state = {"active": False, "activity": None, "queue": [], "index": -1}
pending_dj_activity_choice = False


def _build_dj_queue(activity):
    queue = list(DJ_PLAYLISTS[activity])
    random.shuffle(queue)
    return queue


def dj_play_current():
    if not dj_state["queue"]:
        return "I don't have a DJ queue running."
    query = dj_state["queue"][dj_state["index"]]
    results, error = run_search("youtube", query)
    if error:
        return f"Couldn't start that mix: {error}"
    ok, message = click_last_result(1)
    if not ok:
        return f"Found a mix but couldn't start playback: {message}"
    return f"Now playing for your {dj_state['activity']} session: {query}."


def start_dj_mode(activity_raw):
    activity = DJ_ACTIVITY_ALIASES.get(activity_raw.strip().lower())
    if not activity:
        return None  # unrecognized activity - caller decides how to handle
    dj_state["active"] = True
    dj_state["activity"] = activity
    dj_state["queue"] = _build_dj_queue(activity)
    dj_state["index"] = 0
    return dj_play_current()


def dj_next_track():
    if not dj_state["active"]:
        return "DJ mode isn't running. Say 'start DJ mode for studying/gaming/chilling' first."
    dj_state["index"] += 1
    if dj_state["index"] >= len(dj_state["queue"]):
        dj_state["queue"] = _build_dj_queue(dj_state["activity"])  # reshuffle once the rotation completes
        dj_state["index"] = 0
    return dj_play_current()


def stop_dj_mode():
    if not dj_state["active"]:
        return "DJ mode wasn't running."
    dj_state["active"] = False
    activity = dj_state["activity"]
    dj_state["activity"] = None
    dj_state["queue"] = []
    dj_state["index"] = -1
    return f"Stopping DJ mode. That was your {activity} session." if activity else "DJ mode stopped."


def dj_status_reply():
    if not dj_state["active"]:
        return "DJ mode is off."
    current = dj_state["queue"][dj_state["index"]] if dj_state["queue"] else "nothing yet"
    return f"DJ mode is running in {dj_state['activity']} mode. Currently playing: {current}."


DJ_START_RE = re.compile(
    r"\b(?:start |begin |turn on )?dj mode\b(?:\s+for\s+(\w+)|\s+(\w+))?", re.IGNORECASE)
DJ_ACTIVITY_ONLY_RE = re.compile(
    r"\bplay (?:some|me)? ?(\w+) music\b|\bput on (?:some )?(\w+) music\b|"
    r"\bi'?m (\w+),?\s*play (?:some|me)?\s*music\b", re.IGNORECASE)
DJ_STOP_RE = re.compile(r"\b(?:stop|turn off|end) dj mode\b", re.IGNORECASE)
DJ_NEXT_RE = re.compile(r"\b(?:next (?:track|song|mix)|skip(?: (?:track|song|this))?|dj,? skip)\b", re.IGNORECASE)
DJ_STATUS_RE = re.compile(r"\bwhat'?s (?:playing|the dj playing)\b|\bdj status\b|\bwhat mode is dj in\b",
                           re.IGNORECASE)


def handle_dj_command(cmd):
    """Returns a spoken reply if `cmd` matched a DJ Mode command, else None."""
    global pending_dj_activity_choice

    if pending_dj_activity_choice:
        activity_word = cmd.strip().split()[0] if cmd.strip() else ""
        reply = start_dj_mode(activity_word)
        if reply:
            pending_dj_activity_choice = False
            return reply
        if is_decline(cmd):
            pending_dj_activity_choice = False
            return "No worries, DJ mode cancelled."
        return "Sorry, I've got studying, gaming, chilling, workout, or party. Which one?"

    if DJ_STOP_RE.search(cmd):
        return stop_dj_mode()
    if DJ_NEXT_RE.search(cmd) and dj_state["active"]:
        return dj_next_track()
    if DJ_STATUS_RE.search(cmd):
        return dj_status_reply()

    m = DJ_START_RE.search(cmd)
    if m:
        activity_word = m.group(1) or m.group(2)
        if activity_word:
            reply = start_dj_mode(activity_word)
            if reply:
                return reply
        pending_dj_activity_choice = True
        return "Sure - what are you doing? Studying, gaming, chilling, working out, or partying?"

    m = DJ_ACTIVITY_ONLY_RE.search(cmd)
    if m:
        activity_word = next((g for g in m.groups() if g), "")
        reply = start_dj_mode(activity_word)
        if reply:
            return reply
    return None


# =====================================================================
# ---------- Orbital Simulation Mode ----------
# =====================================================================
# Honest scope up front:
#  - ISS position is REAL, live data from Open Notify's public API (no key).
#  - Next-launch data is REAL, from The Space Devs' free Launch Library.
#  - Asteroid close-approach data is REAL, from NASA's NeoWs API (works with
#    the public DEMO_KEY, rate-limited to ~30 requests/hour/IP - get your
#    own free key at api.nasa.gov for heavier use, then say "set my nasa
#    key to ..." - stored via the existing preferences system).
#  - The globe on screen is a REAL orthographic projection (the same math
#    cartographers use for a "globe view" map) - genuine spherical trig,
#    not a literal 3D engine. tkinter can't do true 3D, so points on the
#    far side of the globe are correctly hidden by the projection math,
#    which is what actually sells the 3D look here.
#  - The rocket-ascent and asteroid-flyby visuals are ILLUSTRATIVE/schematic
#    (there's no live public telemetry feed for either), clearly distinct
#    from the ISS's genuinely tracked live position.
orbital_state = {
    "mode": None,       # "iss" | "rocket" | "asteroid" | None
    "active": False,
    "lat": 0.0, "lon": 0.0,
    "label": "", "detail": "",
    "rotation": 0.0,        # globe spin, degrees
    "asteroid_phase": 0.0,  # 0..1 progress along the schematic flyby arc
}
_orbital_lock = threading.Lock()
_orbital_poll_active = False
_orbital_poll_thread = None

EARTH_MOON_KM = 384400  # for "N times the distance to the Moon" framing


def _describe_location(lat, lon):
    """Rough coordinate readout - there's no reverse-geocoding API wired in
    here, so this is degrees/hemisphere, not a real place name."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.1f}\u00b0{ns}, {abs(lon):.1f}\u00b0{ew}"


def get_iss_position():
    """Real-time ISS lat/lon via Open Notify (no API key). Altitude
    (~408 km) and speed (~27,600 km/h) are well-documented stable averages
    presented as approximations, not live telemetry - Open Notify only
    reports position."""
    try:
        req = urllib.request.Request("http://api.open-notify.org/iss-now.json",
                                      headers={"User-Agent": "NovaAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pos = data["iss_position"]
        return float(pos["latitude"]), float(pos["longitude"])
    except Exception as e:
        print("ISS position fetch error:", e)
        return None, None


def _orbital_poll_loop():
    while _orbital_poll_active:
        lat, lon = get_iss_position()
        if lat is not None:
            with _orbital_lock:
                if orbital_state["mode"] == "iss":
                    orbital_state["lat"], orbital_state["lon"] = lat, lon
                    orbital_state["detail"] = f"{_describe_location(lat, lon)}  |  ~408 km up  |  ~27,600 km/h"
        for _ in range(100):  # ~10s between polls, in short increments so stop() is responsive
            if not _orbital_poll_active:
                return
            time.sleep(0.1)


def start_orbital_poll():
    global _orbital_poll_active, _orbital_poll_thread
    if _orbital_poll_active:
        return
    _orbital_poll_active = True
    _orbital_poll_thread = threading.Thread(target=_orbital_poll_loop, daemon=True)
    _orbital_poll_thread.start()


def stop_orbital_poll():
    global _orbital_poll_active
    _orbital_poll_active = False


def iss_reply():
    lat, lon = get_iss_position()
    if lat is None:
        return "I couldn't reach the ISS tracking service right now."
    with _orbital_lock:
        orbital_state.update(mode="iss", active=True, lat=lat, lon=lon, label="ISS",
                              detail=f"{_describe_location(lat, lon)}  |  ~408 km up  |  ~27,600 km/h")
    start_orbital_poll()
    return (f"The ISS is currently near {_describe_location(lat, lon)}, orbiting at roughly "
            f"408 kilometers up and about 27,600 kilometers per hour. Live tracker's on screen now.")


def get_next_launch():
    """Next scheduled orbital launch from Launch Library 2's free tier."""
    try:
        url = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=1&mode=list"
        req = urllib.request.Request(url, headers={"User-Agent": "NovaAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        pad = r.get("pad") or {}
        location = pad.get("location") or {}
        lat = pad.get("latitude")
        lon = pad.get("longitude")
        return {
            "name": r.get("name") or "Unknown mission",
            "net": r.get("net"),
            "rocket": ((r.get("rocket") or {}).get("configuration") or {}).get("name") or "an unnamed rocket",
            "pad_name": pad.get("name") or "an undisclosed pad",
            "location_name": location.get("name") or "",
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
        }
    except Exception as e:
        print("Launch fetch error:", e)
        return None


def rocket_reply():
    info = get_next_launch()
    if not info:
        return "I couldn't reach the launch schedule service right now."
    when_text = info["net"] or "an unannounced time"
    try:
        when_dt = time.strptime(info["net"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        when_text = time.strftime("%b %d, %H:%M UTC", when_dt)
    except Exception:
        pass
    with _orbital_lock:
        orbital_state.update(mode="rocket", active=True, lat=info["lat"] or 0.0, lon=info["lon"] or 0.0,
                              label=info["name"],
                              detail=f"{info['rocket']}  |  {info['pad_name']}  |  {when_text}")
    stop_orbital_poll()  # schedule data, not moving telemetry - no need to keep polling
    where = f", {info['location_name']}" if info["location_name"] else ""
    return (f"Next launch: {info['name']} on a {info['rocket']} from {info['pad_name']}{where}, "
            f"around {when_text}. I've marked the launch site and simulated the ascent on screen - "
            f"that ascent path is illustrative, since there's no public live telemetry feed for it.")


def get_next_asteroid_approach():
    """Nearest upcoming close-approach object from NASA's NeoWs feed."""
    api_key = get_preference("nasa_api_key", "DEMO_KEY") or "DEMO_KEY"
    today = time.strftime("%Y-%m-%d")
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() + 6 * 86400))
    url = (f"https://api.nasa.gov/neo/rest/v1/feed?start_date={today}&end_date={end}"
           f"&api_key={urllib.parse.quote(api_key)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NovaAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        neos = data.get("near_earth_objects", {})
        candidates = []
        for date, objs in neos.items():
            for o in objs:
                cad = (o.get("close_approach_data") or [{}])[0]
                try:
                    distance_km = float((cad.get("miss_distance") or {}).get("kilometers", 0) or 0)
                    velocity_kph = float((cad.get("relative_velocity") or {}).get("kilometers_per_hour", 0) or 0)
                except (TypeError, ValueError):
                    continue
                candidates.append({
                    "name": o.get("name") or "Unknown object",
                    "date": cad.get("close_approach_date") or date,
                    "distance_km": distance_km,
                    "velocity_kph": velocity_kph,
                    "hazardous": bool(o.get("is_potentially_hazardous_asteroid")),
                })
        if not candidates:
            return None
        candidates.sort(key=lambda c: c["distance_km"])
        return candidates[0]
    except Exception as e:
        print("Asteroid fetch error (DEMO_KEY is rate-limited - try again shortly, or set your own key):", e)
        return None


def asteroid_reply():
    info = get_next_asteroid_approach()
    if not info:
        return "I couldn't reach NASA's near-Earth object service right now - it may be rate-limited."
    lunar_distances = info["distance_km"] / EARTH_MOON_KM
    with _orbital_lock:
        orbital_state.update(mode="asteroid", active=True, lat=0.0, lon=0.0, label=info["name"],
                              detail=f"{info['distance_km']:,.0f} km ({lunar_distances:.1f}x lunar dist.)  |  "
                                     f"{info['velocity_kph']:,.0f} km/h",
                              asteroid_phase=0.0)
    stop_orbital_poll()
    hazard_note = ""
    if info["hazardous"]:
        hazard_note = (" It's flagged 'potentially hazardous' - that's a size/orbit watch category, "
                        "not a prediction that it will hit anything.")
    return (f"Closest upcoming approach: asteroid {info['name']}, passing at about "
            f"{info['distance_km']:,.0f} kilometers away - roughly {lunar_distances:.1f} times the "
            f"distance to the Moon - at {info['velocity_kph']:,.0f} kilometers per hour on "
            f"{info['date']}.{hazard_note} I've put a schematic flyby on screen - not to real scale, "
            f"since at that distance, Earth and the asteroid would be invisible next to each other.")


def stop_orbital_sim():
    stop_orbital_poll()
    with _orbital_lock:
        was_active = orbital_state["active"]
        orbital_state.update(mode=None, active=False, label="", detail="")
    return "Stopping orbital simulation." if was_active else "Orbital simulation wasn't running."


ORBITAL_ISS_RE = re.compile(
    r"\bwhere'?s the iss\b|\bwhere is the iss\b|\btrack the iss\b|\biss location\b|\biss position\b",
    re.IGNORECASE)
ORBITAL_ROCKET_RE = re.compile(
    r"\bsimulate (?:a |the )?rocket launch\b|\bshow (?:me )?(?:a |the )?(?:next )?rocket launch\b|"
    r"\bwhen'?s the next launch\b|\bnext (?:rocket )?launch\b", re.IGNORECASE)
ORBITAL_ASTEROID_RE = re.compile(
    r"\basteroid flyby\b|\bsimulate an? asteroid\b|\bshow (?:me )?an? asteroid\b|"
    r"\bnear[- ]?earth (?:object|asteroid)\b|\bnext asteroid\b", re.IGNORECASE)
ORBITAL_STOP_RE = re.compile(
    r"\bstop (?:the )?orbital ?(?:sim(?:ulation)?)?\b|\bstop tracking(?: the iss)?\b", re.IGNORECASE)


def handle_orbital_command(cmd):
    """Returns a spoken reply if `cmd` matched an Orbital Sim command, else None."""
    if ORBITAL_STOP_RE.search(cmd):
        return stop_orbital_sim()
    if ORBITAL_ISS_RE.search(cmd):
        return iss_reply()
    if ORBITAL_ROCKET_RE.search(cmd):
        return rocket_reply()
    if ORBITAL_ASTEROID_RE.search(cmd):
        return asteroid_reply()
    return None


SPOTIFY_PLAYPAUSE_SELECTOR = '[data-testid="control-button-playpause"]'


def play_video():
    driver = _get_driver()
    if driver is None:
        return False, "The browser isn't open."
    try:
        video = driver.find_element(By.TAG_NAME, "video")
        driver.execute_script("arguments[0].play();", video)
        return True, "Playing."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("play_video error:", e)
    try:
        button = driver.find_element(By.CSS_SELECTOR, SPOTIFY_PLAYPAUSE_SELECTOR)
        if "play" in (button.get_attribute("aria-label") or "").lower():
            _safe_click(driver, button)
        return True, "Playing."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("Spotify play error:", e)
    return False, "I couldn't find anything to play."


def pause_video():
    driver = _get_driver()
    if driver is None:
        return False, "The browser isn't open."
    try:
        video = driver.find_element(By.TAG_NAME, "video")
        driver.execute_script("arguments[0].pause();", video)
        return True, "Paused."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("pause_video error:", e)
    try:
        button = driver.find_element(By.CSS_SELECTOR, SPOTIFY_PLAYPAUSE_SELECTOR)
        if "pause" in (button.get_attribute("aria-label") or "").lower():
            _safe_click(driver, button)
        return True, "Paused."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("Spotify pause error:", e)
    return False, "I couldn't find anything to pause."


def toggle_play_pause():
    driver = _get_driver()
    if driver is None:
        return False, "The browser isn't open."
    try:
        video = driver.find_element(By.TAG_NAME, "video")
        is_paused = driver.execute_script("return arguments[0].paused;", video)
        if is_paused:
            driver.execute_script("arguments[0].play();", video)
            return True, "Playing."
        else:
            driver.execute_script("arguments[0].pause();", video)
            return True, "Paused."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("video toggle error:", e)
    try:
        button = driver.find_element(By.CSS_SELECTOR, SPOTIFY_PLAYPAUSE_SELECTOR)
        _safe_click(driver, button)
        return True, "Toggled playback."
    except NoSuchElementException:
        pass
    except Exception as e:
        print("Spotify toggle error:", e)
    return False, "I couldn't find anything playing to control."


def seek_video(seconds_delta):
    driver = _get_driver()
    if driver is None:
        return False, "The browser isn't open."
    try:
        video = driver.find_element(By.TAG_NAME, "video")
        driver.execute_script(f"arguments[0].currentTime += {seconds_delta};", video)
        direction = "forward" if seconds_delta > 0 else "backward"
        return True, f"Skipped {direction}."
    except NoSuchElementException:
        return False, "I couldn't find a video to seek."
    except Exception as e:
        print("seek_video error:", e)
        return False, "Couldn't seek the video."


def close_browser():
    global _driver, last_results, last_platform
    if _driver is not None:
        try:
            _driver.quit()
        except Exception as e:
            print("Error closing browser:", e)
        _driver = None
    last_results = []
    last_platform = None


# =====================================================================
# ---------- Camera vision (cv2/mediapipe/ultralytics - imported lazily) ----------
# =====================================================================
CV2_AVAILABLE = False
MEDIAPIPE_AVAILABLE = False
YOLO_AVAILABLE = False
_vision_deps_import_attempted = False
_yolo_import_attempted = False
cv2 = mp = mp_hands = mp_draw = YOLO = None


def _ensure_vision_deps():
    """Import cv2 + mediapipe on first use only. This is the single biggest
    startup-speed win: mediapipe (and especially ultralytics/PyTorch, loaded
    separately below) can take several seconds to import, so the app should
    not pay that cost until the camera is actually requested."""
    global _vision_deps_import_attempted, CV2_AVAILABLE, MEDIAPIPE_AVAILABLE
    global cv2, mp, mp_hands, mp_draw

    if _vision_deps_import_attempted:
        return CV2_AVAILABLE and MEDIAPIPE_AVAILABLE
    _vision_deps_import_attempted = True

    try:
        import cv2 as _cv2
        cv2 = _cv2
        CV2_AVAILABLE = True
    except Exception as e:
        print("OpenCV import error (pip install opencv-python):", e)

    try:
        import mediapipe as _mp
        _ = _mp.solutions.hands  # some builds lack this - fail loudly and fall back
        mp = _mp
        mp_hands = _mp.solutions.hands
        mp_draw = _mp.solutions.drawing_utils
        MEDIAPIPE_AVAILABLE = True
    except Exception as e:
        print("MediaPipe import error or missing 'solutions' API (pip install mediapipe):", e)

    return CV2_AVAILABLE and MEDIAPIPE_AVAILABLE


def _ensure_yolo():
    global _yolo_import_attempted, YOLO_AVAILABLE, YOLO
    if _yolo_import_attempted:
        return YOLO_AVAILABLE
    _yolo_import_attempted = True
    try:
        from ultralytics import YOLO as _YOLO
        YOLO = _YOLO
        YOLO_AVAILABLE = True
    except Exception as e:
        print("Ultralytics import error (pip install ultralytics):", e)
    return YOLO_AVAILABLE


STABILITY_FRAMES = 5
ANNOUNCE_COOLDOWN = 2.5
YOLO_CONF_THRESHOLD = 0.5
PEN_ASPECT_RATIO_MIN = 4.0
PEN_MAX_AREA_FRACTION = 0.05

PINCH_DISTANCE_THRESHOLD = 0.07
THUMB_VERTICAL_THRESHOLD = 0.07
SWIPE_MIN_DELTA = 0.25
SWIPE_MAX_WINDOW = 0.6
GESTURE_COOLDOWN = 1.2

FINGER_TIP_MCP_PAIRS = [(8, 5), (12, 9), (16, 13), (20, 17)]  # (tip, base knuckle) per finger

vision_active = False
camera_mode = None            # "interpretation" or "gesture" - mutually exclusive
_vision_thread = None
_yolo_model_cache = None
_current_vision_speak_callback = None

# Embedded camera feed: the vision loop writes the latest processed frame
# here (as an RGB numpy array), and the GUI reads it to display in its own
# panel - no separate OpenCV window needed.
latest_vision_frame = None
_vision_frame_lock = threading.Lock()


# ---------- Pure logic (testable without a camera) ----------
def count_fingers(landmark_points, handedness_label):
    fingers_up = 0
    if handedness_label == "Right":
        if landmark_points[4][0] > landmark_points[3][0]:
            fingers_up += 1
    else:
        if landmark_points[4][0] < landmark_points[3][0]:
            fingers_up += 1
    for tip_id in (8, 12, 16, 20):
        pip_id = tip_id - 2
        if landmark_points[tip_id][1] < landmark_points[pip_id][1]:
            fingers_up += 1
    return fingers_up


def landmark_distance(p1, p2):
    return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5


def is_pinching(landmark_points, threshold=PINCH_DISTANCE_THRESHOLD):
    return landmark_distance(landmark_points[4], landmark_points[8]) < threshold


def _fingers_curled(pts, wrist_id=0):
    """
    Rotation-robust curl check: a curled finger folds its tip back closer
    to the wrist than its own base knuckle is. This works regardless of
    hand tilt/rotation, unlike comparing raw y-coordinates (which breaks
    if the hand isn't held perfectly upright).
    """
    return all(
        landmark_distance(pts[tip], pts[wrist_id]) < landmark_distance(pts[mcp], pts[wrist_id])
        for tip, mcp in FINGER_TIP_MCP_PAIRS
    )


def is_thumbs_up(pts, handedness_label):
    if is_pinching(pts):
        return False  # a pinch can look like curled fingers + raised thumb - explicitly rule it out
    if not _fingers_curled(pts):
        return False
    wrist_y = pts[0][1]
    return pts[4][1] < wrist_y - THUMB_VERTICAL_THRESHOLD


def is_thumbs_down(pts, handedness_label):
    if is_pinching(pts):
        return False
    if not _fingers_curled(pts):
        return False
    wrist_y = pts[0][1]
    return pts[4][1] > wrist_y + THUMB_VERTICAL_THRESHOLD


def classify_gesture(pts, handedness_label):
    """
    Single source of truth for which gesture (if any) a hand is making
    this frame. Checked in priority order so gestures can never co-fire -
    this is what fixes pinch and thumbs-up triggering at the same time.
    Returns one of: "pinch", "thumbs_up", "thumbs_down", or None.
    """
    if is_pinching(pts):
        return "pinch"
    if is_thumbs_up(pts, handedness_label):
        return "thumbs_up"
    if is_thumbs_down(pts, handedness_label):
        return "thumbs_down"
    return None


class SwipeTracker:
    def __init__(self, max_window=SWIPE_MAX_WINDOW, min_delta=SWIPE_MIN_DELTA):
        self.positions = deque()
        self.max_window = max_window
        self.min_delta = min_delta

    def update(self, x, now=None):
        now = time.time() if now is None else now
        self.positions.append((now, x))
        while self.positions and now - self.positions[0][0] > self.max_window:
            self.positions.popleft()
        if len(self.positions) < 2:
            return None
        delta = x - self.positions[0][1]
        if delta > self.min_delta:
            self.positions.clear()
            return "right"
        if delta < -self.min_delta:
            self.positions.clear()
            return "left"
        return None


class GestureCooldown:
    def __init__(self, cooldown=GESTURE_COOLDOWN):
        self.cooldown = cooldown
        self._last_fired = {}

    def ready(self, gesture_name, now=None):
        now = time.time() if now is None else now
        return (now - self._last_fired.get(gesture_name, 0)) >= self.cooldown

    def mark_fired(self, gesture_name, now=None):
        self._last_fired[gesture_name] = time.time() if now is None else now


# ---------- 3D depth-based continuous control ----------
# MediaPipe estimates a rough relative depth (z) per landmark, more
# negative meaning closer to the camera. It's noisier than x/y, so this
# smooths readings with a moving average before using them, and only
# reports a delta once a stable baseline is established.
DEPTH_SMOOTHING_WINDOW = 5
DEPTH_NOISE_THRESHOLD = 0.015   # ignore jitter smaller than this
DEPTH_TO_VOLUME_SENSITIVITY = 300  # scales raw z-delta into a volume-percent delta


class DepthTracker:
    def __init__(self, smoothing_window=DEPTH_SMOOTHING_WINDOW):
        self.history = deque(maxlen=smoothing_window)
        self.baseline = None

    def update(self, raw_z):
        """Feed in the current raw z reading. Returns the smoothed delta
        from baseline (negative raw_z = closer to camera, so a hand
        moving closer produces a positive delta here), or None if no
        baseline is established yet (first reading, or after a reset)."""
        self.history.append(raw_z)
        smoothed = sum(self.history) / len(self.history)
        if self.baseline is None:
            self.baseline = smoothed
            return None
        return self.baseline - smoothed  # closer (more negative z) -> positive delta

    def reset(self):
        self.history.clear()
        self.baseline = None


def _shape_from_metrics(vertices, aspect_ratio, circularity):
    if vertices == 3:
        return "triangle"
    if vertices == 4:
        return "square" if 0.90 <= aspect_ratio <= 1.10 else "rectangle"
    if vertices == 5:
        return "pentagon"
    if vertices >= 6:
        return "circle" if circularity > 0.75 else "polygon"
    return None


def _is_pen_like(aspect_ratio, area_fraction):
    return aspect_ratio >= PEN_ASPECT_RATIO_MIN and area_fraction <= PEN_MAX_AREA_FRACTION


def _build_description(hands_seen, total_fingers, object_labels, shape_name, pen_guess):
    if hands_seen > 0:
        plural = "s" if total_fingers != 1 else ""
        return f"I see {total_fingers} finger{plural}"
    if object_labels:
        return "I see " + ", ".join(sorted(set(object_labels)))
    if shape_name:
        return f"That looks like a {shape_name}"
    if pen_guess:
        return "That might be a pen or pencil, but I'm not fully sure"
    return None


# ---------- Camera-dependent helpers ----------
def _load_yolo_model():
    global _yolo_model_cache
    if _yolo_model_cache is not None:
        return _yolo_model_cache
    if not _ensure_yolo():
        return None
    try:
        _yolo_model_cache = YOLO(resource_path("yolov8n.pt"))
    except Exception as e:
        print("YOLO load error:", e)
        _yolo_model_cache = None
    return _yolo_model_cache


def _detect_shape_and_pen(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(blurred, 50, 150), None, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, False

    largest = max(contours, key=cv2.contourArea)
    frame_area = frame.shape[0] * frame.shape[1]
    area = cv2.contourArea(largest)
    if area < 0.01 * frame_area:
        return None, False

    peri = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.04 * peri, True)
    x, y, w, h = cv2.boundingRect(approx)
    aspect_ratio = max(w, h) / float(min(w, h)) if min(w, h) > 0 else 1.0

    (_, _), radius = cv2.minEnclosingCircle(largest)
    circle_area = 3.14159 * radius * radius
    circularity = (area / circle_area) if circle_area > 0 else 0

    box_ratio = w / float(h) if h > 0 else 1.0
    if box_ratio < 1.0:
        box_ratio = 1.0 / box_ratio

    shape_name = _shape_from_metrics(len(approx), box_ratio, circularity)
    pen_guess = _is_pen_like(aspect_ratio, area / frame_area)
    return shape_name, pen_guess


def _vision_loop(speak_callback, camera_index, mode, action_callbacks):
    """
    mode is one of "interpretation" or "gesture" - strictly mutually
    exclusive so narration and gesture-driven actions never run at the
    same time (this is what caused the reported conflicts before).
    """
    global vision_active

    action_callbacks = action_callbacks or {}

    if not _ensure_vision_deps():
        speak_callback("I can't start the camera because OpenCV or MediaPipe isn't installed.")
        vision_active = False
        return

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        speak_callback("I couldn't access the camera.")
        vision_active = False
        return

    hands = mp_hands.Hands(max_num_hands=2, min_detection_confidence=0.7, min_tracking_confidence=0.6)
    yolo_model = _load_yolo_model() if mode == "interpretation" else None

    recent_descriptions = deque(maxlen=STABILITY_FRAMES)
    last_spoken = None
    last_spoken_time = 0.0

    swipe_tracker = SwipeTracker()
    depth_tracker = DepthTracker()
    gesture_cooldown = GestureCooldown()
    was_pinching = False
    was_open_palm = False

    window_title = "Nova Vision - Interpretation" if mode == "interpretation" else "Nova Vision - Gesture Control"

    while vision_active:
        ok, frame = cap.read()
        if not ok:
            continue

        frame = cv2.flip(frame, 1)
        display = frame.copy()

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = hands.process(rgb)
        total_fingers = 0
        hands_seen = 0
        gesture_label = None

        if results.multi_hand_landmarks and results.multi_handedness:
            for hand_landmarks, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                hands_seen += 1
                label = handedness.classification[0].label
                pts = [(lm.x, lm.y, lm.z) for lm in hand_landmarks.landmark]
                total_fingers += count_fingers(pts, label)
                mp_draw.draw_landmarks(display, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                # ---- Gesture-triggered actions ONLY in gesture mode ----
                if mode == "gesture":
                    static_gesture = classify_gesture(pts, label)

                    if static_gesture == "pinch":
                        pinching_now = True
                        if not was_pinching and gesture_cooldown.ready("pinch"):
                            gesture_label = "Pinch (play/pause)"
                            gesture_cooldown.mark_fired("pinch")
                            fn = action_callbacks.get("play_pause")
                            if fn:
                                _, message = fn()
                                speak_callback(message)
                    else:
                        pinching_now = False

                    if static_gesture == "thumbs_up" and gesture_cooldown.ready("thumbs_up"):
                        gesture_label = "Thumbs up (volume up)"
                        gesture_cooldown.mark_fired("thumbs_up")
                        fn = action_callbacks.get("volume_up")
                        if fn:
                            fn()
                            speak_callback("Volume up.")
                    elif static_gesture == "thumbs_down" and gesture_cooldown.ready("thumbs_down"):
                        gesture_label = "Thumbs down (volume down)"
                        gesture_cooldown.mark_fired("thumbs_down")
                        fn = action_callbacks.get("volume_down")
                        if fn:
                            fn()
                            speak_callback("Volume down.")

                    was_pinching = pinching_now

                    # Open palm (all 5 fingers) engages continuous depth-based
                    # volume control - deliberately a different pose from the
                    # relaxed hand used for swiping, so they can't conflict.
                    if total_fingers == 5 and static_gesture is None:
                        if not was_open_palm:
                            depth_tracker.reset()  # fresh baseline on entering this pose
                            was_open_palm = True
                        raw_z = pts[0][2]
                        delta = depth_tracker.update(raw_z)
                        if delta is not None and abs(delta) > DEPTH_NOISE_THRESHOLD:
                            volume_step = max(-5, min(5, delta * DEPTH_TO_VOLUME_SENSITIVITY))
                            fn = action_callbacks.get("volume_scrub")
                            if fn:
                                fn(volume_step)
                            gesture_label = f"Depth control ({volume_step:+.1f}%)"
                            depth_tracker.reset()  # re-baseline so control tracks relative motion, not absolute
                    else:
                        was_open_palm = False

                        # Only check for a swipe when the hand isn't already
                        # doing a static gesture this frame - prevents a swipe
                        # from sneaking in during a pinch/thumbs motion.
                        if static_gesture is None:
                            swipe_result = swipe_tracker.update(pts[0][0])
                            if swipe_result and gesture_cooldown.ready(f"swipe_{swipe_result}"):
                                gesture_cooldown.mark_fired(f"swipe_{swipe_result}")
                                if swipe_result == "right":
                                    gesture_label = "Swipe right (seek forward)"
                                    fn = action_callbacks.get("seek_forward")
                                else:
                                    gesture_label = "Swipe left (seek backward)"
                                    fn = action_callbacks.get("seek_backward")
                                if fn:
                                    _, message = fn()
                                    speak_callback(message)

            cv2.putText(display, f"Fingers: {total_fingers}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            if gesture_label:
                cv2.putText(display, gesture_label, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        else:
            was_pinching = False
            was_open_palm = False

        # ---- Narration (objects/shapes/fingers) ONLY in interpretation mode ----
        if mode == "interpretation":
            object_labels = []
            if yolo_model is not None:
                yolo_results = yolo_model(frame, verbose=False)[0]
                for box in yolo_results.boxes:
                    conf = float(box.conf[0])
                    if conf < YOLO_CONF_THRESHOLD:
                        continue
                    cls_id = int(box.cls[0])
                    obj_label = yolo_model.names[cls_id]
                    object_labels.append(obj_label)
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cv2.rectangle(display, (x1, y1), (x2, y2), (255, 0, 0), 2)
                    cv2.putText(display, obj_label, (x1, max(y1 - 8, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

            shape_name, pen_guess = (None, False)
            if hands_seen == 0 and not object_labels:
                shape_name, pen_guess = _detect_shape_and_pen(frame)
                if shape_name:
                    cv2.putText(display, f"Shape: {shape_name}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2)

            description = _build_description(hands_seen, total_fingers, object_labels, shape_name, pen_guess)

            recent_descriptions.append(description)
            now = time.time()
            if (description is not None
                    and recent_descriptions.count(description) >= STABILITY_FRAMES
                    and description != last_spoken
                    and now - last_spoken_time > ANNOUNCE_COOLDOWN):
                speak_callback(description)
                last_spoken = description
                last_spoken_time = now

        # Embed the frame into the GUI instead of a separate OpenCV window
        global latest_vision_frame
        rgb_display = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        with _vision_frame_lock:
            latest_vision_frame = rgb_display
        cv2.waitKey(1)  # still needed for OpenCV's internal frame housekeeping

    with _vision_frame_lock:
        latest_vision_frame = None  # clears the GUI panel back to "camera inactive"
    cap.release()
    hands.close()


def start_vision(speak_callback, camera_index=0, mode="interpretation", action_callbacks=None):
    global _vision_thread, vision_active, camera_mode, _current_vision_speak_callback
    if vision_active:
        return False
    if mode not in ("interpretation", "gesture"):
        mode = "interpretation"
    vision_active = True
    camera_mode = mode
    _current_vision_speak_callback = speak_callback
    _vision_thread = threading.Thread(
        target=_vision_loop, args=(speak_callback, camera_index, mode, action_callbacks), daemon=True
    )
    _vision_thread.start()
    return True


def stop_vision():
    """Stop the camera and play a distinct beep so it's audibly clear
    you're back in normal reply mode. Safe to call from the voice-command
    handler or from inside the vision loop's own 'q'-keypress handling."""
    global vision_active, camera_mode
    if not vision_active:
        return False
    vision_active = False
    camera_mode = None
    play_mode_revert_beep()
    return True


# ---------- Camera selection ----------
# The bug this fixes: cv2.VideoCapture(0) always opens whatever device
# Windows happens to register as index 0. If a phone-as-webcam app
# (DroidCam, Iriun, Camo, EpocCam, Windows' "Phone Link" camera feature,
# etc.) is installed, it can grab index 0 ahead of the laptop's real
# built-in webcam - Nova was hard-coded to index 0, so it opened whichever
# one Windows put first, not necessarily the laptop's own camera.
_LIKELY_VIRTUAL_CAMERA_NAMES = ("droidcam", "iriun", "camo", "epoccam", "obs virtual",
                                 "phone link", "virtual camera", "ivcam", "ndi", "elgato")


def _get_camera_device_names():
    """Real device names, e.g. 'Integrated Webcam' vs 'DroidCam Source' -
    needs the optional 'pygrabber' package (Windows-only, DirectShow).
    Returns [] without it, rather than guessing at names."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        return FilterGraph().get_input_devices()
    except Exception:
        return []


def list_available_cameras(max_index=5):
    """Probes camera indices 0..max_index-1, actually opens each one and
    reads a frame to confirm it works (not just that the index exists).
    Returns [(index, name, likely_virtual), ...]."""
    if not _ensure_vision_deps():
        return []
    names = _get_camera_device_names()
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        try:
            ok = cap.isOpened()
            if ok:
                ok, _frame = cap.read()
        except Exception:
            ok = False
        finally:
            cap.release()
        if ok:
            name = names[i] if i < len(names) else f"Camera {i}"
            likely_virtual = any(v in name.lower() for v in _LIKELY_VIRTUAL_CAMERA_NAMES)
            found.append((i, name, likely_virtual))
    return found


def get_preferred_camera_index():
    try:
        return int(get_preference("camera_index", 0) or 0)
    except (TypeError, ValueError):
        return 0


def list_cameras_reply():
    cams = list_available_cameras()
    if not cams:
        return "I couldn't find any working cameras on this PC."
    current = get_preferred_camera_index()
    parts = []
    for idx, name, likely_virtual in cams:
        tag = " - looks like a phone/virtual camera" if likely_virtual else ""
        marker = " (currently selected)" if idx == current else ""
        parts.append(f"{idx}: {name}{tag}{marker}")
    return "Cameras found: " + "; ".join(parts) + ". Say 'use camera N for vision' to switch."


def set_vision_camera_reply(index):
    cams = {idx for idx, _name, _v in list_available_cameras()}
    if cams and index not in cams:
        return f"I didn't find a working camera at index {index}. Say 'list cameras' to see what's available."
    set_preference("camera_index", str(index))
    return f"Vision will now use camera {index}. Turn vision off and back on for it to take effect."


CAMERA_LIST_RE = re.compile(r"\blist (?:my )?cameras\b|\bwhat cameras (?:do i have|are available)\b", re.IGNORECASE)
CAMERA_SET_RE = re.compile(r"\buse camera (\d+)(?: for vision)?\b|\bset (?:the )?camera to (\d+)\b", re.IGNORECASE)


def handle_camera_selection_command(cmd):
    """Returns a spoken reply if `cmd` matched a camera-selection command,
    else None."""
    if CAMERA_LIST_RE.search(cmd):
        return list_cameras_reply()
    m = CAMERA_SET_RE.search(cmd)
    if m:
        index = int(m.group(1) or m.group(2))
        return set_vision_camera_reply(index)
    return None


# =====================================================================
# ---------- Intent detection ----------
# =====================================================================
def _gesture_action_callbacks():
    """Built fresh each time so it always calls the current functions."""
    return {
        "play_pause": toggle_play_pause,
        "volume_up": lambda: change_volume(10),
        "volume_down": lambda: change_volume(-10),
        "seek_forward": lambda: seek_video(10),
        "seek_backward": lambda: seek_video(-10),
        "volume_scrub": lambda delta: change_volume(delta),
    }


def choose_action_and_reply(command):
    global pending_search_query, pending_vision_mode_choice

    cmd = command.lower().strip()

    # ---- Interrupt: stop Nova mid-sentence. Checked before anything else,
    # and deliberately silent (empty reply) - speaking a confirmation right
    # after being told to stop talking would defeat the point. Real-time
    # barge-in (stopping the instant you say ANYTHING new) also happens
    # automatically in the audio callback and the typed-command handler;
    # this is for when you want quiet without issuing a new command too. ----
    if _contains_trigger(cmd, "stop talking") or _contains_trigger(cmd, "stop speaking") or \
       _contains_trigger(cmd, "be quiet") or _contains_trigger(cmd, "shut up") or \
       _contains_trigger(cmd, "shush") or _contains_trigger(cmd, "quiet please") or \
       _contains_trigger(cmd, "quiet down") or _contains_trigger(cmd, "silence please"):
        if is_speaking():
            stop_speaking()
        log_activity("Interrupted")
        return ("", None)

    # ---- Pending: waiting for yes/no on an offered search ----
    if pending_search_query:
        if is_affirmative(cmd):
            query = pending_search_query
            pending_search_query = None
            return (search_and_explain(query), None)
        if is_decline(cmd):
            pending_search_query = None
            return ("No worries, let me know if you change your mind.", None)

    # ---- Pending: waiting for the camera mode choice ----
    if pending_vision_mode_choice:
        if any(k in cmd for k in ("interpretation", "identify", "describe", "recognition", "object", "objects")):
            pending_vision_mode_choice = False
            started = start_vision(speak_async, camera_index=get_preferred_camera_index(), mode="interpretation")
            return ("Starting visual interpretation mode." if started else "The camera is already on.", None)
        if any(k in cmd for k in ("gesture", "control", "hand control", "hand gestures", "gestures")):
            pending_vision_mode_choice = False
            started = start_vision(speak_async, camera_index=get_preferred_camera_index(), mode="gesture",
                                    action_callbacks=_gesture_action_callbacks())
            return ("Starting gesture control mode." if started else "The camera is already on.", None)
        if is_decline(cmd) or any(k in cmd for k in ("cancel", "never mind", "nevermind", "forget it")):
            pending_vision_mode_choice = False
            return ("Okay, camera stays off.", None)
        return ("Sorry, which mode, sir? Say 'interpretation' or 'gesture'.", None)

    if not cmd:
        return ("I didn't catch that. Could you say it again?", None)

    # ---- Multi-turn: resolve vague follow-ups ("tell me more", "what about
    # him") into the last real topic before anything else tries to match ----
    resolved = resolve_followup(cmd)
    if resolved:
        cmd = resolved

    # ---- WhatsApp notifications ("should I read it out?" and "read my
    # notifications") - checked first among phone/WhatsApp things since a
    # pending yes/no here is time-sensitive ----
    notif_reply = handle_notification_command(cmd)
    if notif_reply is not None:
        return (notif_reply, None)

    # ---- WhatsApp - checked before phone-call routing so a WhatsApp voice
    # message you're dictating (which might contain words like "call") never
    # gets misrouted into placing an actual phone call ----
    whatsapp_reply_text = handle_whatsapp_command(cmd)
    if whatsapp_reply_text is not None:
        return (whatsapp_reply_text, None)

    # ---- Phone control (calls) - checked early so "call X" never falls
    # through to web search or anything else ----
    phone_reply = handle_phone_command(cmd)
    if phone_reply is not None:
        return (phone_reply, None)

    # ---- Preferences / routines ----
    if re.search(r"\bwhat do you remember about me\b|\bwhat have i told you\b", cmd):
        return (what_do_you_remember_reply(), None)
    pref_routine_reply = handle_preference_and_routine_command(cmd)
    if pref_routine_reply == "":
        return ("", None)  # already spoken (e.g. a routine being read out loud) - nothing more to say
    if pref_routine_reply is not None:
        return (pref_routine_reply, None)

    # ---- General-purpose notes / recall ("remember that...", "did I ever
    # mention...") - the catch-all for memory that isn't a structured fact,
    # preference, or routine ----
    note_recall_reply = handle_note_and_recall_command(cmd)
    if note_recall_reply is not None:
        return (note_recall_reply, None)

    # ---- Camera selection (which physical camera vision uses) ----
    camera_selection_reply = handle_camera_selection_command(cmd)
    if camera_selection_reply is not None:
        return (camera_selection_reply, None)

    # ---- AI DJ Mode - checked before generic "play X on youtube" parsing,
    # since "play some gaming music" would otherwise be treated as a plain
    # video search instead of starting a DJ session ----
    dj_reply = handle_dj_command(cmd)
    if dj_reply is not None:
        return (dj_reply, None)

    # ---- Orbital Simulation Mode ----
    orbital_reply = handle_orbital_command(cmd)
    if orbital_reply is not None:
        return (orbital_reply, None)

    # ---- Schedule / reminders / stopwatch / countdown ----
    sched_reply = handle_schedule_command(cmd)
    if sched_reply is not None:
        return (sched_reply, None)

    # ---- India & World News ----
    news_reply = handle_news_command(cmd)
    if news_reply is not None:
        return (news_reply, None)

    # ---- Multi-Agent Swarm Mode: on/off + status ----
    swarm_reply = handle_swarm_command(cmd)
    if swarm_reply is not None:
        return (swarm_reply, None)

    # ---- Productive Mode: on/off ----
    if any(k in cmd for k in ("turn on productive mode", "start productive mode",
                               "productive mode on", "enable productive mode")):
        set_productive_mode(True)
        return ("Productive mode is on. Ask me anything - maths gets solved on the spot, "
                "and everything else jumps straight to the best result on the web.", None)
    if any(k in cmd for k in ("turn off productive mode", "stop productive mode",
                               "productive mode off", "disable productive mode")):
        set_productive_mode(False)
        return ("Productive mode is off.", None)

    # ---- Kochi Intelligence Map ----
    if any(k in cmd for k in ("kochi map", "kochi intelligence map", "intelligence map",
                               "kochi weather map", "open the kochi map", "show me the kochi map")):
        ok, message = open_kochi_map()
        return (message, None)

    # ---- Camera on/off ----
    if any(k in cmd for k in ("turn on visual interpretation", "turn on the video sensor", "turn on video sensor")):
        if vision_active:
            return ("The camera is already on.", None)
        pending_vision_mode_choice = True
        return ("Which mode, sir? Say 'interpretation' for object and finger recognition, or 'gesture' for hand gesture controls.", None)

    if any(k in cmd for k in ("turn off visual interpretation", "turn off the video sensor", "turn off video sensor")):
        stopped = stop_vision()
        return ("Reverting to reply mode." if stopped else "The camera isn't on.", None)

    # ---- Memory recall ----
    if any(k in cmd for k in ("what's my name", "what is my name", "do you know my name")):
        name = get_fact("name")
        return (f"Your name is {name}." if name else "I don't think you've told me your name yet.", None)

    if any(k in cmd for k in ("what do i like", "what are my interests", "what do you know about me")):
        likes = get_fact("likes")
        if likes:
            likes_text = ", ".join(likes) if isinstance(likes, list) else likes
            return (f"You've mentioned liking {likes_text}.", None)
        return ("I don't have any of your interests saved yet.", None)

    if any(k in cmd for k in ("forget everything", "clear your memory", "forget what you know about me")):
        clear_memory()
        return ("Okay, I've cleared everything I remembered about you.", None)

    if any(k in cmd for k in ("erase my archive forever", "delete my entire memory forever",
                               "wipe my archive forever", "erase everything forever")):
        clear_archive_forever()
        return ("Done. The permanent archive is gone - that one can't be undone.", None)

    # (Legacy "what did I say about X" style recall is now handled earlier,
    # by handle_note_and_recall_command / recall_reply, which covers these
    # same phrases plus a full-archive search - see RECALL_PATTERN above.)

    # ---- Browser: close ----
    if any(k in cmd for k in ("close the browser", "close browser", "exit browser", "quit browser", "shut the browser")):
        close_browser()
        return ("Closing the browser.", None)

    # ---- Browser: click a result from the last search ----
    if any(k in cmd for k in ("click", "open that", "play that", "play it", "click it")):
        if last_results:
            index = parse_click_index(cmd)
            ok, message = click_last_result(index)
            return (message, None)

    # ---- Browser: search YouTube / Spotify / Google / Bing ----
    platform_name, query = parse_search_command(cmd)
    if platform_name and query:
        results, error = run_search(platform_name, query)
        if error:
            return (error, None)
        return (describe_results(results), None)

    # ---- Browser: play/pause ----
    if any(k in cmd for k in ("pause the video", "pause it", "pause")):
        ok, message = pause_video()
        return (message, None)

    if any(k in cmd for k in ("play the video", "resume", "unpause", "continue playing")):
        ok, message = play_video()
        return (message, None)

    # ---- Browser: ad skipper ----
    if any(k in cmd for k in ("skip ads", "turn on ad skipper", "start skipping ads")):
        started = start_ad_skipper()
        return ("Ad skipper is on." if started else "Already skipping ads.", None)

    if any(k in cmd for k in ("stop skipping ads", "turn off ad skipper")):
        stopped = stop_ad_skipper()
        return ("Ad skipper turned off." if stopped else "It wasn't running.", None)

    # ---- Exit ----
    if any(k in cmd for k in ("exit", "quit", "goodbye", "stop listening", "bye")):
        return ("Goodbye. I'll be here when you need me.", "exit")

    if any(k in cmd for k in ("how are you", "how are u", "how r you", "how's it going")):
        return (random.choice(["I'm doing well, thanks — ready to help.", "All systems nominal. How can I assist?"]), None)

    # ---- Flood Watch + Daily Briefing ----
    flood_reply_text = handle_flood_command(cmd)
    if flood_reply_text is not None:
        return (flood_reply_text, None)
    briefing_text = handle_briefing_command(cmd)
    if briefing_text is not None:
        return (briefing_text, None)

    # ---- Weather (Kochi areas are live; other places fall back to a web search) ----
    if "weather" in cmd:
        area = find_kochi_area(cmd)
        if area is None and any(k in cmd for k in ("kochi", "cochin")):
            area = KOCHI_DEFAULT_AREA
        if area is None and cmd.strip() in ("weather", "the weather", "what's the weather", "how's the weather"):
            area = WEATHER_STATE["area"]
        if area:
            return (kochi_weather_reply(area), None)
        if "kerala" in cmd:
            pending_search_query = "weather in kerala"
            return ("Kerala is usually warm and humid. Want me to search live weather online?", None)
        pending_search_query = "weather " + cmd
        return ("I have live weather for every area of Kochi - name one, like 'weather in Fort Kochi'. "
                "For anywhere else, should I search online?", None)

    # ---- Open / launch apps ----
    if any(cmd.startswith(t) or f" {t} " in cmd for t in ("open", "launch", "start", "run")):
        for name in ("youtube", "chrome", "google chrome", "edge", "firefox", "bing", "minecraft", "copilot", "spotify", "explorer", "file explorer", "settings", "google"):
            if name in cmd:
                key = "chrome" if name == "google chrome" else ("explorer" if name in ("file explorer", "fileexplorer") else name)
                success = open_app(key)
                if success:
                    if key in ("youtube", "bing", "google"):
                        return (f"Opening {name}. Would you like me to search for something there?", None)
                    return (f"Opening {name} for you.", None)
                return (f"Couldn't open {name}. Please check the path in APP_PATHS.", None)

    # ---- Mute / unmute ----
    if "mute" in cmd and "unmute" not in cmd:
        ok = mute_volume(True)
        return ("Muted." if ok else "Couldn't mute.", None)

    if "unmute" in cmd:
        ok = mute_volume(False)
        return ("Unmuted." if ok else "Couldn't unmute.", None)

    # ---- Volume up/down ----
    if any(k in cmd for k in ("increase volume", "raise volume", "turn up", "volume up")):
        ok = change_volume(10)
        return ("Volume increased." if ok else "Couldn't increase volume.", None)

    if any(k in cmd for k in ("decrease volume", "lower volume", "turn down", "reduce volume", "volume down")):
        ok = change_volume(-10)
        return ("Volume decreased." if ok else "Couldn't decrease volume.", None)

    # ---- Volume set to X% ----
    if "volume" in cmd or "set volume" in cmd or "sound" in cmd:
        percent = extract_percent(cmd)
        if percent is not None:
            ok = set_volume(percent)
            if ok:
                return (f"Setting volume to {percent} percent.", None)
            return ("I couldn't change the volume. Make sure pycaw is installed on Windows.", None)
        return ("How loud would you like the volume set? Say for example 'set volume to 40 percent'.", None)

    # ---- Shutdown / restart ----
    if any(k in cmd for k in ("shutdown the", "shut down", "shutdown now", "turn off the computer", "power off")):
        return ("Are you sure? Say 'yes shutdown' to confirm.", None)
    if any(k in cmd for k in ("restart the", "restart now", "reboot", "restart computer")):
        return ("Are you sure? Say 'yes restart' to confirm.", None)
    if cmd in ("yes shutdown", "confirm shutdown"):
        ok = shutdown_system()
        return ("Shutting down now." if ok else "Couldn't shut down. Try running as administrator.", None)
    if cmd in ("yes restart", "confirm restart"):
        ok = restart_system()
        return ("Restarting now." if ok else "Couldn't restart. Try running as administrator.", None)

    # ---- Wi-Fi / Bluetooth ----
    if any(k in cmd for k in ("wifi on", "turn wifi on", "enable wifi")):
        ok = wifi_on()
        return ("Turning Wi-Fi on." if ok else "Couldn't toggle Wi-Fi.", None)
    if any(k in cmd for k in ("wifi off", "turn wifi off", "disable wifi")):
        ok = wifi_off()
        return ("Turning Wi-Fi off." if ok else "Couldn't toggle Wi-Fi.", None)
    if any(k in cmd for k in ("bluetooth on", "turn bluetooth on", "enable bluetooth")):
        ok = bluetooth_on()
        return ("Turning Bluetooth on." if ok else "Couldn't toggle Bluetooth.", None)
    if any(k in cmd for k in ("bluetooth off", "turn bluetooth off", "disable bluetooth")):
        ok = bluetooth_off()
        return ("Turning Bluetooth off." if ok else "Couldn't toggle Bluetooth.", None)

    # ---- Self-Healing System ----
    heal_text = handle_heal_command(cmd)
    if heal_text is not None:
        return (heal_text, None)

    # ---- Screen-aware help / ask AI ----
    screen_reply = handle_screen_command(cmd)
    if screen_reply is not None:
        return (screen_reply, None)

    # ---- Maths: ALWAYS available, never gated behind Productive Mode.
    # sympy is a real, deterministic symbolic engine - there's no reason
    # "solve 2x+3=7" or "derivative of x^2" should need a separate mode
    # turned on first. (Everything more specific - alarms, timers, phone,
    # WhatsApp, screen help, etc. - is already checked above this point,
    # so this can't accidentally steal a command meant for one of those.)
    if looks_like_math(cmd):
        remember_topic(cmd)
        return (solve_math_question(cmd), None)

    # ---- Productive Mode: intercept general questions, skipping the
    # "here are 3 results, say 'click the first one'" back-and-forth and
    # jumping straight to the top result instead. (Checked after every
    # specific command above, so "mute", "pause", etc. behave normally
    # even while this is on.) ----
    if PRODUCTIVE_MODE_ACTIVE:
        if is_question(cmd) and not re.search(r"\b(my|your|you|yours|me|myself|yourself)\b", cmd):
            return (productive_web_redirect(cmd), None)

    # ---- Diagrams ("show me a diagram of X") ----
    m = DIAGRAM_TRIGGER_RE.search(cmd)
    if m:
        topic = m.group(1).strip().rstrip("?.! ")
        if topic:
            remember_topic(topic)
            return (show_topic_diagram(topic), None)

    # ---- Hologram ("hologram of a cube", "hologram DANGER", "zoom in on
    # the hologram", ...) - checked before the generic search/question
    # fallback below so "hologram <anything>" always lands here first ----
    hologram_reply = handle_hologram_command(cmd)
    if hologram_reply is not None:
        return (hologram_reply, None)

    # ---- Questions -> search and explain right away ----
    # Personal/conversational questions ("how are you", "what do you think of me")
    # aren't web lookups - let them fall through to the conversational reply.
    if is_question(cmd) and not re.search(r"\b(my|your|you|yours|me|myself|yourself)\b", cmd):
        return (search_and_explain(cmd), None)

    # ---- Default conversational reply ----
    remember(cmd)
    reply, offered_search = get_human_reply(cmd)
    if offered_search:
        pending_search_query = cmd
    return (reply, None)


# =====================================================================
# ---------- Background callback ----------
# =====================================================================
def strip_pause_markers(text):
    """For logs/history - the spoken "||" pause markers shouldn't clutter
    the activity log or memory file."""
    return re.sub(r"\s*\|{2,3}\s*", " ", text or "").strip()


def callback(recognizer_obj, audio):
    heard_text, heard_lang = None, "en"

    if USE_LOCAL_WHISPER:
        # transcribe_locally() only supports English (language="en" is
        # hardcoded where it calls whisper) - so local Whisper users lose
        # Hindi/Malayalam recognition until USE_LOCAL_WHISPER is turned off.
        whisper_text = transcribe_locally(audio)
        if whisper_text:
            heard_text, heard_lang = whisper_text, "en"

    if not heard_text:
        try:
            result = recognize_multilingual(recognizer_obj, audio)
        except sr.RequestError as e:
            print("Speech API error:", e)
            global last_error_time
            last_error_time = time.time()
            log_activity(f"Speech API error: {e}")
            return
        if not result:
            print("Could not understand audio.")
            return
        heard_text, heard_lang = result

    # Barge-in: the moment we've actually understood something new, stop
    # Nova talking over you rather than waiting for it to finish its sentence.
    if is_speaking():
        stop_speaking()

    command, translated_ok = translate_command_to_english(heard_text, heard_lang)
    if not translated_ok:
        print("Translation unavailable for:", heard_text)
        log_activity(f"Translation unavailable ({LANGUAGES[heard_lang]['name']}): {heard_text}")
        speak_async("Sorry, translation isn't reachable right now. Please try again in a moment.")
        return

    # Multi-Agent Swarm Mode: "Atlas, ..." addresses that agent directly
    # (always available); with Swarm Mode on, an unaddressed request also
    # gets auto-routed to whichever agent best fits it. Skipped entirely
    # while Nova's mid-conversation on something else (see _awaiting_followup).
    if _awaiting_followup():
        agent_key = None
    else:
        agent_key, command = detect_addressed_agent(command)
        if agent_key is None and SWARM_MODE_ACTIVE:
            agent_key = guess_agent_for(command)
    bare_agent_wake = bool(agent_key) and not command.strip()

    print("You said:", heard_text)
    log_activity(f"You ({LANGUAGES[heard_lang]['name']}): {heard_text}")

    # Broad catch-all: a bug in ANY feature (search, browser, vision, etc.)
    # should never be able to silently kill this background listening
    # thread. Better to log it and stay listening than to go unresponsive
    # with zero visible indication of what happened.
    try:
        set_last_heard(heard_lang, heard_text, command)
        user_emotion = detect_emotion(command, audio_data=audio)
        register = detect_register(command)

        if bare_agent_wake:
            reply, action = f"{AGENTS[agent_key]['name']} here. Go ahead.", None
        else:
            lang_reply = handle_language_command(command)
            if lang_reply is not None:
                reply, action = lang_reply, None
            else:
                reply, action = choose_action_and_reply(command.lower())
            if agent_key and reply:
                reply = agent_intro_prefix(agent_key) + reply
        reply = adapt_reply_tone(reply, register) if reply else reply
        add_exchange(command, strip_pause_markers(reply))

        reply_lang = resolve_reply_lang()
        spoken, spoken_lang = localize_for_speech(reply, reply_lang)
        voice = agent_voice_for(agent_key, spoken_lang)
        tag = f" [{AGENTS[agent_key]['name']}]" if agent_key and spoken_lang == "en" else ""
        log_activity(f"Nova ({LANGUAGES[spoken_lang]['name']}){tag}: {strip_pause_markers(spoken)}")

        if action == "exit":
            speak_async(spoken, emotion=response_emotion_for(user_emotion), voice=voice)
            time.sleep(0.6)
            stop_nova_engine()
            return

        speak_async(spoken, emotion=response_emotion_for(user_emotion), voice=voice)
    except Exception as e:
        import traceback
        print("Unhandled error while processing command:", command)
        print(traceback.format_exc())
        last_error_time = time.time()
        log_activity(f"Error handling command: {e}")
        speak_async("Sorry, something went wrong with that.")


# =====================================================================
# ---------- System tray app ----------
# =====================================================================
stop_listening = None  # set by recognizer.listen_in_background(); None means "not currently listening"
tray_icon_ref = None

WAKE_WORD = "enter"
# Same wake word, said in Hindi/Malayalam - checked alongside the English
# one so waking Nova up doesn't force switching back to English first.
# (Precomputed rather than translated live: the sleep listener runs
# constantly in the background and shouldn't depend on a network call.)
WAKE_WORD_TRANSLATIONS = {
    "hi": "\u090f\u0902\u091f\u0930",       # "enter" (transliterated)
    "ml": "\u0d0e\u0d28\u0d4d\u0d31\u0d7c",  # "enter" (transliterated)
}
_sleep_stop_listening = None


def _sleep_callback(recognizer_obj, audio):
    """
    Lightweight callback used only while asleep (after 'exit'). Listens
    for exactly one wake word (in whichever language is currently
    configured to listen for) and ignores everything else - deliberately
    cheap, doesn't run through the full command pipeline or a translation
    call.
    """
    listen_lang = LANG_STATE["listen"]
    stt_lang = LANGUAGES[listen_lang]["stt"] if listen_lang != "auto" else LANGUAGES["en"]["stt"]
    try:
        text = recognizer_obj.recognize_google(audio, language=stt_lang).lower()
    except (sr.UnknownValueError, sr.RequestError):
        return
    except Exception as e:
        print("Sleep listener error:", e)
        return

    wake_words = [WAKE_WORD] + list(WAKE_WORD_TRANSLATIONS.values())
    if any(w in text for w in wake_words):
        print("Wake word detected - resuming.")
        threading.Thread(target=start_nova_engine, daemon=True).start()


def start_sleep_listener():
    global _sleep_stop_listening
    if _sleep_stop_listening is not None:
        return
    try:
        _sleep_stop_listening = recognizer.listen_in_background(new_mic(), _sleep_callback, phrase_time_limit=3)
        print(f"Sleep mode active - say '{WAKE_WORD}' to resume.")
        log_activity(f"Asleep - say '{WAKE_WORD}' to resume")
    except Exception as e:
        print("Failed to start sleep listener:", e)
        _sleep_stop_listening = None


def stop_sleep_listener():
    global _sleep_stop_listening
    if _sleep_stop_listening is None:
        return
    try:
        _sleep_stop_listening(wait_for_stop=False)
    except Exception as e:
        print("Error stopping sleep listener:", e)
    _sleep_stop_listening = None


def start_nova_engine():
    """Run the startup sound/greeting, calibrate the mic, and begin
    listening in the background. Safe to call again after stop_nova_engine()."""
    global stop_listening, app_state

    if stop_listening is not None:
        print("Nova is already listening.")
        return

    # Never let the sleep listener and the main listener touch the mic at
    # the same time - that dual-stream overlap is what caused the earlier
    # startup crash. Stop it first and give the stream a moment to release.
    stop_sleep_listener()
    time.sleep(0.3)

    try:
        play_startup_chime()
        # Greet in whichever language REPLY is currently pinned to; if it's
        # "match" (the default), greet in English - there's nothing heard
        # yet to match at startup.
        greet_lang = LANG_STATE["reply"] if LANG_STATE["reply"] != "match" else "en"
        speak(LANGUAGES[greet_lang]["greeting"], voice=LANGUAGES[greet_lang]["voice"])  # blocking - finishes before mic calibration starts

        if USE_LOCAL_WHISPER:
            print(f"Preloading Whisper model ({WHISPER_MODEL_SIZE})...")
            _load_whisper_model()  # pays the one-time load cost now, not on your first sentence

        print("Calibrating microphone for ambient noise (stay silent)...")
        try:
            with new_mic() as source:
                recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_CALIBRATE_SECONDS)
        except Exception as e:
            print("Microphone calibration error:", e)

        print("Starting background listener. Speak anytime (say 'exit' to stop).")
        stop_listening = recognizer.listen_in_background(new_mic(), callback, phrase_time_limit=LISTEN_PHRASE_LIMIT)
        app_state = "listening"
        log_activity("Nova is now listening")

        # A short pause before opening the SECOND, independent microphone
        # stream (for the visualizer) gives the driver a moment to settle
        # after the recognizer's own stream just opened - two audio streams
        # grabbing the same device in the same instant is the likeliest
        # cause of a hard crash here.
        time.sleep(0.3)
        start_volume_monitor()
    except Exception as e:
        import traceback
        print("start_nova_engine failed:", traceback.format_exc())
        log_activity(f"Startup error: {e}")
        stop_listening = None
        app_state = "idle"


def stop_nova_engine(enter_sleep_mode=True):
    global stop_listening, app_state
    if stop_listening is None:
        print("Nova isn't currently listening.")
        return
    stop_listening(wait_for_stop=False)
    stop_listening = None
    app_state = "idle"
    stop_volume_monitor()
    print("Nova stopped listening.")
    log_activity("Nova stopped listening")

    if enter_sleep_mode:
        time.sleep(0.3)  # let the main listener's stream fully release first
        start_sleep_listener()


def _tray_status_text(item):
    return "Status: Listening" if stop_listening is not None else "Status: Stopped"


def _tray_toggle_listening(icon, item):
    if stop_listening is not None:
        stop_nova_engine()
    else:
        threading.Thread(target=start_nova_engine, daemon=True).start()


def _tray_open_log(icon, item):
    try:
        if OS_NAME.startswith("windows"):
            os.startfile(LOG_FILE)
        else:
            webbrowser.open(LOG_FILE)
    except Exception as e:
        print("Couldn't open log file:", e)


def _tray_quit(icon, item):
    stop_nova_engine()
    close_browser()   # clean up the Selenium session if one is open
    stop_vision()      # release the camera if it's active
    icon.stop()


def run_as_tray_app():
    """
    Entry point for the packaged app: no console window, an icon near
    the clock, and a right-click menu. All console output still happens
    via print() - it just goes to a log file instead of a visible window,
    since there's no console to show it in.
    """
    global tray_icon_ref

    log_f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_f
    sys.stderr = log_f
    print(f"\n--- Nova started {time.ctime()} ---")

    tray_image = Image.open(ICON_FILE)

    menu = pystray.Menu(
        pystray.MenuItem(_tray_status_text, None, enabled=False),
        pystray.MenuItem("Start/Stop Listening", _tray_toggle_listening),
        pystray.MenuItem("Open Log", _tray_open_log),
        pystray.MenuItem("Quit", _tray_quit),
    )

    tray_icon_ref = pystray.Icon("Nova", tray_image, "Nova Assistant", menu)

    # Start listening automatically on launch, same as before.
    threading.Thread(target=start_nova_engine, daemon=True).start()

    tray_icon_ref.run()  # blocks here - this IS the main loop now


# =====================================================================
# ---------- Phone control (calls via ADB or Windows Phone Link) ----------
# =====================================================================
# Windows does not let third-party apps act as a Bluetooth phone headset, so
# Nova can't dial "over Bluetooth" directly. Two backends that DO work:
#
#   1. ADB (Android, USB or wireless debugging): Nova sends the real
#      "place call" command to the phone. Reliable, fully hands-free, and
#      can also hang up, answer, read call state and import your contacts.
#
#   2. Windows Phone Link (Android or iPhone, uses Bluetooth for calls):
#      Nova opens the number in Phone Link and tries to press Call for you.
#      Best-effort - it depends on Phone Link's UI, so it may ask you to
#      press the Call button yourself.
#
# "auto" (default) uses ADB when a phone is connected, otherwise Phone Link.
import shutil
import difflib

PHONE_FILE = os.path.join(DATA_DIR, "nova_phone.json")
_NO_WINDOW = 0x08000000 if OS_NAME.startswith("windows") else 0  # CREATE_NO_WINDOW: no console flash from adb

_phone_config = {
    "contacts": {},   # saved by voice / by hand: {"Display Name": "+15551234567"}
    "synced": {},     # imported from the phone via ADB
    "adb_path": "",   # optional manual path to adb.exe
    "backend": "auto",  # "auto" | "adb" | "phonelink"
}
phone_state = {
    "backend": "none",      # "adb" | "phonelink" | "none"
    "device": "",
    "serial": "",
    "call_state": "unknown",  # "idle" | "ringing" | "in call" | "unknown"
    "contacts": 0,
    "note": "",
    "last_call": "",
}
_phone_lock = threading.Lock()
pending_call = None            # {"name", "number", "time"} while waiting for a yes/no on a call WE placed
PENDING_CALL_TIMEOUT = 30
pending_incoming_call = None   # {"name", "number", "time"} while a call is ringing and waiting for accept/decline
PENDING_INCOMING_TIMEOUT = 25  # give up asking after this long (phone will stop ringing on its own around then)
_adb_cache = None
_adb_model_cache = {}


# ---------- Config / contacts ----------
def all_contacts():
    merged = dict(_phone_config.get("synced", {}))
    merged.update(_phone_config.get("contacts", {}))  # hand-saved entries win
    return merged


def load_phone_config():
    try:
        if os.path.exists(PHONE_FILE):
            with open(PHONE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in _phone_config:
                if key in data:
                    _phone_config[key] = data[key]
    except Exception as e:
        print("Phone config load error (starting fresh):", e)
    phone_state["contacts"] = len(all_contacts())


def save_phone_config():
    try:
        tmp = PHONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_phone_config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PHONE_FILE)
        return True
    except Exception as e:
        print("Phone config save error:", e)
        return False


def clean_number(text):
    """Return a safe dial string ('+' plus digits only) or None. Everything
    else is stripped, so nothing odd can ever reach the adb command line."""
    t = re.sub(r"^\s*plus\s*", "+", str(text).strip(), flags=re.IGNORECASE)
    digits = re.sub(r"\D", "", t)
    if not (3 <= len(digits) <= 15):
        return None
    return ("+" if t.startswith("+") else "") + digits


def save_contact(name, number):
    num = clean_number(number)
    if not num or not name.strip():
        return False
    display = " ".join(w.capitalize() for w in name.strip().split())
    _phone_config["contacts"][display] = num
    save_phone_config()
    phone_state["contacts"] = len(all_contacts())
    return True


def lookup_contact(name):
    """Returns ('exact'|'fuzzy', display, number), ('ambiguous', [names]) or ('none',).
    Speech recognition mangles names, so near-misses are 'fuzzy' and Nova
    asks for confirmation before dialling them."""
    contacts = all_contacts()
    if not contacts:
        return ("none",)
    key = re.sub(r"[^\w\s']", "", name.lower()).strip()
    lower = {n.lower(): n for n in contacts}
    if key in lower:
        n = lower[key]
        return ("exact", n, contacts[n])
    partial = [n for n in contacts if key and key in n.lower().split()]
    if len(partial) == 1:
        return ("exact", partial[0], contacts[partial[0]])
    if len(partial) > 1:
        return ("ambiguous", sorted(partial)[:4])
    close = difflib.get_close_matches(key, list(lower), n=1, cutoff=0.6)
    if close:
        n = lower[close[0]]
        return ("fuzzy", n, contacts[n])
    return ("none",)


def _norm_number(n):
    """Last 10 digits only, so +1 555-123-4567, 5551234567 and 15551234567
    are all treated as the same number regardless of country-code prefix."""
    digits = re.sub(r"\D", "", n or "")
    return digits[-10:] if len(digits) >= 10 else digits


def reverse_lookup_contact(number):
    """Number -> saved contact name, or None if it's not in the address book."""
    if not number:
        return None
    target = _norm_number(number)
    if not target:
        return None
    for name, num in all_contacts().items():
        if _norm_number(num) == target:
            return name
    return None


# ---------- ADB helpers ----------
def find_adb():
    global _adb_cache
    if _adb_cache and os.path.isfile(_adb_cache):
        return _adb_cache
    exe = "adb.exe" if OS_NAME.startswith("windows") else "adb"
    candidates = [
        _phone_config.get("adb_path") or "",
        resource_path(os.path.join("platform-tools", exe)),   # bundled by the installer
        os.path.join(APP_DIR, "platform-tools", exe),
        os.path.join(DATA_DIR, "platform-tools", exe),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            _adb_cache = c
            return c
    _adb_cache = shutil.which("adb")
    return _adb_cache


def _adb(args, serial=None, timeout=8):
    adb = find_adb()
    if not adb:
        return False, "adb not found"
    cmd = [adb] + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return False, "adb timed out"
    except Exception as e:
        return False, str(e)


def adb_devices():
    """Returns (ready_serials, problem) where problem is '' or 'unauthorized'."""
    ok, out = _adb(["devices"], timeout=15)
    if not ok:
        return [], out
    ready, unauthorized = [], False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            if parts[1] == "device":
                ready.append(parts[0])
            elif parts[1] == "unauthorized":
                unauthorized = True
    return ready, ("unauthorized" if unauthorized and not ready else "")


def _adb_call_state(serial):
    ok, out = _adb(["shell", "dumpsys", "telephony.registry"], serial=serial, timeout=6)
    if not ok:
        return "unknown"
    states = [int(x) for x in re.findall(r"mCallState=(\d)", out)]
    if not states:
        return "unknown"
    return {0: "idle", 1: "ringing", 2: "in call"}.get(max(states), "unknown")


def _extract_incoming_number(serial):
    """Best-effort caller-ID lookup for a ringing call. The exact `dumpsys`
    text differs across Android versions/OEMs, so this tries a few known
    formats and gives up (returns None) rather than guess wrong. If caller
    ID never shows up on your phone, the announcement just says 'someone'
    instead of a name/number - calls can still be accepted/declined either way."""
    # Newer Android: telecom manages the call and prints a "Handle: tel:+123..."
    # line inside the block for the ringing call.
    ok, out = _adb(["shell", "dumpsys", "telecom"], serial=serial, timeout=6)
    if ok and out:
        block_state = None
        for line in out.splitlines():
            line = line.strip()
            sm = re.match(r"(?:mCallState|State):\s*(\w+)", line)
            if sm:
                block_state = sm.group(1).upper()
            hm = re.match(r"Handle:\s*tel:([+\d]+)", line)
            if hm and block_state and "RING" in (block_state or ""):
                return hm.group(1)
    # Older Android (mostly pre-10): telephony.registry sometimes exposes it directly.
    ok, out = _adb(["shell", "dumpsys", "telephony.registry"], serial=serial, timeout=6)
    if ok and out:
        nm = re.search(r"mCallIncomingNumber\s*=\s*([+\d]+)", out)
        if nm:
            return nm.group(1)
    return None


def phone_refresh_status():
    """Work out which backend is usable right now and refresh the call state.
    Spawns adb, so it only runs from the monitor thread / before a call."""
    pref = _phone_config.get("backend", "auto")
    backend, device, serial, note = "none", "", "", ""

    if pref in ("auto", "adb") and find_adb():
        devices, problem = adb_devices()
        if devices:
            serial = devices[0]
            backend = "adb"
            model = _adb_model_cache.get(serial)
            if not model:
                ok, out = _adb(["shell", "getprop", "ro.product.model"], serial=serial)
                model = out.strip() if ok and out.strip() else serial
                _adb_model_cache[serial] = model
            device = model
        elif problem == "unauthorized":
            note = "Phone found - tap 'Allow USB debugging' on it."

    if backend == "none" and pref in ("auto", "phonelink") and OS_NAME.startswith("windows"):
        backend, device = "phonelink", "Phone Link (Bluetooth)"

    call_state = _adb_call_state(serial) if backend == "adb" else "unknown"
    if backend == "none" and not note:
        note = "No phone. Plug in Android w/ USB debugging, or set up Phone Link. Click Setup."

    with _phone_lock:
        phone_state.update({"backend": backend, "device": device, "serial": serial,
                            "call_state": call_state, "note": note,
                            "contacts": len(all_contacts())})


_phone_monitor_active = False
_phone_monitor_thread = None


def _phone_monitor_loop():
    while _phone_monitor_active:
        try:
            phone_refresh_status()
        except Exception as e:
            print("Phone monitor error (non-fatal):", e)
        delay = 4 if phone_state["backend"] == "adb" else 15
        for _ in range(delay * 10):
            if not _phone_monitor_active:
                return
            time.sleep(0.1)


def start_phone_monitor():
    global _phone_monitor_active, _phone_monitor_thread
    if _phone_monitor_active:
        return
    _phone_monitor_active = True
    _phone_monitor_thread = threading.Thread(target=_phone_monitor_loop, daemon=True)
    _phone_monitor_thread.start()
    start_ring_watch()
    start_whatsapp_notification_watch()


def stop_phone_monitor():
    global _phone_monitor_active
    _phone_monitor_active = False
    stop_whatsapp_notification_watch()
    stop_ring_watch()


# =====================================================================
# ---------- WhatsApp (text via ADB intent + best-effort auto-send) ----------
# =====================================================================
# How this actually works, and its real limits:
#  - Opening a WhatsApp chat with a message pre-filled is a genuine,
#    documented Android feature: the "https://wa.me/<number>?text=<msg>"
#    link, launched via an ADB VIEW intent. No WhatsApp API/key needed,
#    and it works on any phone with WhatsApp installed.
#  - Actually pressing Send is NOT an official capability - there's no
#    public intent for it. This automates it by dumping the on-screen UI
#    layout (uiautomator), finding WhatsApp's send button by its resource
#    id, and tapping those exact coordinates. That resource id has been
#    stable for years, but it's WhatsApp's internal implementation detail,
#    not a contract - if a WhatsApp update ever renames it, auto-send
#    stops working and Nova falls back to "message is ready, tap Send
#    yourself" instead of guessing blindly at a screen coordinate.
#  - A real audio voice-NOTE (an actual recorded attachment) can't be
#    reliably auto-sent to a specific contact without you tapping through
#    WhatsApp's own share sheet - there's no ADB shortcut for that part.
#    So "send a voice message" here means: Nova listens, transcribes what
#    you say, and sends it as WhatsApp text - same contact-targeting
#    reliability as a typed message, just spoken instead of typed.
WHATSAPP_SEND_ID = "com.whatsapp:id/send"


def _whatsapp_tap_send(serial):
    """Best-effort: dump the current screen layout, find WhatsApp's Send
    button by resource-id, tap its center. Returns True only if it found
    and tapped a real button - never guesses a fixed coordinate."""
    ok, _out = _adb(["shell", "uiautomator", "dump", "/sdcard/nova_ui.xml"], serial=serial, timeout=6)
    if not ok:
        return False
    ok, xml_out = _adb(["shell", "cat", "/sdcard/nova_ui.xml"], serial=serial, timeout=6)
    if not ok or WHATSAPP_SEND_ID not in xml_out:
        return False
    m = re.search(
        re.escape(WHATSAPP_SEND_ID) + r'"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_out)
    if not m:
        return False
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    ok, _out = _adb(["shell", "input", "tap", str(cx), str(cy)], serial=serial, timeout=5)
    return ok


def send_whatsapp_text(target, message):
    """Returns (status, spoken_message). status: 'sent' (opened + auto-
    tapped Send), 'ready' (opened, pre-filled, needs your tap), or 'failed'."""
    if phone_state["backend"] == "none":
        phone_refresh_status()
    if phone_state["backend"] != "adb":
        return "failed", ("WhatsApp automation needs your phone connected over ADB - "
                          "Phone Link can't drive other apps. Say 'phone setup' for how.")

    number = clean_number(target)
    display_name = target
    if not number:
        res = lookup_contact(target)
        if res[0] in ("exact", "fuzzy"):
            display_name, number = res[1], res[2]
        elif res[0] == "ambiguous":
            return "failed", "I have several matches: " + ", ".join(res[1]) + ". Say the full name."
        else:
            return "failed", f"I couldn't find {target} in your contacts."

    serial = phone_state["serial"]
    url = f"https://wa.me/{number.lstrip('+')}?text={urllib.parse.quote(message)}"
    ok, out = _adb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                   serial=serial, timeout=10)
    if not ok or "Error" in out:
        return "failed", f"I couldn't open WhatsApp on your phone. ({out.strip()[:120]})"

    time.sleep(2.5)  # let WhatsApp finish opening the chat before hunting for the Send button
    if _whatsapp_tap_send(serial):
        log_activity(f"WhatsApp: sent to {display_name}")
        return "sent", f"Sent to {display_name} on WhatsApp."
    return "ready", (f"I've opened WhatsApp to {display_name} with your message typed in - "
                     f"I couldn't confirm the Send button, so please tap it on your phone.")


pending_whatsapp_voice = None  # {"target", "time"} while waiting for the message to speak


def whatsapp_reply(target, message):
    status, msg = send_whatsapp_text(target, message)
    return msg


WHATSAPP_TEXT_RE = re.compile(
    r"\bwhatsapp\s+(.+?)\s*[:,]\s*(.+)$"
    r"|\bsend\s+(.+?)\s+a\s+whatsapp\s+message\s+saying\s+(.+)$"
    r"|\btext\s+(.+?)\s+on\s+whatsapp\s+(.+)$"
    r"|\bwhatsapp\s+(.+?)\s+and\s+(?:say|tell (?:them|him|her))\s+(.+)$",
    re.IGNORECASE)
WHATSAPP_VOICE_RE = re.compile(
    r"\bsend\s+(.+?)\s+a\s+(?:voice message|voice note)(?:\s+on whatsapp)?\b"
    r"|\bwhatsapp\s+(.+?)\s+a\s+(?:voice message|voice note)\b", re.IGNORECASE)


def handle_whatsapp_command(cmd):
    """Returns a spoken reply if `cmd` was a WhatsApp command, else None."""
    global pending_whatsapp_voice

    if pending_whatsapp_voice:
        if time.time() - pending_whatsapp_voice["time"] > 30:
            pending_whatsapp_voice = None
        else:
            target = pending_whatsapp_voice["target"]
            pending_whatsapp_voice = None
            if is_decline(cmd):
                return "Okay, cancelled."
            return whatsapp_reply(target, cmd)

    m = WHATSAPP_TEXT_RE.search(cmd)
    if m:
        groups = m.groups()
        target, message = next((groups[i], groups[i + 1]) for i in (0, 2, 4, 6) if groups[i])
        return whatsapp_reply(target.strip(), message.strip().rstrip("."))

    m = WHATSAPP_VOICE_RE.search(cmd)
    if m:
        target = next(g for g in m.groups() if g).strip()
        pending_whatsapp_voice = {"target": target, "time": time.time()}
        return f"Sure - what should I tell {target}?"
    return None


# ---------- WhatsApp notification watcher (ADB only) ----------
# Polls the phone's notification shade (adb shell dumpsys notification) for
# new WhatsApp messages. Best-effort: dumpsys's notification content is
# redacted on some Android versions/OEM skins unless --noredact is honoured,
# so this quietly does nothing on a phone where it isn't. No new content is
# ever sent to your phone - this only reads what's already showing there.
WHATSAPP_NOTIFICATIONS = deque(maxlen=100)   # each: {id, sender, text, time, read}
_notif_id_counter = 0
_seen_whatsapp_notif_keys = set()
pending_notification_readout = None   # {"id", "time"} while waiting on "should I read it?"
NOTIF_PENDING_TIMEOUT = 30
_whatsapp_notif_watch_active = False
_whatsapp_notif_watch_thread = None


def _parse_whatsapp_notifications(raw):
    """Pulls (sender, text) pairs for com.whatsapp entries out of a raw
    'dumpsys notification' text dump. The exact layout varies by Android
    version, so this only assumes a NotificationRecord( ... key=0|pkg|... )
    header followed somewhere below by android.title= / android.text=."""
    found = []
    for chunk in raw.split("NotificationRecord(")[1:]:
        head = chunk[:chunk.find("\n")] if "\n" in chunk else chunk
        pkg_m = re.search(r"key=0\|([\w.]+)\|", head)
        if not pkg_m or pkg_m.group(1) != "com.whatsapp":
            continue
        key_m = re.search(r"key=(\S+)", head)
        title_m = re.search(r"android\.title=(.*)", chunk)
        text_m = re.search(r"android\.bigText=(.*)", chunk) or re.search(r"android\.text=(.*)", chunk)
        sender = (title_m.group(1).strip() if title_m else "")
        text = (text_m.group(1).strip() if text_m else "")
        # skip WhatsApp's own "3 new messages" summary card and redacted/empty entries
        if not sender or sender == "WhatsApp" or not text or text == "null" or "***" in text:
            continue
        found.append({"key": key_m.group(1) if key_m else f"{sender}:{text}", "sender": sender, "text": text})
    return found


def _whatsapp_notif_watch_loop():
    global pending_notification_readout, _notif_id_counter
    while _whatsapp_notif_watch_active:
        try:
            if phone_state["backend"] == "adb" and phone_state["serial"]:
                ok, out = _adb(["shell", "dumpsys", "notification", "--noredact"],
                               serial=phone_state["serial"], timeout=10)
                for item in (_parse_whatsapp_notifications(out) if ok else []):
                    if item["key"] in _seen_whatsapp_notif_keys:
                        continue
                    _seen_whatsapp_notif_keys.add(item["key"])
                    if len(_seen_whatsapp_notif_keys) > 500:
                        _seen_whatsapp_notif_keys.clear()  # simple periodic reset - harmless if a rare repeat slips through
                    _notif_id_counter += 1
                    WHATSAPP_NOTIFICATIONS.append({"id": _notif_id_counter, "sender": item["sender"],
                                                   "text": item["text"], "time": time.time(), "read": False})
                    log_activity(f"WhatsApp notification from {item['sender']}")
                    pending_notification_readout = {"id": _notif_id_counter, "time": time.time()}
                    speak_async(f"A WhatsApp message just came in from {item['sender']}. Should I read it out?")
        except Exception as e:
            print("WhatsApp notification watch error (non-fatal):", e)
        time.sleep(6)  # dumpsys is a bit heavy - no need to poll faster than this


def start_whatsapp_notification_watch():
    global _whatsapp_notif_watch_active, _whatsapp_notif_watch_thread
    if _whatsapp_notif_watch_active:
        return
    _whatsapp_notif_watch_active = True
    _whatsapp_notif_watch_thread = threading.Thread(target=_whatsapp_notif_watch_loop, daemon=True)
    _whatsapp_notif_watch_thread.start()


def stop_whatsapp_notification_watch():
    global _whatsapp_notif_watch_active
    _whatsapp_notif_watch_active = False


def handle_notification_command(cmd):
    """Returns a spoken reply if `cmd` was about notifications, else None."""
    global pending_notification_readout
    c = cmd.lower().strip().rstrip(".!?,")

    # Nova just asked "should I read it out?" - resolve that first.
    if pending_notification_readout:
        if time.time() - pending_notification_readout["time"] > NOTIF_PENDING_TIMEOUT:
            pending_notification_readout = None
        else:
            nid = pending_notification_readout["id"]
            pending_notification_readout = None
            entry = next((n for n in WHATSAPP_NOTIFICATIONS if n["id"] == nid), None)
            if is_affirmative(c):
                if entry is None:
                    return "That notification isn't there anymore."
                entry["read"] = True
                return f"{entry['sender']} says: {entry['text']}"
            if is_decline(c):
                return "Okay, it'll stay unread in your Notifications panel."

    if re.search(r"\b(read|check|any)\b.*\b(whatsapp )?notifications?\b", c) or \
       re.search(r"\bread (?:me )?(?:my|the) (?:whatsapp )?messages?\b", c) or \
       re.search(r"\bany new (?:whatsapp )?messages?\b", c):
        unread = [n for n in WHATSAPP_NOTIFICATIONS if not n["read"]]
        if not unread:
            return "No new WhatsApp notifications."
        for n in unread:
            n["read"] = True
        return " ".join(f"{n['sender']} says: {n['text']}." for n in unread[:5])

    if re.search(r"\bclear\b.*\bnotifications?\b", c):
        WHATSAPP_NOTIFICATIONS.clear()
        return "Cleared your notifications."

    return None


# ---------- Incoming call watcher ----------
# Separate, faster-polling thread that only cares about ring detection (the
# general status thread above polls slower to keep overhead down). Only runs
# anything when the ADB backend is active - Phone Link gives Nova no way to
# see an incoming call, so on that backend this thread just idles.
_ring_watch_active = False
_ring_watch_thread = None


def _ring_watch_loop():
    global pending_incoming_call
    last_state = "idle"
    while _ring_watch_active:
        try:
            if phone_state["backend"] == "adb" and phone_state["serial"]:
                serial = phone_state["serial"]
                state = _adb_call_state(serial)

                if state == "ringing" and last_state != "ringing":
                    number = _extract_incoming_number(serial)
                    name = reverse_lookup_contact(number)
                    label = name or number or "someone"
                    pending_incoming_call = {"name": label, "number": number, "time": time.time()}
                    log_activity(f"Incoming call: {label}")
                    speak_async(f"{label} is calling. Do you want to accept or decline?")

                elif state != "ringing" and last_state == "ringing" and pending_incoming_call:
                    # Ringing stopped before you answered through Nova - either it went
                    # to voicemail, you answered on the phone itself, or it was declined
                    # from the phone. Either way, the prompt is no longer relevant.
                    log_activity(f"Call from {pending_incoming_call['name']} no longer ringing")
                    pending_incoming_call = None

                last_state = state
            else:
                last_state = "idle"
        except Exception as e:
            print("Incoming call watch error (non-fatal):", e)
        time.sleep(1.0 if last_state == "ringing" else 1.5)


def start_ring_watch():
    global _ring_watch_active, _ring_watch_thread
    if _ring_watch_active:
        return
    _ring_watch_active = True
    _ring_watch_thread = threading.Thread(target=_ring_watch_loop, daemon=True)
    _ring_watch_thread.start()


def stop_ring_watch():
    global _ring_watch_active
    _ring_watch_active = False


# ---------- Phone Link (UI automation, best effort) ----------
def _phonelink_click(title_pattern, wait=2.5):
    """Try to press a button in the Phone Link window. Needs the optional
    'pywinauto' package; returns False (never raises) if anything's missing."""
    try:
        from pywinauto import Desktop
    except Exception:
        return False
    try:
        time.sleep(wait)
        for win in Desktop(backend="uia").windows(title_re=r".*(Phone Link|Your Phone).*"):
            try:
                btn = win.child_window(title_re=title_pattern, control_type="Button")
                if btn.exists(timeout=2):
                    btn.invoke()
                    return True
            except Exception:
                continue
    except Exception as e:
        print("Phone Link automation error:", e)
    return False


# ---------- Call actions ----------
def _adb_ok(ok, out):
    return ok and "Error" not in out and "Exception" not in out


def place_call(number):
    """Returns (status, message); status is 'placed', 'partial' or 'failed'."""
    num = clean_number(number)
    if not num:
        return "failed", "That doesn't look like a valid phone number."
    if phone_state["backend"] == "none":
        phone_refresh_status()
    backend = phone_state["backend"]

    if backend == "adb":
        uri = "tel:" + num.replace("+", "%2B")
        ok, out = _adb(["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", uri],
                       serial=phone_state["serial"], timeout=10)
        if _adb_ok(ok, out):
            return "placed", "Calling now."
        ok2, out2 = _adb(["shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri],
                         serial=phone_state["serial"], timeout=10)
        if _adb_ok(ok2, out2):
            return "partial", "I opened the dialer on your phone with the number, but it wouldn't let me press call."
        print("ADB call failed:", out, "|", out2)
        return "failed", "I couldn't reach your phone over ADB. Check the USB connection."

    if backend == "phonelink":
        try:
            os.startfile("tel:" + num)
        except Exception as e:
            print("tel: handler error:", e)
            return "failed", "I couldn't open Phone Link. Set Phone Link as your default for phone calls in Windows settings."
        if _phonelink_click(r"^Call$"):
            return "placed", "Calling now."
        return "partial", "I put the number into Phone Link. Press Call to dial."

    return "failed", "No phone is connected. Click Setup on the phone panel for options."


def _do_call(name, number):
    status, msg = place_call(number)
    if status == "placed":
        phone_state["last_call"] = f"{name} @ {time.strftime('%H:%M')}"
        log_activity(f"Phone: calling {name}")
        return f"Calling {name}."
    log_activity(f"Phone: {msg}")
    return msg


def call_target(text):
    """Resolve a spoken/typed target (contact name or number) and call it."""
    global pending_call
    t = text.strip().strip(".,!?")
    t = re.sub(r"^plus\s+", "+", t, flags=re.IGNORECASE)
    if re.fullmatch(r"\+?[\d\s\-\(\)\.]{3,}", t) and clean_number(t):
        return _do_call("that number", t)

    res = lookup_contact(t)
    if res[0] == "exact":
        return _do_call(res[1], res[2])
    if res[0] == "fuzzy":
        pending_call = {"name": res[1], "number": res[2], "time": time.time()}
        return f"Did you mean {res[1]}? Say yes to call."
    if res[0] == "ambiguous":
        return "I have several matches: " + ", ".join(res[1]) + ". Say the full name."
    if not all_contacts():
        return ("I don't have any contacts yet. Say 'sync my contacts', "
                "or 'save contact', a name, then the number.")
    return f"I couldn't find {t} in your contacts."


def hang_up_call():
    if phone_state["backend"] == "none":
        phone_refresh_status()
    if phone_state["backend"] == "adb":
        serial = phone_state["serial"]
        _adb(["shell", "input", "keyevent", "KEYCODE_ENDCALL"], serial=serial)
        time.sleep(0.8)
        if _adb_call_state(serial) not in ("idle", "unknown"):
            _adb(["shell", "telecom", "end-call"], serial=serial)
        log_activity("Phone: hang up")
        return "Call ended."
    if phone_state["backend"] == "phonelink":
        if _phonelink_click(r"^(End call|End|Hang up)$", wait=0.5):
            return "Call ended."
        return "I couldn't press End in Phone Link - please end it there."
    return "No phone is connected."


def answer_incoming_call():
    if phone_state["backend"] == "none":
        phone_refresh_status()
    if phone_state["backend"] == "adb":
        serial = phone_state["serial"]
        _adb(["shell", "input", "keyevent", "KEYCODE_CALL"], serial=serial)
        time.sleep(0.8)
        if _adb_call_state(serial) == "ringing":
            _adb(["shell", "telecom", "accept-ringing-call"], serial=serial)
        log_activity("Phone: answered")
        return "Answering."
    if phone_state["backend"] == "phonelink":
        if _phonelink_click(r"^(Answer|Accept)$", wait=0.5):
            return "Answering."
        return "I couldn't press Answer in Phone Link - please answer it there."
    return "No phone is connected."


def sync_contacts_reply():
    if phone_state["backend"] == "none":
        phone_refresh_status()
    if phone_state["backend"] != "adb":
        return "Importing contacts needs an Android phone connected over ADB."
    ok, out = _adb(["shell", "content", "query", "--uri", "content://com.android.contacts/data/phones",
                    "--projection", "display_name:data1"], serial=phone_state["serial"], timeout=30)
    if not ok:
        return "I couldn't read the contacts. On some phones you must allow 'USB debugging (security settings)'."
    synced = {}
    for line in out.splitlines():
        m = re.match(r"Row:\s*\d+\s+display_name=(.*), data1=(.*)$", line.strip())
        if not m:
            continue
        name, num = m.group(1).strip(), clean_number(m.group(2))
        if name and name != "NULL" and num:
            synced.setdefault(name, num)
    if not synced:
        return "I didn't find any contacts on the phone."
    _phone_config["synced"] = synced
    save_phone_config()
    phone_state["contacts"] = len(all_contacts())
    log_activity(f"Phone: synced {len(synced)} contacts")
    return f"Imported {len(synced)} contacts from your phone."


def phone_status_reply():
    phone_refresh_status()
    s = phone_state
    if s["backend"] == "adb":
        return f"Connected to {s['device']} over ADB. Call state: {s['call_state']}."
    if s["backend"] == "phonelink":
        return "Using Phone Link over Bluetooth. I can't read the call state in this mode."
    return "No phone is connected."


PHONE_HELP_TEXT = (
    "OPTION A - Android + USB (most reliable, fully hands-free)\n"
    "  1. Phone: Settings > About phone > tap 'Build number' 7 times.\n"
    "  2. Settings > Developer options > turn on USB debugging.\n"
    "  3. Plug the phone in and tap 'Allow' on the prompt.\n"
    "  4. Put Google's 'platform-tools' folder next to Nova (or on PATH).\n"
    "  Nova can then call, hang up, answer, and import your contacts.\n\n"
    "OPTION B - Bluetooth via Windows Phone Link (Android or iPhone)\n"
    "  1. Install/open 'Phone Link' and pair your phone over Bluetooth.\n"
    "  2. Enable 'Calls' in Phone Link and set it as the handler for phone calls.\n"
    "  3. Optional: pip install pywinauto so Nova can press Call for you.\n\n"
    "VOICE COMMANDS\n"
    "  'call John'  |  'call 555 123 4567'  |  'hang up'  |  'answer the call'\n"
    "  'decline'  |  'sync my contacts'  |  'save contact Mom 5551234567'  |  'phone status'\n\n"
    "INCOMING CALLS (ADB backend only)\n"
    "  When your phone rings, Nova announces the caller (by name if they're\n"
    "  in your contacts, otherwise the number) and asks 'accept or decline?'.\n"
    "  Say 'accept'/'answer'/'yes' or 'decline'/'reject'/'no'. You can also\n"
    "  say 'decline' at any moment a call is ringing or active, without\n"
    "  waiting for Nova to finish asking.\n\n"
    "WHATSAPP (ADB backend only)\n"
    "  'whatsapp John: running 10 minutes late'\n"
    "  'send Mom a whatsapp message saying I'll call tonight'\n"
    "  'send Priya a voice message' -> Nova asks what to say, then sends your\n"
    "  spoken reply as WhatsApp text (a real recorded audio note can't be\n"
    "  auto-sent to a specific contact without your own tap in WhatsApp's\n"
    "  share sheet, so this speaks-then-sends-as-text instead).\n"
    "  Nova opens the chat with your message ready and tries to tap Send for\n"
    "  you; if WhatsApp's layout ever changes underneath that, it'll tell you\n"
    "  the message is ready and ask you to tap Send yourself."
)

_PHONE_HANGUP_RE = re.compile(r"\b(hang up|end (the |this |my )?call|cut (the )?call|disconnect (the )?call)\b")
# Deliberately narrower than the incoming-prompt word list below: this fires
# any time (not just while Nova is asking), so it must not match ordinary
# sentences like "reject that idea" or "ignore that email".
_PHONE_DECLINE_RE = re.compile(
    r"^\s*decline\s*(the call|it|this call|my call)?\s*$"
    r"|\b(decline|reject)( the| this| my)?( incoming)?( phone)? call\b"
    r"|\breject it\b")
_PHONE_ANSWER_RE = re.compile(r"\b(answer|accept|pick up)( the| my| this| that)?( incoming)?( phone| call)\b")
_PHONE_SYNC_RE = re.compile(r"\b(sync|import|refresh|update) (my |the )?(phone )?contacts\b")
_PHONE_STATUS_RE = re.compile(r"\b(phone status|is my phone connected|phone connected)\b")
_PHONE_SAVE_RE = re.compile(
    r"^(?:save|add|store)\s+(?:a\s+)?(?:new\s+)?contact\s+(.+?)\s+"
    r"(?:(?:as|number|with number|with the number|at|is)\s+)?((?:\+|plus\s*)?\d[\d\s\-\(\)]{4,})$")
_PHONE_CALL_RE = re.compile(
    r"^(?:hey nova[, ]+)?(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:please\s+)?"
    r"(?:call|phone|ring|dial|make a call to|give a call to|place a call to)\s+(.+?)"
    r"(?:\s+(?:on|from|using|with|via|through|over)\s+(?:my\s+|the\s+)?"
    r"(?:phone|mobile|cell ?phone|cell|bluetooth|smartphone))?(?:\s+please)?$")

# Words that answer the "accept or decline?" prompt. Broader than the general
# is_affirmative/is_decline lists, since people say "accept"/"answer"/"take
# it" or "decline"/"reject"/"ignore" here, not just plain yes/no.
_INCOMING_ACCEPT_WORDS = ("yes", "yeah", "yep", "sure", "accept", "answer", "pick up", "take it", "take the call")
_INCOMING_DECLINE_WORDS = ("no", "nope", "decline", "reject", "ignore", "let it ring", "don't answer", "dont answer")


def handle_phone_command(cmd):
    """Returns a spoken reply if `cmd` was a phone command, else None."""
    global pending_call, pending_incoming_call
    c = cmd.lower().strip().rstrip(".!?,")

    # A call is ringing and Nova already asked "accept or decline?" - resolve
    # that first, before anything else, since it's the most time-sensitive.
    if pending_incoming_call:
        if time.time() - pending_incoming_call["time"] > PENDING_INCOMING_TIMEOUT:
            pending_incoming_call = None
        else:
            label = pending_incoming_call["name"]
            if any(_contains_trigger(c, w) for w in _INCOMING_ACCEPT_WORDS):
                pending_incoming_call = None
                answer_incoming_call()
                return f"Accepting the call from {label}."
            if any(_contains_trigger(c, w) for w in _INCOMING_DECLINE_WORDS):
                pending_incoming_call = None
                hang_up_call()
                return f"Declined the call from {label}."

    # "Decline" works any time a call is ringing or active, not just when
    # Nova is actively waiting on the prompt above.
    if _PHONE_DECLINE_RE.search(c):
        pending_incoming_call = None
        return hang_up_call()

    if pending_call:
        if time.time() - pending_call["time"] > PENDING_CALL_TIMEOUT:
            pending_call = None
        elif is_affirmative(c):
            p, pending_call = pending_call, None
            return _do_call(p["name"], p["number"])
        elif is_decline(c):
            pending_call = None
            return "Okay, cancelled."

    if _PHONE_HANGUP_RE.search(c):
        return hang_up_call()

    if _PHONE_ANSWER_RE.search(c):
        return answer_incoming_call()
    if _PHONE_SYNC_RE.search(c):
        return sync_contacts_reply()
    if _PHONE_STATUS_RE.search(c):
        return phone_status_reply()

    m = _PHONE_SAVE_RE.match(c)
    if m:
        name, number = m.group(1).strip(), m.group(2)
        if save_contact(name, number):
            return f"Saved {name.title()}."
        return "That number doesn't look valid."

    m = _PHONE_CALL_RE.match(c)
    if m:
        target = m.group(1).strip()
        # "call me Aibel" is the existing name-memory phrase, not a phone call
        if not target or re.match(r"^(me|you|us|it|that|this|him|her|them|myself|yourself)\b", target):
            return None
        return call_target(target)
    return None


load_phone_config()


# =====================================================================
# ---------- GUI (visible window, live mic visualizer) ----------
# =====================================================================
import tkinter as tk
from tkinter import ttk

COLOR_BG = "#050908"
COLOR_PANEL = "#0b1312"
COLOR_PANEL_BORDER = "#173330"
COLOR_CYAN = "#1ae8ff"          # tuned toward a J.A.R.V.I.S.-style electric cyan (was a softer teal)
COLOR_CYAN_DIM = "#0a3b44"
COLOR_GOLD = "#ffd700"          # secondary HUD accent - arc-reactor/pulse highlights
COLOR_SPEAKING = "#ffcf6b"
COLOR_TEXT = "#eafff9"
COLOR_TEXT_DIM = "#5f8f89"
COLOR_GOOD = "#39e6a6"
COLOR_WARN = "#ff6b6b"
COLOR_MOOD_SAD = "#6f9bd6"
COLOR_MOOD_ANXIOUS = "#c78bff"

# Adaptive Smart Cube: maps a detected mood to (color, icon, short label).
# Falls back to "neutral" for anything not in this table.
MOOD_STYLE = {
    "happy":   (COLOR_GOOD, "\u2600", "HAPPY"),
    "excited": (COLOR_SPEAKING, "\u2726", "EXCITED"),
    "sad":     (COLOR_MOOD_SAD, "\u2601", "SAD"),
    "angry":   (COLOR_WARN, "\u26A1", "ANGRY"),
    "anxious": (COLOR_MOOD_ANXIOUS, "\u3030", "ANXIOUS"),
    "tired":   (COLOR_TEXT_DIM, "\u263E", "TIRED"),
    "neutral": (COLOR_CYAN, "\u25C9", "NEUTRAL"),
}
# The rotating set of "faces" the cube's front panel cycles through, plus
# what each one shows and a short mood-adaptive note for the mood face.
CUBE_FACE_TYPES = ["mood", "time", "system", "subsystems"]
CUBE_MOOD_NOTES = {
    "happy": "Keeping replies upbeat.",
    "excited": "Matching your energy.",
    "sad": "Going gently for a bit.",
    "angry": "Dialing things down.",
    "anxious": "Keeping it calm and simple.",
    "tired": "Standing by quietly.",
    "neutral": "Nothing notable detected.",
}

VISUALIZER_SIZE = 260
VISUALIZER_BAR_COUNT = 28
VISUALIZER_HISTORY = deque([0.0] * VISUALIZER_BAR_COUNT, maxlen=VISUALIZER_BAR_COUNT)

# =====================================================================
# ---------- Hologram: an interactive, genuinely-3D wireframe display ----------
# =====================================================================
# Unlike the Adaptive Cube above (a flat isometric illusion - two
# parallelogram "faces" faked with stipple shading), this is real 3D:
# actual points in (x, y, z) space, rotated with proper rotation matrices
# and perspective-projected onto the canvas every frame. Drag to rotate,
# the on-screen arrows pan it, +/-/scroll zoom it.
#
# "Hologram anything": a small library of procedural shapes covers the
# common asks (cube, sphere, pyramid, torus, diamond, star, dna, atom),
# and anything that ISN'T one of those names is instead rendered as
# floating holographic TEXT - your own words, arced through 3D space and
# rotating with everything else. So quite literally any text can be
# holographed, not just the presets.
HOLOGRAM_SIZE = 380  # the canvas is its own size, bigger than VISUALIZER_SIZE (260) which the Cube/Visor still use
HOLOGRAM_SHAPES = ("cube", "pyramid", "sphere", "torus", "diamond", "star", "dna", "atom",
                  "cone", "cylinder", "heart", "saturn", "house", "arrow", "crystal", "spiral",
                  "lightning", "rocket", "reactor")
HOLOGRAM_SHAPE_LABELS = {"dna": "DNA", "lightning": "Bolt"}  # everything else is just .capitalize()
HOLOGRAM_ZOOM_MIN, HOLOGRAM_ZOOM_MAX = 0.4, 3.0
HOLOGRAM_PAN_LIMIT = 180.0
HOLOGRAM_AUTOROTATE_RESUME_SEC = 2.5  # how long manual rotation pauses auto-spin after you let go
HOLOGRAM_STATE = {
    "mode": "shape",      # "shape" or "text"
    "shape": "cube",
    "text": "NOVA",
    "rot_x": -18.0, "rot_y": 32.0,   # degrees
    "pan_x": 0.0, "pan_y": 0.0,      # pixels
    "zoom": 1.0,
    "auto_rotate": True,
    "last_interact": 0.0,            # time.time() of the last manual drag - gates auto-rotate resuming
}


def hologram_show_shape(name):
    name = (name or "").strip().lower()
    if name not in HOLOGRAM_SHAPES:
        # Looser match: the target just needs to MENTION a known shape as a
        # whole word ("dna helix", "a sphere please", "diamond ring") -
        # anything that doesn't is left to fall through to text mode.
        name = next((s for s in HOLOGRAM_SHAPES if re.search(rf"\b{s}\b", name)), None)
        if name is None:
            return False
    HOLOGRAM_STATE["mode"] = "shape"
    HOLOGRAM_STATE["shape"] = name
    return True


def hologram_show_text(text):
    text = (text or "").strip()
    if not text:
        return False
    HOLOGRAM_STATE["mode"] = "text"
    HOLOGRAM_STATE["text"] = text[:28]
    return True


def hologram_reset_view():
    HOLOGRAM_STATE["rot_x"], HOLOGRAM_STATE["rot_y"] = -18.0, 32.0
    HOLOGRAM_STATE["pan_x"] = HOLOGRAM_STATE["pan_y"] = 0.0
    HOLOGRAM_STATE["zoom"] = 1.0


def hologram_pan(dx, dy):
    HOLOGRAM_STATE["pan_x"] = max(-HOLOGRAM_PAN_LIMIT, min(HOLOGRAM_PAN_LIMIT, HOLOGRAM_STATE["pan_x"] + dx))
    HOLOGRAM_STATE["pan_y"] = max(-HOLOGRAM_PAN_LIMIT, min(HOLOGRAM_PAN_LIMIT, HOLOGRAM_STATE["pan_y"] + dy))


def hologram_zoom(factor):
    HOLOGRAM_STATE["zoom"] = max(HOLOGRAM_ZOOM_MIN, min(HOLOGRAM_ZOOM_MAX, HOLOGRAM_STATE["zoom"] * factor))


def _hologram_geometry(shape):
    """(points, edges) in local 3D space, centered near the origin with a
    roughly 1-1.5 unit radius - the renderer scales/projects from there.
    edges is a list of (i, j) point-index pairs to draw as connected
    lines; any point not referenced by an edge is still drawn, as a dot
    (used for the atom's nucleus)."""
    pts, edges = [], []

    if shape == "cube":
        r = 1.0
        for x in (-r, r):
            for y in (-r, r):
                for z in (-r, r):
                    pts.append((x, y, z))
        idx = {p: i for i, p in enumerate(pts)}
        for x in (-r, r):
            for y in (-r, r):
                edges.append((idx[(x, y, -r)], idx[(x, y, r)]))
        for x in (-r, r):
            for z in (-r, r):
                edges.append((idx[(x, -r, z)], idx[(x, r, z)]))
        for y in (-r, r):
            for z in (-r, r):
                edges.append((idx[(-r, y, z)], idx[(r, y, z)]))

    elif shape == "pyramid":
        r, h = 1.1, 1.3
        base = [(-r, -h / 2, -r), (r, -h / 2, -r), (r, -h / 2, r), (-r, -h / 2, r)]
        pts = base + [(0, h / 2, 0)]
        for i in range(4):
            edges.append((i, (i + 1) % 4))
            edges.append((i, 4))

    elif shape == "diamond":  # octahedron
        r = 1.3
        pts = [(r, 0, 0), (0, 0, r), (-r, 0, 0), (0, 0, -r), (0, r, 0), (0, -r, 0)]
        for i in range(4):
            edges.append((i, (i + 1) % 4))
            edges.append((i, 4))
            edges.append((i, 5))

    elif shape == "sphere":
        rings, segs, r = 7, 14, 1.3

        def idx(i, j):
            return i * segs + (j % segs)

        for i in range(rings + 1):
            lat = math.pi * (i / rings - 0.5)
            for j in range(segs):
                lon = 2 * math.pi * j / segs
                pts.append((r * math.cos(lat) * math.cos(lon), r * math.sin(lat), r * math.cos(lat) * math.sin(lon)))
        for i in range(rings + 1):
            for j in range(segs):
                edges.append((idx(i, j), idx(i, j + 1)))
                if i < rings:
                    edges.append((idx(i, j), idx(i + 1, j)))

    elif shape == "torus":
        big_r, small_r, rings, segs = 1.0, 0.42, 16, 10

        def idx(i, j):
            return (i % rings) * segs + (j % segs)

        for i in range(rings):
            theta = 2 * math.pi * i / rings
            for j in range(segs):
                phi = 2 * math.pi * j / segs
                pts.append(((big_r + small_r * math.cos(phi)) * math.cos(theta),
                           small_r * math.sin(phi),
                           (big_r + small_r * math.cos(phi)) * math.sin(theta)))
        for i in range(rings):
            for j in range(segs):
                edges.append((idx(i, j), idx(i, j + 1)))
                edges.append((idx(i, j), idx(i + 1, j)))

    elif shape == "star":
        # Two rings (outer spikes, inner valleys) in the XY plane, extruded
        # slightly along Z so it reads as solid rather than a flat decal.
        spikes, r_out, r_in, depth = 5, 1.3, 0.5, 0.25
        ring2d = []
        for i in range(spikes * 2):
            ang = math.pi / 2 + i * math.pi / spikes
            r = r_out if i % 2 == 0 else r_in
            ring2d.append((r * math.cos(ang), r * math.sin(ang)))
        pts = [(x, y, depth) for x, y in ring2d] + [(x, y, -depth) for x, y in ring2d]
        n = len(ring2d)
        for i in range(n):
            edges.append((i, (i + 1) % n))
            edges.append((n + i, n + (i + 1) % n))
            edges.append((i, n + i))

    elif shape == "dna":
        turns, pts_per_turn, height, radius = 2.5, 10, 2.6, 0.75
        total = int(turns * pts_per_turn)
        strand_a, strand_b = [], []
        for i in range(total + 1):
            ang = 2 * math.pi * (i / pts_per_turn)
            y = height * (i / total) - height / 2
            strand_a.append((radius * math.cos(ang), y, radius * math.sin(ang)))
            strand_b.append((radius * math.cos(ang + math.pi), y, radius * math.sin(ang + math.pi)))
        pts = strand_a + strand_b
        na = len(strand_a)
        for i in range(na - 1):
            edges.append((i, i + 1))
            edges.append((na + i, na + i + 1))
        for i in range(0, na, 3):  # rungs every 3rd step - every step is too dense to read
            edges.append((i, na + i))

    elif shape == "atom":
        pts.append((0, 0, 0))  # nucleus - index 0, no edges, drawn as a dot
        ring_segs = 28
        for tilt_x, tilt_z in ((0, 0), (1.05, 0), (1.05, math.pi / 2)):
            start = len(pts)
            for j in range(ring_segs):
                ang = 2 * math.pi * j / ring_segs
                x, y, z = 1.3 * math.cos(ang), 1.3 * math.sin(ang), 0.0
                y, z = y * math.cos(tilt_x) - z * math.sin(tilt_x), y * math.sin(tilt_x) + z * math.cos(tilt_x)
                x, y = x * math.cos(tilt_z) - y * math.sin(tilt_z), x * math.sin(tilt_z) + y * math.cos(tilt_z)
                pts.append((x, y, z))
            for j in range(ring_segs):
                edges.append((start + j, start + (j + 1) % ring_segs))

    elif shape == "cone":
        segs, r, h = 20, 1.1, 1.6
        pts = [(r * math.cos(2 * math.pi * i / segs), -h / 2, r * math.sin(2 * math.pi * i / segs))
              for i in range(segs)]
        pts.append((0, h / 2, 0))  # apex
        apex_i = segs
        for i in range(segs):
            edges.append((i, (i + 1) % segs))
            edges.append((i, apex_i))

    elif shape == "cylinder":
        segs, r, h = 20, 1.0, 1.6
        top = [(r * math.cos(2 * math.pi * i / segs), h / 2, r * math.sin(2 * math.pi * i / segs))
              for i in range(segs)]
        bot = [(r * math.cos(2 * math.pi * i / segs), -h / 2, r * math.sin(2 * math.pi * i / segs))
              for i in range(segs)]
        pts = top + bot
        for i in range(segs):
            edges.append((i, (i + 1) % segs))
            edges.append((segs + i, segs + (i + 1) % segs))
            if i % 2 == 0:  # not every strut - full ring of them is too dense to read
                edges.append((i, segs + i))

    elif shape == "heart":
        # Classic parametric heart curve, normalized to roughly a +/-1.2
        # unit radius, extruded along Z for a bit of thickness (same idea
        # as the star shape above).
        segs, depth = 26, 0.22
        ring2d = []
        for i in range(segs):
            t = 2 * math.pi * i / segs
            x = 16 * math.sin(t) ** 3
            y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
            ring2d.append((x / 13.0, y / 13.0))
        pts = [(x, y, depth) for x, y in ring2d] + [(x, y, -depth) for x, y in ring2d]
        n = len(ring2d)
        for i in range(n):
            edges.append((i, (i + 1) % n))
            edges.append((n + i, n + (i + 1) % n))
            edges.append((i, n + i))

    elif shape == "saturn":
        rings, segs, r = 5, 10, 0.85

        def idx(i, j):
            return i * segs + (j % segs)

        for i in range(rings + 1):
            lat = math.pi * (i / rings - 0.5)
            for j in range(segs):
                lon = 2 * math.pi * j / segs
                pts.append((r * math.cos(lat) * math.cos(lon), r * math.sin(lat), r * math.cos(lat) * math.sin(lon)))
        for i in range(rings + 1):
            for j in range(segs):
                edges.append((idx(i, j), idx(i, j + 1)))
                if i < rings:
                    edges.append((idx(i, j), idx(i + 1, j)))
        ring_segs, tilt, start = 36, 0.5, len(pts)
        for j in range(ring_segs):
            ang = 2 * math.pi * j / ring_segs
            x, y, z = 1.9 * math.cos(ang), 0.0, 1.1 * math.sin(ang)
            y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
            pts.append((x, y, z))
        for j in range(ring_segs):
            edges.append((start + j, start + (j + 1) % ring_segs))

    elif shape == "house":
        bw, bd, by0, by1 = 1.0, 1.0, -0.9, 0.0
        base_pts = [(x, y, z) for x in (-bw, bw) for y in (by0, by1) for z in (-bd, bd)]
        idx = {p: i for i, p in enumerate(base_pts)}
        pts = list(base_pts)
        for x in (-bw, bw):
            for y in (by0, by1):
                edges.append((idx[(x, y, -bd)], idx[(x, y, bd)]))
        for x in (-bw, bw):
            for z in (-bd, bd):
                edges.append((idx[(x, by0, z)], idx[(x, by1, z)]))
        for y in (by0, by1):
            for z in (-bd, bd):
                edges.append((idx[(-bw, y, z)], idx[(bw, y, z)]))
        roof_corners = [idx[(-bw, by1, -bd)], idx[(bw, by1, -bd)], idx[(bw, by1, bd)], idx[(-bw, by1, bd)]]
        pts.append((0, 1.0, 0))
        apex_i = len(pts) - 1
        for i in range(4):
            edges.append((roof_corners[i], roof_corners[(i + 1) % 4]))
            edges.append((roof_corners[i], apex_i))

    elif shape == "arrow":
        # Thin shaft (a narrow box) topped with a wider 4-point head ring
        # and an apex - reads as a pointer/cursor/compass-needle shape.
        pts = [(-0.12, -1.1, -0.12), (0.12, -1.1, -0.12), (0.12, -1.1, 0.12), (-0.12, -1.1, 0.12),
              (-0.12, 0.3, -0.12), (0.12, 0.3, -0.12), (0.12, 0.3, 0.12), (-0.12, 0.3, 0.12)]
        for i in range(4):
            edges.append((i, (i + 1) % 4))
            edges.append((4 + i, 4 + (i + 1) % 4))
            edges.append((i, 4 + i))
        head_ring = [(-0.4, 0.3, -0.4), (0.4, 0.3, -0.4), (0.4, 0.3, 0.4), (-0.4, 0.3, 0.4)]
        start = len(pts)
        pts.extend(head_ring)
        pts.append((0, 1.3, 0))  # apex
        apex_i = len(pts) - 1
        for i in range(4):
            edges.append((start + i, start + (i + 1) % 4))
            edges.append((start + i, apex_i))

    elif shape == "crystal":
        # Hexagonal bipyramid - a 6-sided "gem" facet look, distinct from
        # the 4-sided octahedron used for "diamond" above.
        segs, r, h = 6, 0.9, 1.4
        ring = [(r * math.cos(2 * math.pi * i / segs), 0, r * math.sin(2 * math.pi * i / segs))
               for i in range(segs)]
        pts = ring + [(0, h, 0), (0, -h, 0)]
        top_i, bot_i = segs, segs + 1
        for i in range(segs):
            edges.append((i, (i + 1) % segs))
            edges.append((i, top_i))
            edges.append((i, bot_i))

    elif shape == "spiral":
        turns, pts_per_turn, height, r0, r1 = 3.0, 12, 2.4, 0.2, 1.3
        total = int(turns * pts_per_turn)
        for i in range(total + 1):
            t = i / total
            ang = 2 * math.pi * turns * t
            rad = r0 + (r1 - r0) * t
            pts.append((rad * math.cos(ang), height * t - height / 2, rad * math.sin(ang)))
        for i in range(len(pts) - 1):
            edges.append((i, i + 1))

    elif shape == "lightning":
        profile = [(0.25, 1.3), (-0.15, 0.25), (0.2, 0.25), (-0.3, -1.3), (0.0, -0.1), (-0.25, -0.1)]
        depth = 0.18
        pts = [(x, y, depth) for x, y in profile] + [(x, y, -depth) for x, y in profile]
        n = len(profile)
        for i in range(n):
            edges.append((i, (i + 1) % n))
            edges.append((n + i, n + (i + 1) % n))
            edges.append((i, n + i))

    elif shape == "rocket":
        segs, r, body_h = 10, 0.45, 1.6
        top = [(r * math.cos(2 * math.pi * i / segs), body_h / 2, r * math.sin(2 * math.pi * i / segs))
              for i in range(segs)]
        bot = [(r * math.cos(2 * math.pi * i / segs), -body_h / 2, r * math.sin(2 * math.pi * i / segs))
              for i in range(segs)]
        pts = top + bot
        for i in range(segs):
            edges.append((i, (i + 1) % segs))
            edges.append((segs + i, segs + (i + 1) % segs))
            if i % 2 == 0:
                edges.append((i, segs + i))
        pts.append((0, body_h / 2 + 0.9, 0))  # nose apex
        apex_i = len(pts) - 1
        for i in range(0, segs, 2):
            edges.append((i, apex_i))
        for k in range(3):  # three fins around the base
            ang = 2 * math.pi * k / 3
            bx, bz = r * math.cos(ang), r * math.sin(ang)
            tip_x, tip_z = 0.9 * math.cos(ang), 0.9 * math.sin(ang)
            base_i = len(pts)
            pts.append((bx, -body_h / 2, bz))
            pts.append((tip_x, -body_h / 2 - 0.1, tip_z))
            pts.append((bx, -body_h / 2 + 0.5, bz))
            edges.append((base_i, base_i + 1))
            edges.append((base_i + 1, base_i + 2))
            edges.append((base_i + 2, base_i))

    elif shape == "reactor":
        # Arc-reactor look: a flat outer ring, a tilted inner ring, and a
        # compact octahedral "crystal" core at the center.
        segs = 40
        r_outer, r_inner, tilt = 1.3, 0.85, 0.3
        for j in range(segs):
            ang = 2 * math.pi * j / segs
            pts.append((r_outer * math.cos(ang), r_outer * math.sin(ang), 0.0))
        for j in range(segs):
            edges.append((j, (j + 1) % segs))
        start2 = len(pts)
        for j in range(segs):
            ang = 2 * math.pi * j / segs
            x, y, z = r_inner * math.cos(ang), r_inner * math.sin(ang), 0.0
            y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
            pts.append((x, y, z))
        for j in range(segs):
            edges.append((start2 + j, start2 + (j + 1) % segs))
        core_r, core_start = 0.42, len(pts)
        pts.extend([(core_r, 0, 0), (0, 0, core_r), (-core_r, 0, 0), (0, 0, -core_r),
                   (0, core_r, 0), (0, -core_r, 0)])
        for i in range(4):
            edges.append((core_start + i, core_start + (i + 1) % 4))
            edges.append((core_start + i, core_start + 4))
            edges.append((core_start + i, core_start + 5))

    else:
        pts, edges = [(0, 0, 0)], []

    return pts, edges


def _hologram_text_glyphs(text):
    """(char, (x, y, z)) pairs - each character of `text` positioned along
    a gentle arc in 3D space. A true 3D font extrusion isn't practical on
    a 2D canvas, but individually-projected, rotating glyphs still reads
    unmistakably as holographic text."""
    text = (text or "NOVA").strip() or "NOVA"
    n = len(text)
    spread = min(2.6, 0.32 * n + 0.6)
    glyphs = []
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        t = (i / (n - 1) - 0.5) if n > 1 else 0.0
        ang = t * spread
        x, z = math.sin(ang) * 1.6, math.cos(ang) * 1.6 - 1.6
        y = 0.14 * math.sin(i * 1.3)  # gentle vertical ripple instead of a flat line
        glyphs.append((ch, (x, y, z)))
    return glyphs


def _hologram_project(point, canvas_size, state):
    """Rotates a local 3D point by the hologram's current orientation and
    perspective-projects it to 2D canvas coordinates. Returns (sx, sy, depth) -
    depth is the rotated z, used by the renderer for a closer-is-brighter
    fade, same idea as real holographic depth cueing."""
    x, y, z = point
    ry, rx = math.radians(state["rot_y"]), math.radians(state["rot_x"])
    x1 = x * math.cos(ry) + z * math.sin(ry)
    z1 = -x * math.sin(ry) + z * math.cos(ry)
    y2 = y * math.cos(rx) - z1 * math.sin(rx)
    z2 = y * math.sin(rx) + z1 * math.cos(rx)

    fov = 4.2
    factor = fov / max(0.4, fov + z2)
    scale = canvas_size * 0.21 * state["zoom"]
    cx, cy = canvas_size / 2 + state["pan_x"], canvas_size / 2 + state["pan_y"]
    return cx + x1 * scale * factor, cy - y2 * scale * factor, z2


def _hologram_depth_color(z2, z_min, z_max):
    """Blends COLOR_CYAN_DIM (far) -> COLOR_CYAN (near) by depth."""
    span = max(0.001, z_max - z_min)
    t = max(0.0, min(1.0, 1.0 - (z2 - z_min) / span))  # nearer (smaller z2) -> brighter
    far = tuple(int(COLOR_CYAN_DIM[i:i + 2], 16) for i in (1, 3, 5))
    near = tuple(int(COLOR_CYAN[i:i + 2], 16) for i in (1, 3, 5))
    rgb = tuple(int(far[i] + (near[i] - far[i]) * t) for i in range(3))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


# =====================================================================
# ---------- Tactical Radar: a real sweep display, not a decoration ----------
# =====================================================================
# Green "friendly" blips are Nova's own subsystems, exactly the same five
# states the SUBSYSTEMS panel already tracks (Mic/Vision/Ad-Skip/Phone/
# Productive) - only shown when actually active. A red "contact" blip
# appears for a real recent error (the same last_error_time signal that
# drives the top-bar status dot and CORE STATUS badge), and fades off the
# scope after RADAR_ALERT_FADE_SEC once nothing's gone wrong for a while.
# Nothing here is fabricated data - if the radar is quiet, nothing is
# actually happening.
RADAR_SIZE = 220
RADAR_SWEEP_PERIOD_SEC = 4.0     # one full rotation
RADAR_ALERT_FADE_SEC = 25.0      # how long a red contact lingers after an error


def _radar_stable_angle(key, salt=47):
    """A deterministic (not random/jittery) angle in degrees for a given
    name, so each subsystem's blip sits in the same spot every frame
    instead of jumping around."""
    return (sum(ord(c) for c in key) * salt) % 360


_HOLOGRAM_WORD_RE = re.compile(r"\bhologram\w*\b", re.IGNORECASE)
_HOLOGRAM_TRIGGER_RE = re.compile(r"\bhologram\w*(?:\s+(?:of|the|a|an))*\s+(.+?)[.?!]*$", re.IGNORECASE)


def handle_hologram_command(cmd):
    """Returns a reply if `cmd` was a hologram command, else None. Shape
    names are case-insensitive; arbitrary target text keeps its original
    casing (for display) via matching against `cmd`, not a lowercased copy."""
    c = cmd.lower().strip().rstrip(".!?")
    if not _HOLOGRAM_WORD_RE.search(c):
        return None

    if re.search(r"\b(reset|recenter|center)\b", c):
        hologram_reset_view()
        return "Hologram view reset."
    if re.search(r"\b(stop|freeze|pause|hold)\b", c):
        HOLOGRAM_STATE["auto_rotate"] = False
        return "Holding the hologram still."
    if re.search(r"\b(spin|rotate|resume)\b", c):
        HOLOGRAM_STATE["auto_rotate"] = True
        return "Spinning it up."
    if "zoom in" in c:
        hologram_zoom(1.35)
        return "Zoomed in."
    if "zoom out" in c:
        hologram_zoom(1 / 1.35)
        return "Zoomed out."

    m = _HOLOGRAM_TRIGGER_RE.search(cmd)  # original casing, so holographed text keeps its case
    target = m.group(1).strip() if m else ""
    target = re.sub(r"^(?:me\s+)?(?:a\s+|an\s+|the\s+)?", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\s+(?:please|now|for me)$", "", target, flags=re.IGNORECASE).strip()

    if not target:
        hologram_show_shape("cube")
        return "Here's a hologram for you."
    if hologram_show_shape(target):
        article = "" if HOLOGRAM_STATE["shape"] == "lightning" else "a "
        return f"Here's a hologram of {article}{target.lower()}."
    hologram_show_text(target)
    return f"Holographing \u201c{target}\u201d for you."


# ---------- Real activity log (what the "system log" panel actually shows) ----------
ACTIVITY_LOG_MAXLEN = 8
activity_log = deque(maxlen=ACTIVITY_LOG_MAXLEN)
last_error_time = 0.0  # updated by the crash-logging hooks; drives the "system status" indicator


def log_activity(text):
    """Push one real event into the on-screen activity log, timestamped."""
    timestamp = time.strftime("%H:%M:%S")
    activity_log.append(f"[{timestamp}] {text}")


def get_system_stats():
    """Real CPU/memory/disk usage - not decoration, actual psutil readings."""
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(APP_DIR if OS_NAME.startswith("windows") else "/").percent
        return cpu, mem, disk
    except Exception as e:
        print("get_system_stats error:", e)
        return 0.0, 0.0, 0.0


_last_net_io = {"t": time.time(), "sent": 0, "recv": 0}


def get_network_speed():
    """Real upload/download rate in KB/s, measured since the last call (not
    a running average since boot - psutil only gives cumulative byte
    counters, so the rate has to be derived from two samples over time)."""
    global _last_net_io
    try:
        counters = psutil.net_io_counters()
        now = time.time()
        dt = max(now - _last_net_io["t"], 0.001)
        up = max(0.0, (counters.bytes_sent - _last_net_io["sent"]) / dt / 1024)
        down = max(0.0, (counters.bytes_recv - _last_net_io["recv"]) / dt / 1024)
        _last_net_io = {"t": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
        return up, down
    except Exception as e:
        print("Network speed read error:", e)
        return 0.0, 0.0


def get_battery_status():
    """Returns (percent, plugged_in) or (None, None) on a desktop with no
    battery - callers should handle that case rather than assume a laptop."""
    try:
        batt = psutil.sensors_battery()
        if batt is None:
            return None, None
        return batt.percent, batt.power_plugged
    except Exception as e:
        print("Battery read error:", e)
        return None, None


def get_disk_usage_all():
    """Per-drive usage. On Windows this checks every letter A-Z for a real
    mounted drive (C:\\, D:\\, etc. - however many the machine actually
    has); elsewhere it's just '/'. Returns [(label, percent), ...]."""
    drives = []
    try:
        if OS_NAME.startswith("windows"):
            import string
            for letter in string.ascii_uppercase:
                path = f"{letter}:\\"
                if os.path.exists(path):
                    try:
                        drives.append((f"{letter}:", psutil.disk_usage(path).percent))
                    except Exception:
                        continue
        else:
            drives.append(("/", psutil.disk_usage("/").percent))
    except Exception as e:
        print("Disk enumeration error:", e)
    return drives


def get_gpu_usage():
    """NVIDIA-only, best-effort: shells out to nvidia-smi (bundled with any
    NVIDIA driver install) for GPU utilization %. Returns None - not 0 -
    on anything else (AMD/Intel GPUs, or no nvidia-smi on PATH), so callers
    can honestly show 'N/A' instead of a fake reading."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, creationflags=_NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


_app_start_time = time.time()
CPU_HISTORY = deque([0.0] * 40, maxlen=40)  # sampled once per render tick for the sparkline panel


def get_uptime_str():
    """How long THIS Nova process has been running - not the OS uptime,
    since psutil.boot_time() would just show the computer's own uptime,
    which is a different (and less useful) number to show here."""
    seconds = int(time.time() - _app_start_time)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m"


# =====================================================================
# ---------- Background system-stats cache (fixes "Not Responding") ----------
# =====================================================================
# get_gpu_usage() spawns a subprocess (nvidia-smi) and get_disk_usage_all()
# calls os.path.exists()/psutil.disk_usage() on every possible drive letter
# - including ones that don't have real media in them (an empty DVD drive,
# an unreachable mapped network drive). On Windows, checking a drive letter
# like that can silently block for several SECONDS. The render loop used to
# call both of these directly, every 200ms, on the same thread that runs
# the Tkinter event loop - so a single slow/offline drive letter was enough
# to freeze mouse clicks and repaints and make Windows mark the whole
# window "(Not Responding)". They (and the other stat reads, for
# consistency) now run here instead, on their own thread, on a slower
# cadence - the render loop just reads the latest cached numbers, which is
# always instant.
SYSTEM_STATS_STATE = {
    "cpu": 0.0, "mem": 0.0, "disk": 0.0, "gpu": None,
    "batt_pct": None, "batt_plugged": None,
    "net_up": 0.0, "net_down": 0.0,
    "drives": [], "uptime": "0d 0h 0m",
}
SYSTEM_STATS_SAMPLE_SEC = 1.5
_system_stats_thread = None
_system_stats_stop = threading.Event()


def _system_stats_loop():
    while not _system_stats_stop.is_set():
        try:
            cpu, mem, disk = get_system_stats()
            SYSTEM_STATS_STATE["cpu"] = cpu
            SYSTEM_STATS_STATE["mem"] = mem
            SYSTEM_STATS_STATE["disk"] = disk
            CPU_HISTORY.append(cpu)
            SYSTEM_STATS_STATE["gpu"] = get_gpu_usage()
            batt_pct, plugged = get_battery_status()
            SYSTEM_STATS_STATE["batt_pct"] = batt_pct
            SYSTEM_STATS_STATE["batt_plugged"] = plugged
            up, down = get_network_speed()
            SYSTEM_STATS_STATE["net_up"] = up
            SYSTEM_STATS_STATE["net_down"] = down
            SYSTEM_STATS_STATE["drives"] = get_disk_usage_all()
            SYSTEM_STATS_STATE["uptime"] = get_uptime_str()
        except Exception as e:
            print("System stats loop error:", e)
        _system_stats_stop.wait(SYSTEM_STATS_SAMPLE_SEC)


def start_system_monitor():
    global _system_stats_thread
    if _system_stats_thread is not None and _system_stats_thread.is_alive():
        return
    _system_stats_stop.clear()
    _system_stats_thread = threading.Thread(target=_system_stats_loop, daemon=True)
    _system_stats_thread.start()


def stop_system_monitor():
    _system_stats_stop.set()


def _rounded_rect(canvas, x1, y1, x2, y2, r=10, **kwargs):
    """tkinter has no native rounded-rectangle primitive - approximate one
    with a polygon so panels don't look like plain sharp-cornered boxes."""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Panel:
    """One dashboard card, styled as an angular HUD panel - cut (chamfered)
    corners plus small targeting-bracket accents, instead of a plain
    rectangle. The card auto-sizes to whatever content is actually packed
    into .body, instead of trusting a guessed fixed height - a fixed guess
    is exactly what caused labels to spill past the border into whatever
    sits below once a panel's content grew past that guess. `height` is
    still accepted for compatibility with existing call sites, but is
    otherwise ignored."""

    def __init__(self, parent, title, subtitle, width, height=None, cut=16):
        self.canvas = tk.Canvas(parent, width=width, bg=COLOR_BG, highlightthickness=0)
        self.frame = self.canvas  # external code calls .frame.pack(...) - Canvas supports that too
        self._width, self._cut = width, cut

        content = tk.Frame(self.canvas, bg=COLOR_PANEL)
        header = tk.Frame(content, bg=COLOR_PANEL)
        header.pack(fill="x", padx=(cut + 6, cut + 6), pady=(6, 4))
        tk.Label(header, text=title, font=("Consolas", 9, "bold"),
                 fg=COLOR_TEXT_DIM, bg=COLOR_PANEL).pack(side="left")
        tk.Label(header, text=subtitle, font=("Consolas", 8),
                 fg=COLOR_TEXT_DIM, bg=COLOR_PANEL).pack(side="right")

        self.body = tk.Frame(content, bg=COLOR_PANEL)
        self.body.pack(fill="both", expand=True, padx=(cut + 6, cut + 6), pady=(0, 10))

        self._content_id = self.canvas.create_window(0, 0, anchor="nw", window=content, width=width)
        self._content = content
        content.bind("<Configure>", self._resize)
        self.canvas.after(1, self._resize)  # first paint, once children have their real sizes

    def _resize(self, _event=None):
        self._content.update_idletasks()
        needed_h = max(40, self._content.winfo_reqheight())
        self.canvas.config(height=needed_h)
        self._draw_border(self._width, needed_h, self._cut)

    def _draw_border(self, width, height, cut):
        self.canvas.delete("border")
        pts = [
            cut, 0, width - cut, 0, width, cut,
            width, height - cut, width - cut, height,
            cut, height, 0, height - cut, 0, cut,
        ]
        self.canvas.create_polygon(pts, fill=COLOR_PANEL, outline=COLOR_PANEL_BORDER, width=1, tags="border")
        b = 12
        for cx0, cy0, sx, sy in (
            (2, 2, 1, 1), (width - 2, 2, -1, 1),
            (2, height - 2, 1, -1), (width - 2, height - 2, -1, -1),
        ):
            self.canvas.create_line(cx0, cy0 + sy * b, cx0, cy0, cx0 + sx * b, cy0,
                                     fill=COLOR_CYAN, width=2, tags="border")
        self.canvas.tag_lower("border")


class NovaGUI:
    def __init__(self, root):
        global _gui_root_ref
        self.root = root
        _gui_root_ref = root  # lets background threads schedule GUI work (e.g. diagrams) via root.after()
        self.root.title("Nova")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("1180x820")
        self.root.minsize(900, 500)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # (engine on/off state is read live from the global stop_listening,
        # not tracked separately here - see _update_start_button)

        start_system_monitor()  # off-thread now - see SYSTEM_STATS_STATE for why

        self._build_top_bar()
        self._build_bottom_bar()
        self._build_body()
        self.core_rotation = 0.0  # degrees; advanced each frame for the Arc Core's rotating rings
        self._render_loop()
        self._video_render_loop()
        self._hologram_render_loop()
        self._radar_render_loop()
        self._timer_render_loop()
        show_screen_picker()  # the small draggable "explain this" handle - on by default

    # -----------------------------------------------------------------
    # Layout construction
    # -----------------------------------------------------------------
    def _build_top_bar(self):
        bar = tk.Frame(self.root, bg=COLOR_BG, height=60)
        bar.pack(fill="x", padx=20, pady=(15, 5))

        tk.Label(bar, text="NOVA", font=("Consolas", 18, "bold"),
                 fg=COLOR_CYAN, bg=COLOR_BG).pack(side="left")
        tk.Label(bar, text="  Personal assistant, online", font=("Consolas", 9),
                 fg=COLOR_TEXT_DIM, bg=COLOR_BG).pack(side="left", padx=(4, 0))

        right = tk.Frame(bar, bg=COLOR_BG)
        right.pack(side="right")

        # Real user name from memory, not a fictional owner name
        name = get_fact("name") or "Guest"
        initial = name[0].upper()
        avatar = tk.Label(right, text=initial, font=("Consolas", 11, "bold"),
                           fg=COLOR_BG, bg=COLOR_CYAN, width=2, height=1)
        avatar.pack(side="right", padx=(10, 0))
        tk.Label(right, text=name.upper(), font=("Consolas", 10, "bold"),
                 fg=COLOR_TEXT, bg=COLOR_BG).pack(side="right", padx=(0, 8))

        # Real, clickable: gear opens the actual log file
        gear = tk.Label(right, text="\u2699", font=("Segoe UI Symbol", 13),
                         fg=COLOR_TEXT_DIM, bg=COLOR_BG, cursor="hand2")
        gear.pack(side="right", padx=10)
        gear.bind("<Button-1>", lambda e: self._tray_open_log_from_gui())

        # Real, clickable: bell shows what Nova actually remembers about you
        bell = tk.Label(right, text="\U0001F514", font=("Segoe UI Emoji", 12),
                         fg=COLOR_TEXT_DIM, bg=COLOR_BG, cursor="hand2")
        bell.pack(side="right", padx=10)
        bell.bind("<Button-1>", lambda e: self._show_memory_popup())

        self.time_label = tk.Label(right, text="", font=("Consolas", 10),
                                    fg=COLOR_TEXT_DIM, bg=COLOR_BG)
        self.time_label.pack(side="right", padx=(0, 15))
        self.date_label = tk.Label(right, text="", font=("Consolas", 10, "bold"),
                                    fg=COLOR_CYAN, bg=COLOR_BG)
        self.date_label.pack(side="right", padx=15)

        self.status_dot_label = tk.Label(right, text="\u25CF STATUS", font=("Consolas", 9, "bold"),
                                          fg=COLOR_GOOD, bg=COLOR_BG)
        self.status_dot_label.pack(side="right", padx=15)

        # ---- Tactical status badges: real readouts dressed as HUD chips,
        # not decoration - CORE STATUS mirrors the dot above, ARC POWER is
        # battery (or CPU headroom on a desktop with no battery), DEFENSE
        # GRID reflects whether Ad-Skip is actually running. ----
        badge_bar = tk.Frame(self.root, bg=COLOR_BG)
        badge_bar.pack(fill="x", padx=20, pady=(0, 8))

        def _make_badge(label):
            f = tk.Frame(badge_bar, bg="#0c1a18", highlightbackground=COLOR_PANEL_BORDER, highlightthickness=1)
            f.pack(side="left", padx=(0, 10), ipadx=8, ipady=3)
            tk.Label(f, text=label, font=("Consolas", 8), fg=COLOR_TEXT_DIM, bg="#0c1a18").pack(side="left")
            val = tk.Label(f, text="--", font=("Consolas", 8, "bold"), fg=COLOR_GOOD, bg="#0c1a18")
            val.pack(side="left", padx=(4, 0))
            return val

        self.core_status_badge = _make_badge("CORE STATUS:")
        self.arc_power_badge = _make_badge("ARC POWER:")
        self.defense_grid_badge = _make_badge("DEFENSE GRID:")

    def _update_status_badges(self):
        healthy = time.time() - last_error_time >= 10
        self.core_status_badge.config(text="NOMINAL" if healthy else "ATTENTION",
                                      fg=COLOR_GOOD if healthy else COLOR_WARN)

        batt_pct = SYSTEM_STATS_STATE["batt_pct"]
        power_val = batt_pct if batt_pct is not None else max(0.0, 100.0 - SYSTEM_STATS_STATE["cpu"])
        power_label = f"{power_val:.0f}%" + ("" if batt_pct is not None else " (CPU HEADROOM)")
        self.arc_power_badge.config(text=power_label, fg=COLOR_GOLD if power_val > 20 else COLOR_WARN)

        self.defense_grid_badge.config(text="ACTIVE" if _ad_skipper_active else "STANDBY",
                                       fg=COLOR_CYAN if _ad_skipper_active else COLOR_TEXT_DIM)

    def _build_body(self):
        # The dashboard's panels (esp. the right column) add up to well
        # over 1300px tall - taller than most laptop screens. Rather than
        # letting the window clip content with no way to reach it, the
        # whole body lives inside a scrollable canvas: a fixed-size window
        # just shows less at once and the user scrolls (mouse wheel or the
        # scrollbar) to see the rest, instead of panels being cut off.
        outer = tk.Frame(self.root, bg=COLOR_BG)
        outer.pack(fill="both", expand=True, padx=(20, 4), pady=10)

        body_canvas = tk.Canvas(outer, bg=COLOR_BG, highlightthickness=0)
        vscroll = tk.Scrollbar(outer, orient="vertical", command=body_canvas.yview)
        body_canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        body_canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(body_canvas, bg=COLOR_BG)
        body_window = body_canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(_event=None):
            body_canvas.configure(scrollregion=body_canvas.bbox("all"))
        body.bind("<Configure>", _on_body_configure)

        def _on_canvas_configure(event):
            # Keep the inner frame at least as wide as the visible canvas
            # so the center column can still expand to fill the width.
            body_canvas.itemconfig(body_window, width=max(event.width, 1))
        body_canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            if getattr(event, "num", None) == 4:
                body_canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                body_canvas.yview_scroll(3, "units")
            else:
                body_canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")

        # bind_all so the wheel scrolls the body no matter which child
        # widget (label, entry, canvas...) the cursor happens to be over
        body_canvas.bind_all("<MouseWheel>", _on_mousewheel)   # Windows / macOS
        body_canvas.bind_all("<Button-4>", _on_mousewheel)      # Linux scroll up
        body_canvas.bind_all("<Button-5>", _on_mousewheel)      # Linux scroll down

        # Faint HUD background grid + corner framing brackets, drawn first
        # so every column packed after it sits visually on top. Purely
        # atmospheric - it doesn't intercept clicks since the opaque
        # column frames cover it wherever there's actually a widget.
        BODY_W, BODY_H = 1140, 1180
        bg_canvas = tk.Canvas(body, width=BODY_W, height=BODY_H, bg=COLOR_BG, highlightthickness=0)
        bg_canvas.place(x=0, y=0)
        grid_step = 42
        for x in range(0, BODY_W, grid_step):
            bg_canvas.create_line(x, 0, x, BODY_H, fill="#0d1a18", width=1)
        for y in range(0, BODY_H, grid_step):
            bg_canvas.create_line(0, y, BODY_W, y, fill="#0d1a18", width=1)
        bracket = 34
        for cx0, cy0, sx, sy in (
            (4, 4, 1, 1), (BODY_W - 4, 4, -1, 1),
            (4, BODY_H - 4, 1, -1), (BODY_W - 4, BODY_H - 4, -1, -1),
        ):
            bg_canvas.create_line(cx0, cy0 + sy * bracket, cx0, cy0, cx0 + sx * bracket, cy0,
                                   fill=COLOR_CYAN_DIM, width=2)

        left_col = tk.Frame(body, bg=COLOR_BG)
        left_col.pack(side="left", fill="y")
        center_col = tk.Frame(body, bg=COLOR_BG)
        center_col.pack(side="left", fill="both", expand=True, padx=15)
        right_col = tk.Frame(body, bg=COLOR_BG)
        right_col.pack(side="right", fill="y")

        # ---- Left column: mic level, system resources, browser status ----
        self.mic_panel = Panel(left_col, "SYSTEM //", "MIC LEVEL", 280, 190)
        self.mic_panel.frame.pack(pady=(0, 12))
        self.mic_canvas = tk.Canvas(self.mic_panel.body, bg=COLOR_PANEL, highlightthickness=0)
        self.mic_canvas.pack(fill="both", expand=True)

        self.resource_panel = Panel(left_col, "SYSTEM //", "RESOURCES", 280, 205)
        self.resource_panel.frame.pack(pady=(0, 12))
        self.cpu_label = self._make_stat_row(self.resource_panel.body, "CPU LOAD")
        self.mem_label = self._make_stat_row(self.resource_panel.body, "MEMORY")
        self.disk_label = self._make_stat_row(self.resource_panel.body, "DISK")
        self.gpu_label = self._make_stat_row(self.resource_panel.body, "GPU")

        self.browser_panel = Panel(left_col, "SYSTEM //", "BROWSER", 280, 170)
        self.browser_panel.frame.pack(pady=(0, 12))
        self.browser_status_label = tk.Label(self.browser_panel.body, text="", font=("Consolas", 10),
                                              fg=COLOR_TEXT, bg=COLOR_PANEL, justify="left", anchor="w")
        self.browser_status_label.pack(fill="both", expand=True)

        # ---- Phone control panel: status, quick dial, setup ----
        self.phone_panel = Panel(left_col, "SYSTEM //", "PHONE", 280, 232)
        self.phone_panel.frame.pack()
        self.phone_dot = tk.Label(self.phone_panel.body, text="\u25CF DISCONNECTED", font=("Consolas", 9, "bold"),
                                   fg=COLOR_WARN, bg=COLOR_PANEL, anchor="w")
        self.phone_dot.pack(fill="x")
        self.phone_detail_label = tk.Label(self.phone_panel.body, text="No phone connected", font=("Consolas", 9),
                                            fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, justify="left", anchor="w",
                                            wraplength=250)
        self.phone_detail_label.pack(fill="x", pady=(4, 8))

        # Incoming-call banner: hidden by default, shown only while a call is
        # ringing (packed/unpacked in _update_phone_panel, not destroyed).
        self.phone_incoming_frame = tk.Frame(self.phone_panel.body, bg="#241a10",
                                              highlightbackground=COLOR_WARN, highlightthickness=1)
        self.phone_incoming_label = tk.Label(self.phone_incoming_frame, text="", font=("Consolas", 9, "bold"),
                                              fg=COLOR_WARN, bg="#241a10", justify="left", anchor="w",
                                              wraplength=245)
        self.phone_incoming_label.pack(fill="x", padx=6, pady=(6, 4))
        incoming_btn_row = tk.Frame(self.phone_incoming_frame, bg="#241a10")
        incoming_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(incoming_btn_row, text="Accept", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg=COLOR_GOOD, relief="flat", padx=10, command=self._phone_accept_incoming).pack(side="left")
        tk.Button(incoming_btn_row, text="Decline", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg="#e05a5a", relief="flat", padx=10, command=self._phone_decline_incoming).pack(
                  side="left", padx=(6, 0))
        # not packed here - _update_phone_panel packs/unpacks it as calls come and go

        dial_row = tk.Frame(self.phone_panel.body, bg=COLOR_PANEL)
        self.phone_dial_row = dial_row
        dial_row.pack(fill="x")
        self.phone_entry = tk.Entry(dial_row, font=("Consolas", 10), bg="#141c28",
                                     fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.phone_entry.insert(0, "Name or number")
        self.phone_entry.bind("<FocusIn>", self._phone_entry_focus_in)
        self.phone_entry.bind("<Return>", lambda e: self._phone_dial())
        self.phone_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        tk.Button(dial_row, text="Call", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg=COLOR_GOOD, relief="flat", padx=10, command=self._phone_dial).pack(side="left")

        btn_row = tk.Frame(self.phone_panel.body, bg=COLOR_PANEL)
        btn_row.pack(fill="x", pady=(8, 0))
        tk.Button(btn_row, text="Hang Up", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._phone_hangup).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Sync Contacts", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._phone_sync).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Setup", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._phone_setup_popup).pack(side="left")

        # ---- WhatsApp panel: status + quick-send, sitting under Phone since
        # it shares the same ADB link and contact book ----
        self.whatsapp_panel = Panel(left_col, "SYSTEM //", "WHATSAPP", 280, 210)
        self.whatsapp_panel.frame.pack(pady=(12, 0))
        self.whatsapp_dot = tk.Label(self.whatsapp_panel.body, text="\u25CF NEEDS ADB", font=("Consolas", 9, "bold"),
                                      fg=COLOR_WARN, bg=COLOR_PANEL, anchor="w")
        self.whatsapp_dot.pack(fill="x")
        self.whatsapp_status_label = tk.Label(self.whatsapp_panel.body, text="Connect your phone over ADB to send.",
                                               font=("Consolas", 9), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL,
                                               justify="left", anchor="w", wraplength=250)
        self.whatsapp_status_label.pack(fill="x", pady=(4, 8))

        self.whatsapp_to_entry = tk.Entry(self.whatsapp_panel.body, font=("Consolas", 10), bg="#141c28",
                                           fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.whatsapp_to_entry.insert(0, "Name or number")
        self.whatsapp_to_entry.bind("<FocusIn>", lambda e: self._entry_clear_placeholder(
            self.whatsapp_to_entry, "Name or number"))
        self.whatsapp_to_entry.pack(fill="x", ipady=4, pady=(0, 6))

        msg_row = tk.Frame(self.whatsapp_panel.body, bg=COLOR_PANEL)
        msg_row.pack(fill="x")
        self.whatsapp_msg_entry = tk.Entry(msg_row, font=("Consolas", 10), bg="#141c28",
                                            fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.whatsapp_msg_entry.insert(0, "Message")
        self.whatsapp_msg_entry.bind("<FocusIn>", lambda e: self._entry_clear_placeholder(
            self.whatsapp_msg_entry, "Message"))
        self.whatsapp_msg_entry.bind("<Return>", lambda e: self._whatsapp_send())
        self.whatsapp_msg_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        tk.Button(msg_row, text="Send", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_GOOD,
                  relief="flat", padx=10, command=self._whatsapp_send).pack(side="left")

        tk.Button(self.whatsapp_panel.body, text="Voice Note Instead", font=("Consolas", 8), fg=COLOR_TEXT,
                  bg="#141c28", relief="flat", padx=8, command=self._whatsapp_voice_note).pack(
                  fill="x", pady=(8, 0))

        # ---- NOTIFICATIONS: WhatsApp messages Nova caught from your phone's
        # notification shade, so you can read (or re-read) them any time,
        # not just in the moment Nova asked about them ----
        self.notif_panel = Panel(left_col, "SYSTEM //", "NOTIFICATIONS", 280, 240)
        self.notif_panel.frame.pack(pady=(12, 0))
        nb = self.notif_panel.body
        self.notif_status_label = tk.Label(nb, text="No notifications yet.", font=("Consolas", 9),
                                           fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="w")
        self.notif_status_label.pack(fill="x", pady=(0, 4))
        self.notif_list = tk.Listbox(nb, font=("Consolas", 9), bg="#141c28", fg=COLOR_TEXT, height=6,
                                     selectbackground=COLOR_CYAN_DIM, selectforeground=COLOR_TEXT,
                                     highlightthickness=0, relief="flat", activestyle="none")
        self.notif_list.pack(fill="both", expand=True)
        self.notif_list.bind("<Double-Button-1>", lambda e: self._notif_read_selected())
        notif_btns = tk.Frame(nb, bg=COLOR_PANEL)
        notif_btns.pack(fill="x", pady=(6, 0))
        tk.Button(notif_btns, text="Read", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=8, command=self._notif_read_selected).pack(side="left", padx=(0, 6))
        tk.Button(notif_btns, text="Read All", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=8, command=self._notif_read_all).pack(side="left", padx=(0, 6))
        tk.Button(notif_btns, text="Clear", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=8, command=self._notif_clear).pack(side="left")
        self._notif_list_ids = []
        self._notif_last_shown = None

        # ---- TIME TOOLS: stopwatch + countdown timers ----
        self.time_panel = Panel(left_col, "SYSTEM //", "TIME TOOLS", 280, 322)
        self.time_panel.frame.pack(pady=(12, 0))
        tb = self.time_panel.body
        self.sw_label = tk.Label(tb, text="00:00.0", font=("Consolas", 26, "bold"),
                                 fg=COLOR_CYAN, bg=COLOR_PANEL, anchor="w")
        self.sw_label.pack(fill="x")
        sw_row = tk.Frame(tb, bg=COLOR_PANEL)
        sw_row.pack(fill="x", pady=(2, 0))
        self.sw_start_btn = tk.Button(sw_row, text="Start", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                                      bg=COLOR_CYAN, relief="flat", padx=12, command=self._sw_toggle)
        self.sw_start_btn.pack(side="left", padx=(0, 6))
        tk.Button(sw_row, text="Lap", font=("Segoe UI", 9), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=10, command=self._sw_lap).pack(side="left", padx=(0, 6))
        tk.Button(sw_row, text="Reset", font=("Segoe UI", 9), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=10, command=sw_reset).pack(side="left")
        self.sw_laps_label = tk.Label(tb, text="", font=("Consolas", 8), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL,
                                      anchor="w", justify="left", height=2)
        self.sw_laps_label.pack(fill="x", pady=(4, 0))
        tk.Frame(tb, bg=COLOR_PANEL_BORDER, height=1).pack(fill="x", pady=(2, 6))
        cd_row = tk.Frame(tb, bg=COLOR_PANEL)
        cd_row.pack(fill="x")
        self.cd_entry = tk.Entry(cd_row, font=("Consolas", 10), bg="#141c28", fg=COLOR_TEXT, width=9,
                                 insertbackground=COLOR_CYAN, relief="flat")
        self.cd_entry.insert(0, "5m")
        self.cd_entry.bind("<Return>", lambda e: self._cd_start())
        self.cd_entry.pack(side="left", ipady=4, padx=(0, 6))
        tk.Button(cd_row, text="Start Countdown", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_CYAN,
                  relief="flat", padx=8, command=self._cd_start).pack(side="left")
        preset_row = tk.Frame(tb, bg=COLOR_PANEL)
        preset_row.pack(fill="x", pady=(6, 0))
        for label, secs in (("1m", 60), ("5m", 300), ("10m", 600), ("25m", 1500)):
            tk.Button(preset_row, text=label, font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                      padx=7, command=lambda s=secs: cd_add(s)).pack(side="left", padx=(0, 5))
        tk.Button(preset_row, text="Clear", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=7, command=cd_clear).pack(side="right")
        self.cd_label = tk.Label(tb, text="No timers running", font=("Consolas", 10, "bold"),
                                 fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="nw", justify="left")
        self.cd_label.pack(fill="both", expand=True, pady=(8, 0))
        self._sw_btn_text = "Start"

        # ---- SCHEDULE: type "call mom at 6pm" / "standup every weekday 9am" ----
        self.sched_panel = Panel(left_col, "SYSTEM //", "SCHEDULE", 280, 358)
        self.sched_panel.frame.pack(pady=(12, 0))
        sb = self.sched_panel.body
        add_row = tk.Frame(sb, bg=COLOR_PANEL)
        add_row.pack(fill="x")
        self._sched_placeholder = "e.g. call mom at 6pm"
        self.sched_entry = tk.Entry(add_row, font=("Consolas", 10), bg="#141c28", fg=COLOR_TEXT_DIM,
                                    insertbackground=COLOR_CYAN, relief="flat")
        self.sched_entry.insert(0, self._sched_placeholder)
        self.sched_entry.bind("<FocusIn>", self._sched_entry_focus_in)
        self.sched_entry.bind("<Return>", lambda e: self._sched_add_click())
        self.sched_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        tk.Button(add_row, text="Add", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_GOOD,
                  relief="flat", padx=10, command=self._sched_add_click).pack(side="left")
        self.sched_status_label = tk.Label(sb, text="Reminders speak up when they're due.", font=("Consolas", 8),
                                           fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="nw", justify="left",
                                           wraplength=250, height=2)
        self.sched_status_label.pack(fill="x", pady=(6, 4))
        self.sched_list = tk.Listbox(sb, font=("Consolas", 9), bg="#141c28", fg=COLOR_TEXT, height=8,
                                     selectbackground=COLOR_CYAN_DIM, selectforeground=COLOR_TEXT,
                                     highlightthickness=0, relief="flat", activestyle="none")
        self.sched_list.pack(fill="both", expand=True)
        sched_btns = tk.Frame(sb, bg=COLOR_PANEL)
        sched_btns.pack(fill="x", pady=(6, 0))
        tk.Button(sched_btns, text="Delete", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                  padx=8, command=self._sched_delete_click).pack(side="left", padx=(0, 6))
        tk.Button(sched_btns, text="Snooze 10m", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._sched_snooze_click).pack(side="left", padx=(0, 6))
        tk.Button(sched_btns, text="Clear all", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._sched_clear_click).pack(side="left")
        self._sched_list_ids = []
        self._sched_shown_key = None
        self._sched_msg, self._sched_msg_color = "Reminders speak up when they're due.", COLOR_TEXT_DIM
        start_scheduler()

        # ---- Center: the core visualizer + controls + command bar ----
        tk.Label(center_col, text="Good day. How may I assist?",
                 font=("Segoe UI", 14, "bold"), fg=COLOR_TEXT, bg=COLOR_BG).pack(pady=(10, 6))

        self.start_button = tk.Button(
            center_col, text="Start", font=("Segoe UI", 11, "bold"),
            fg=COLOR_BG, bg=COLOR_CYAN, activebackground=COLOR_CYAN,
            activeforeground=COLOR_BG, relief="flat", padx=26, pady=6,
            command=self.on_start_stop,
        )
        self.start_button.pack(pady=(0, 10))

        self.canvas = tk.Canvas(center_col, width=VISUALIZER_SIZE, height=VISUALIZER_SIZE,
                                 bg=COLOR_BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda e: self.on_start_stop())  # click the visualizer to start/stop too

        self.status_label = tk.Label(center_col, text="", font=("Segoe UI", 12),
                                      fg=COLOR_TEXT_DIM, bg=COLOR_BG)
        self.status_label.pack(pady=(6, 14))

        # Real typed-command bar - an actual input, not decoration
        cmd_frame = tk.Frame(center_col, bg=COLOR_BG)
        cmd_frame.pack(fill="x", pady=(0, 10))
        self.command_entry = tk.Entry(cmd_frame, font=("Consolas", 11), bg=COLOR_PANEL,
                                       fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.command_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.command_entry.bind("<Return>", lambda e: self._submit_typed_command())
        tk.Button(cmd_frame, text="Send", font=("Segoe UI", 10, "bold"), fg=COLOR_BG,
                  bg=COLOR_CYAN, relief="flat", padx=14, command=self._submit_typed_command).pack(side="left")

        # Real embedded camera feed - whatever the vision system actually
        # sees, shown right in the window instead of a separate OpenCV popup
        self.video_panel = Panel(center_col, "SYSTEM //", "CAMERA FEED", VISUALIZER_SIZE + 20, 220)
        self.video_panel.frame.pack(pady=(4, 0))
        self.video_label = tk.Label(self.video_panel.body, text="Camera inactive",
                                     font=("Consolas", 10), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL)
        self.video_label.pack(fill="both", expand=True)
        self._video_photo = None  # keep a reference alive - Tkinter drops images without one

        # ---- Tactical Radar: a real sweep display - green blips are
        # Nova's own active subsystems, a red blip is a genuine recent
        # error (fades out once things are quiet again). See
        # _radar_stable_angle / _draw_radar for how it's actually driven. ----
        self.radar_panel = Panel(center_col, "SYSTEM //", "TACTICAL RADAR", RADAR_SIZE + 20, RADAR_SIZE + 90)
        self.radar_panel.frame.pack(pady=(12, 0))
        rb = self.radar_panel.body
        self.radar_canvas = tk.Canvas(rb, width=RADAR_SIZE, height=RADAR_SIZE,
                                      bg="#061018", highlightthickness=0)
        self.radar_canvas.pack(pady=(0, 6))
        radar_info_row = tk.Frame(rb, bg=COLOR_PANEL)
        radar_info_row.pack(fill="x")
        self.radar_friendly_label = tk.Label(radar_info_row, text="0 ACTIVE", font=("Consolas", 8, "bold"),
                                             fg=COLOR_GOOD, bg=COLOR_PANEL)
        self.radar_friendly_label.pack(side="left")
        self.radar_contacts_label = tk.Label(radar_info_row, text="0 ALERTS", font=("Consolas", 8, "bold"),
                                             fg=COLOR_TEXT_DIM, bg=COLOR_PANEL)
        self.radar_contacts_label.pack(side="right")
        self._radar_pinged_error_time = 0.0  # de-dupes the alert "ping" beep to once per new error

        # ---- Adaptive Smart Cube: a live isometric cube whose front face
        # rotates through several real readouts (mood, time, system load,
        # active subsystems) every 60s, and jumps straight to the mood
        # face the instant a new mood is detected - so it reacts to you,
        # not just the clock. Its glow color also tracks your mood live.
        self.cube_panel = Panel(center_col, "SYSTEM //", "ADAPTIVE CUBE", VISUALIZER_SIZE + 20, 200)
        self.cube_panel.frame.pack(pady=(12, 0))
        self.cube_canvas = tk.Canvas(self.cube_panel.body, bg=COLOR_PANEL, highlightthickness=0)
        self.cube_canvas.pack(fill="both", expand=True)
        self.cube_face_index = 0
        self.cube_pending_face_index = 0
        self.cube_flip_frames_left = 0
        self.cube_last_switch_time = time.time()
        self.cube_last_mood = last_detected_emotion

        # ---- HOLOGRAM: a real interactive 3D wireframe display, not an
        # isometric illusion like the cube above. Left-drag to rotate,
        # right-drag or the arrow buttons to pan, scroll wheel or +/- to
        # zoom. "Hologram anything": the preset shapes cover the common
        # asks, and typing/saying anything else holograms those words as
        # floating 3D text instead. ----
        self.hologram_panel = Panel(center_col, "SYSTEM //", "HOLOGRAM", HOLOGRAM_SIZE + 20, 560)
        self.hologram_panel.frame.pack(pady=(12, 0))
        hb = self.hologram_panel.body
        self.hologram_canvas = tk.Canvas(hb, width=HOLOGRAM_SIZE, height=HOLOGRAM_SIZE,
                                         bg=COLOR_PANEL, highlightthickness=0, cursor="fleur")
        self.hologram_canvas.pack(pady=(0, 8))
        self._hologram_drag = {"mode": None, "x": 0, "y": 0}
        self.hologram_canvas.bind("<ButtonPress-1>", self._hologram_drag_start_rotate)
        self.hologram_canvas.bind("<B1-Motion>", self._hologram_drag_move)
        self.hologram_canvas.bind("<ButtonRelease-1>", self._hologram_drag_end)
        self.hologram_canvas.bind("<ButtonPress-3>", self._hologram_drag_start_pan)
        self.hologram_canvas.bind("<B3-Motion>", self._hologram_drag_move)
        self.hologram_canvas.bind("<ButtonRelease-3>", self._hologram_drag_end)
        self.hologram_canvas.bind("<MouseWheel>", self._hologram_scroll_zoom)       # Windows/macOS
        self.hologram_canvas.bind("<Button-4>", lambda e: self._hologram_zoom_click(1.15))   # Linux scroll up
        self.hologram_canvas.bind("<Button-5>", lambda e: self._hologram_zoom_click(1 / 1.15))  # Linux scroll down

        # Laid out automatically in rows of 5 so adding more shapes to
        # HOLOGRAM_SHAPES later doesn't need any GUI changes to match.
        self.hologram_shape_buttons = {}
        per_row = 5
        for row_start in range(0, len(HOLOGRAM_SHAPES), per_row):
            row = tk.Frame(hb, bg=COLOR_PANEL)
            row.pack(fill="x", pady=(0, 3))
            for name in HOLOGRAM_SHAPES[row_start:row_start + per_row]:
                label = HOLOGRAM_SHAPE_LABELS.get(name, name.capitalize())
                btn = tk.Button(row, text=label, font=("Consolas", 8), fg=COLOR_TEXT,
                                bg="#141c28", relief="flat", padx=2,
                                command=lambda n=name: self._hologram_pick_shape(n))
                btn.pack(side="left", fill="x", expand=True, padx=1)
                self.hologram_shape_buttons[name] = btn
        tk.Frame(hb, bg=COLOR_PANEL, height=5).pack()

        text_row = tk.Frame(hb, bg=COLOR_PANEL)
        text_row.pack(fill="x", pady=(0, 8))
        self.hologram_text_entry = tk.Entry(text_row, font=("Consolas", 9), bg="#141c28", fg=COLOR_TEXT,
                                            insertbackground=COLOR_CYAN, relief="flat")
        self.hologram_text_entry.insert(0, "Hologram anything...")
        self.hologram_text_entry.bind("<FocusIn>", lambda e: self._entry_clear_placeholder(
            self.hologram_text_entry, "Hologram anything..."))
        self.hologram_text_entry.bind("<Return>", lambda e: self._hologram_text_go())
        self.hologram_text_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        tk.Button(text_row, text="Go", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_CYAN,
                  relief="flat", padx=10, command=self._hologram_text_go).pack(side="left")

        ctrl_row = tk.Frame(hb, bg=COLOR_PANEL)
        ctrl_row.pack(fill="x")
        for sym, cmd_fn in (
            ("\u25C0", lambda: hologram_pan(-20, 0)), ("\u25B2", lambda: hologram_pan(0, -20)),
            ("\u25BC", lambda: hologram_pan(0, 20)), ("\u25B6", lambda: hologram_pan(20, 0)),
        ):
            tk.Button(ctrl_row, text=sym, font=("Consolas", 9), fg=COLOR_TEXT, bg="#141c28",
                      relief="flat", padx=8, command=cmd_fn).pack(side="left", padx=2)
        tk.Button(ctrl_row, text="\u2212", font=("Consolas", 9, "bold"), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=lambda: self._hologram_zoom_click(1 / 1.2)
                  ).pack(side="left", padx=(12, 2))
        tk.Button(ctrl_row, text="Reset", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=6, command=self._hologram_reset_click).pack(side="left", padx=2)
        tk.Button(ctrl_row, text="+", font=("Consolas", 9, "bold"), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=lambda: self._hologram_zoom_click(1.2)).pack(side="left", padx=2)
        tk.Button(ctrl_row, text="\u26A1 Pulse", font=("Consolas", 8, "bold"), fg=COLOR_BG, bg=COLOR_GOLD,
                  relief="flat", padx=6, command=self._hologram_pulse_click).pack(side="right", padx=(8, 2))
        self.hologram_spin_button = tk.Button(ctrl_row, text="\u23F8", font=("Consolas", 9), fg=COLOR_TEXT,
                                              bg="#141c28", relief="flat", padx=8,
                                              command=self._hologram_toggle_spin)
        self.hologram_spin_button.pack(side="right", padx=2)
        self._hologram_last_shape_shown = None

        # ---- NEWS: India + World headlines, refreshed hourly, with an
        # optional AI-written digest when Ollama is available ----
        self.news_panel = Panel(center_col, "SYSTEM //", "INDIA + WORLD NEWS", VISUALIZER_SIZE + 220, 300)
        self.news_panel.frame.pack(pady=(12, 0))
        nwb = self.news_panel.body
        news_top = tk.Frame(nwb, bg=COLOR_PANEL)
        news_top.pack(fill="x")
        self.news_tab_var = tk.StringVar(value="india")
        self.news_india_btn = tk.Button(news_top, text="INDIA", font=("Consolas", 8, "bold"), fg=COLOR_BG,
                                        bg=COLOR_CYAN, relief="flat", padx=10,
                                        command=lambda: self._news_set_tab("india"))
        self.news_india_btn.pack(side="left", padx=(0, 4))
        self.news_world_btn = tk.Button(news_top, text="WORLD", font=("Consolas", 8, "bold"), fg=COLOR_TEXT,
                                        bg="#141c28", relief="flat", padx=10,
                                        command=lambda: self._news_set_tab("world"))
        self.news_world_btn.pack(side="left")
        tk.Button(news_top, text="\u21BB", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_CYAN,
                  relief="flat", padx=6, command=lambda: refresh_news_async()).pack(side="right")
        self.news_updated_label = tk.Label(nwb, text="Fetching the latest...", font=("Consolas", 8),
                                           fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="w")
        self.news_updated_label.pack(fill="x", pady=(4, 4))
        # A Listbox truncates every line to whatever fits in one row with no
        # wrap - that's what was clipping long headlines. A Text widget with
        # wrap="word" shows the full headline on as many lines as it needs;
        # the panel grows taller to fit (Panel auto-sizes to its content),
        # instead of hiding text off the edge.
        self.news_list = tk.Text(nwb, font=("Consolas", 9), bg="#141c28", fg=COLOR_TEXT, height=9,
                                 wrap="word", cursor="hand2", highlightthickness=0, relief="flat",
                                 padx=6, pady=4, spacing3=6)
        self.news_list.pack(fill="both", expand=True)
        self.news_list.tag_configure("headline", foreground=COLOR_TEXT)
        self.news_list.tag_configure("source", foreground=COLOR_TEXT_DIM)
        self.news_list.config(state="disabled")
        self._news_item_links = []
        tk.Button(nwb, text="\U0001F916 Hear AI Digest", font=("Consolas", 8, "bold"), fg=COLOR_BG,
                  bg=COLOR_GOOD, relief="flat", padx=8, command=self._news_read_digest).pack(fill="x", pady=(6, 0))
        self._news_last_shown = None

        # ---- Right column: language, CPU history, subsystem status, quick actions, activity log ----

        # ---- Language panel: what Nova listens for, and what it replies in ----
        self.lang_panel = Panel(right_col, "SYSTEM //", "LANGUAGE", 300, 218)
        self.lang_panel.frame.pack(pady=(0, 12))
        lgb = self.lang_panel.body
        tk.Label(lgb, text="LISTEN", font=("Consolas", 8, "bold"), fg=COLOR_TEXT_DIM,
                 bg=COLOR_PANEL, anchor="w").pack(fill="x")
        listen_row = tk.Frame(lgb, bg=COLOR_PANEL)
        listen_row.pack(fill="x", pady=(2, 8))
        self.lang_listen_buttons = {}
        for code, label in (("auto", "AUTO"), ("en", "EN"), ("hi", "HI"), ("ml", "ML")):
            btn = tk.Button(listen_row, text=label, font=("Consolas", 8, "bold"), fg=COLOR_TEXT,
                             bg="#141c28", relief="flat", padx=7, pady=3,
                             command=lambda c=code: self._lang_set_listen(c))
            btn.pack(side="left", padx=(0, 4))
            self.lang_listen_buttons[code] = btn
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Nova.TCombobox", fieldbackground="#141c28", background="#141c28",
                        foreground=COLOR_TEXT, arrowcolor=COLOR_CYAN, bordercolor=COLOR_PANEL_BORDER)
        style.map("Nova.TCombobox", fieldbackground=[("readonly", "#141c28")],
                  foreground=[("readonly", COLOR_TEXT)])
        # every language beyond the four quick buttons above - Arabic, Tamil,
        # Urdu, Kannada, Bengali, Spanish, Italian, French, German and more
        # as they're added to LANGUAGES - is reachable through this dropdown
        self._lang_more_map = {f"{L['name']} ({L['native']})": code for code, L in LANGUAGES.items()
                               if code not in ("en", "hi", "ml")}
        self.lang_listen_more = ttk.Combobox(listen_row, state="readonly", style="Nova.TCombobox",
                                             values=["More..."] + sorted(self._lang_more_map), width=13)
        self.lang_listen_more.set("More...")
        self.lang_listen_more.bind("<<ComboboxSelected>>", self._lang_listen_more_chosen)
        self.lang_listen_more.pack(side="left", padx=(4, 0))

        tk.Label(lgb, text="REPLY", font=("Consolas", 8, "bold"), fg=COLOR_TEXT_DIM,
                 bg=COLOR_PANEL, anchor="w").pack(fill="x")
        reply_row = tk.Frame(lgb, bg=COLOR_PANEL)
        reply_row.pack(fill="x", pady=(2, 8))
        self.lang_reply_buttons = {}
        for code, label in (("match", "MATCH"), ("en", "EN"), ("hi", "HI"), ("ml", "ML")):
            btn = tk.Button(reply_row, text=label, font=("Consolas", 8, "bold"), fg=COLOR_TEXT,
                             bg="#141c28", relief="flat", padx=7, pady=3,
                             command=lambda c=code: self._lang_set_reply(c))
            btn.pack(side="left", padx=(0, 4))
            self.lang_reply_buttons[code] = btn
        self.lang_reply_more = ttk.Combobox(reply_row, state="readonly", style="Nova.TCombobox",
                                            values=["More..."] + sorted(self._lang_more_map), width=13)
        self.lang_reply_more.set("More...")
        self.lang_reply_more.bind("<<ComboboxSelected>>", self._lang_reply_more_chosen)
        self.lang_reply_more.pack(side="left", padx=(4, 0))

        self.lang_status_label = tk.Label(lgb, text="", font=("Consolas", 8), fg=COLOR_TEXT_DIM,
                                           bg=COLOR_PANEL, anchor="nw", justify="left", wraplength=270)
        self.lang_status_label.pack(fill="both", expand=True)
        self._lang_last_shown = None

        self.cpu_history_panel = Panel(right_col, "SYSTEM //", "CPU HISTORY", 300, 100)
        self.cpu_history_panel.frame.pack(pady=(0, 12))
        self.cpu_history_canvas = tk.Canvas(self.cpu_history_panel.body, bg=COLOR_PANEL, highlightthickness=0)
        self.cpu_history_canvas.pack(fill="both", expand=True)

        self.subsystem_panel = Panel(right_col, "SYSTEM //", "SUBSYSTEMS", 300, 155)
        self.subsystem_panel.frame.pack(pady=(0, 12))
        self.subsystem_rows = {}
        for name in ("Mic", "Vision", "Ad-Skip", "Phone", "Productive"):
            row = tk.Frame(self.subsystem_panel.body, bg=COLOR_PANEL)
            row.pack(fill="x", pady=3)
            dot = tk.Label(row, text="\u25CF", font=("Consolas", 10), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL)
            dot.pack(side="left")
            tk.Label(row, text=" " + name, font=("Consolas", 9), fg=COLOR_TEXT_DIM,
                     bg=COLOR_PANEL).pack(side="left")
            state_lbl = tk.Label(row, text="OFF", font=("Consolas", 9, "bold"), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL)
            state_lbl.pack(side="right")
            self.subsystem_rows[name] = (dot, state_lbl)

        # ---- DJ Mode panel: status + quick controls ----
        self.dj_panel = Panel(right_col, "SYSTEM //", "DJ MODE", 300, 118)
        self.dj_panel.frame.pack(pady=(0, 12))
        self.dj_status_label = tk.Label(self.dj_panel.body, text="Off", font=("Consolas", 9),
                                         fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, justify="left", anchor="w",
                                         wraplength=260)
        self.dj_status_label.pack(fill="x", pady=(0, 6))
        dj_entry_row = tk.Frame(self.dj_panel.body, bg=COLOR_PANEL)
        dj_entry_row.pack(fill="x")
        self.dj_activity_entry = tk.Entry(dj_entry_row, font=("Consolas", 9), bg="#141c1a",
                                           fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.dj_activity_entry.insert(0, "studying/gaming/chilling")
        self.dj_activity_entry.bind("<FocusIn>", self._dj_entry_focus_in)
        self.dj_activity_entry.bind("<Return>", lambda e: self._dj_start())
        self.dj_activity_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 6))
        tk.Button(dj_entry_row, text="Start", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg=COLOR_GOOD, relief="flat", padx=8, command=self._dj_start).pack(side="left")
        dj_btn_row = tk.Frame(self.dj_panel.body, bg=COLOR_PANEL)
        dj_btn_row.pack(fill="x", pady=(6, 0))
        tk.Button(dj_btn_row, text="Skip", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c1a",
                  relief="flat", padx=8, command=self._dj_skip).pack(side="left", padx=(0, 6))
        tk.Button(dj_btn_row, text="Stop", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c1a",
                  relief="flat", padx=8, command=self._dj_stop).pack(side="left")

        # ---- Orbital Simulation panel: globe canvas + status + controls ----
        self.orbital_panel = Panel(right_col, "SYSTEM //", "ORBITAL SIM", 300, 232)
        self.orbital_panel.frame.pack(pady=(0, 12))
        self.orbital_canvas = tk.Canvas(self.orbital_panel.body, bg=COLOR_PANEL, highlightthickness=0, height=100)
        self.orbital_canvas.pack(fill="x", pady=(0, 6))
        self.orbital_status_label = tk.Label(self.orbital_panel.body, text="Inactive", font=("Consolas", 9),
                                              fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, justify="left", anchor="w",
                                              wraplength=260)
        self.orbital_status_label.pack(fill="x", pady=(0, 6))
        orbital_entry_row = tk.Frame(self.orbital_panel.body, bg=COLOR_PANEL)
        orbital_entry_row.pack(fill="x")
        self.orbital_mode_entry = tk.Entry(orbital_entry_row, font=("Consolas", 9), bg="#141c1a",
                                            fg=COLOR_TEXT, insertbackground=COLOR_CYAN, relief="flat")
        self.orbital_mode_entry.insert(0, "iss/rocket/asteroid")
        self.orbital_mode_entry.bind("<FocusIn>", self._orbital_entry_focus_in)
        self.orbital_mode_entry.bind("<Return>", lambda e: self._orbital_start())
        self.orbital_mode_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 6))
        tk.Button(orbital_entry_row, text="Track", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg=COLOR_GOOD, relief="flat", padx=8, command=self._orbital_start).pack(side="left")
        tk.Button(self.orbital_panel.body, text="Stop", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c1a",
                  relief="flat", padx=8, command=self._orbital_stop).pack(anchor="w", pady=(6, 0))

        # ---- Kochi Area Weather panel: pick any area, live conditions ----
        self.weather_panel = Panel(right_col, "SYSTEM //", "KOCHI WEATHER", 300, 236)
        self.weather_panel.frame.pack(pady=(0, 12))
        wbody = self.weather_panel.body
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Nova.TCombobox", fieldbackground="#141c28", background="#141c28",
                        foreground=COLOR_TEXT, arrowcolor=COLOR_CYAN, bordercolor=COLOR_PANEL_BORDER)
        style.map("Nova.TCombobox", fieldbackground=[("readonly", "#141c28")],
                  foreground=[("readonly", COLOR_TEXT)])
        top_row = tk.Frame(wbody, bg=COLOR_PANEL)
        top_row.pack(fill="x")
        self.weather_area_var = tk.StringVar(value=WEATHER_STATE["area"])
        self.weather_combo = ttk.Combobox(top_row, textvariable=self.weather_area_var, state="readonly",
                                          values=sorted(KOCHI_AREAS), style="Nova.TCombobox", width=20)
        self.weather_combo.pack(side="left")
        self.weather_combo.bind("<<ComboboxSelected>>", self._weather_area_chosen)
        tk.Button(top_row, text="\u21BB", font=("Segoe UI", 10, "bold"), fg=COLOR_BG, bg=COLOR_CYAN,
                  relief="flat", padx=6, command=lambda: refresh_weather_async()).pack(side="right")
        self.weather_temp_label = tk.Label(wbody, text="--\u00b0C", font=("Consolas", 26, "bold"),
                                           fg=COLOR_CYAN, bg=COLOR_PANEL, anchor="w")
        self.weather_temp_label.pack(fill="x", pady=(6, 0))
        self.weather_cond_label = tk.Label(wbody, text="Loading...", font=("Consolas", 10),
                                           fg=COLOR_TEXT, bg=COLOR_PANEL, anchor="w")
        self.weather_cond_label.pack(fill="x")
        self.weather_detail_label = tk.Label(wbody, text="", font=("Consolas", 9), fg=COLOR_TEXT_DIM,
                                             bg=COLOR_PANEL, anchor="w", justify="left")
        self.weather_detail_label.pack(fill="x", pady=(4, 0))
        self._weather_last_shown = None
        self._weather_next_refresh = 0
        self._weather_synced_area = WEATHER_STATE["area"]

        # ---- Flood Watch panel: rain-based risk level for the selected area ----
        self.flood_panel = Panel(right_col, "SYSTEM //", "FLOOD WATCH", 300, 124)
        self.flood_panel.frame.pack(pady=(0, 12))
        self.flood_level_label = tk.Label(self.flood_panel.body, text="CHECKING...", font=("Consolas", 18, "bold"),
                                          fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="w")
        self.flood_level_label.pack(fill="x")
        self.flood_detail_label = tk.Label(self.flood_panel.body, text="", font=("Consolas", 9), fg=COLOR_TEXT_DIM,
                                           bg=COLOR_PANEL, anchor="w", justify="left", wraplength=270)
        self.flood_detail_label.pack(fill="x", pady=(2, 0))
        self._flood_next_refresh = 0
        self._flood_last_shown = None
        self._flood_area = None

        # ---- Self-Healing panel: live status of the background immune system ----
        self.heal_panel = Panel(right_col, "SYSTEM //", "SELF-HEALING", 300, 168)
        self.heal_panel.frame.pack(pady=(0, 12))
        heal_top = tk.Frame(self.heal_panel.body, bg=COLOR_PANEL)
        heal_top.pack(fill="x")
        self.heal_status_label = tk.Label(heal_top, text="HEALTHY", font=("Consolas", 18, "bold"),
                                          fg=COLOR_GOOD, bg=COLOR_PANEL, anchor="w")
        self.heal_status_label.pack(side="left")
        tk.Button(heal_top, text="Scan Now", font=("Segoe UI", 9, "bold"), fg=COLOR_BG, bg=COLOR_CYAN,
                  relief="flat", padx=8, pady=2, command=self._heal_scan_click).pack(side="right")
        self.heal_stats_label = tk.Label(self.heal_panel.body, text="", font=("Consolas", 9),
                                         fg=COLOR_TEXT, bg=COLOR_PANEL, anchor="w")
        self.heal_stats_label.pack(fill="x", pady=(2, 2))
        self.heal_event_label = tk.Label(self.heal_panel.body, text="Watching CPU, memory, disk and drivers.",
                                         font=("Consolas", 9), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL,
                                         anchor="nw", justify="left", wraplength=270)
        self.heal_event_label.pack(fill="both", expand=True)
        self._heal_last_shown = None
        start_self_heal()

        # ---- Kochi Intelligence Map panel: one-click launch ----
        self.kochi_panel = Panel(right_col, "SYSTEM //", "KOCHI MAP", 300, 130)
        self.kochi_panel.frame.pack(pady=(0, 12))
        tk.Label(self.kochi_panel.body, text="Weather \u00b7 seismic \u00b7 air quality \u00b7 volcanic risk",
                  font=("Consolas", 9), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, justify="left",
                  anchor="w", wraplength=260).pack(fill="x", pady=(0, 8))
        tk.Button(self.kochi_panel.body, text="Open Map", font=("Segoe UI", 9, "bold"), fg=COLOR_BG,
                  bg=COLOR_CYAN, relief="flat", padx=10, pady=3, command=self._open_kochi_map).pack(anchor="w")

        # ---- AGENT SWARM: named sub-agents you can address directly (or let
        # Swarm Mode auto-route to), each answering in its own voice ----
        self.swarm_panel = Panel(right_col, "SYSTEM //", "AGENT SWARM", 300, 300)
        self.swarm_panel.frame.pack(pady=(0, 12))
        sw = self.swarm_panel.body
        swarm_top = tk.Frame(sw, bg=COLOR_PANEL)
        swarm_top.pack(fill="x")
        self.swarm_mode_label = tk.Label(swarm_top, text="SWARM MODE: OFF", font=("Consolas", 9, "bold"),
                                         fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="w")
        self.swarm_mode_label.pack(side="left")
        tk.Button(swarm_top, text="Toggle", font=("Consolas", 8), fg=COLOR_TEXT, bg="#141c28",
                  relief="flat", padx=8, command=self._toggle_swarm_mode).pack(side="right")
        tk.Label(sw, text="Address one directly - type or say \"<name>, ...\"", font=("Consolas", 8),
                  fg=COLOR_TEXT_DIM, bg=COLOR_PANEL, anchor="w", wraplength=270).pack(fill="x", pady=(4, 6))
        for key, agent in AGENTS.items():
            row = tk.Frame(sw, bg=COLOR_PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="\u25CF", font=("Consolas", 10), fg=agent["color"], bg=COLOR_PANEL).pack(side="left")
            tk.Label(row, text=f"{agent['name']} \u2014 {agent['role']}", font=("Consolas", 8), fg=COLOR_TEXT,
                      bg=COLOR_PANEL, anchor="w", justify="left", wraplength=205).pack(
                      side="left", fill="x", expand=True, padx=(4, 0))
            tk.Button(row, text="Talk", font=("Consolas", 7), fg=COLOR_TEXT, bg="#141c28", relief="flat",
                      padx=5, command=lambda k=key: self._talk_to_agent(k)).pack(side="right")
        self._swarm_last_shown = None

        self.actions_panel = Panel(right_col, "SYSTEM //", "QUICK ACTIONS", 300, 305)
        self.actions_panel.frame.pack(pady=(0, 12))
        actions_grid = tk.Frame(self.actions_panel.body, bg=COLOR_PANEL)
        actions_grid.pack(fill="both", expand=True)

        self.mute_button = self._make_action_button(actions_grid, "\U0001F507", "Mute", self._toggle_mute, 0, 0)
        self.vision_button = self._make_action_button(actions_grid, "\U0001F441", "Vision", self._toggle_vision, 0, 1)
        self.adskip_button = self._make_action_button(actions_grid, "\u23ED", "Ad-Skip", self._toggle_adskip, 0, 2)
        self.productive_button = self._make_action_button(
            actions_grid, "\U0001F3AF", "Productive", self._toggle_productive, 1, 0)
        self.screen_button = self._make_action_button(
            actions_grid, "\U0001F5A5", "Screen Help", self._screen_help_click, 1, 1)
        self.briefing_button = self._make_action_button(
            actions_grid, "\U0001F4CB", "Briefing", self._briefing_click, 2, 0)
        self.pick_button = self._make_action_button(
            actions_grid, "\U0001F50D", "Pick Area", self._pick_area_click, 2, 1)
        self.stop_talking_button = self._make_action_button(
            actions_grid, "\U0001F910", "Stop Talking", self._stop_talking_click, 2, 2)
        self.quit_button = self._make_action_button(actions_grid, "\u23FB", "Quit", self.on_close, 1, 2)
        for i in range(3):
            actions_grid.grid_columnconfigure(i, weight=1)

        self.log_panel = Panel(right_col, "SYSTEM //", "ACTIVITY LOG", 300, 400)
        self.log_panel.frame.pack()
        self.log_text = tk.Text(self.log_panel.body, bg=COLOR_PANEL, fg=COLOR_CYAN,
                                 font=("Consolas", 9), relief="flat", wrap="word",
                                 state="disabled", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)

    def _build_bottom_bar(self):
        """A thin strip of real system vitals - battery, network throughput,
        per-drive disk usage, process uptime - the same kind of dense HUD
        readouts as the reference dashboard, all backed by live psutil
        readings rather than placeholder numbers."""
        bar = tk.Frame(self.root, bg=COLOR_PANEL, highlightbackground=COLOR_PANEL_BORDER,
                        highlightthickness=1, height=40)
        bar.pack(fill="x", side="bottom", padx=20, pady=(0, 15))
        bar.pack_propagate(False)
        inner = tk.Frame(bar, bg=COLOR_PANEL)
        inner.pack(fill="both", expand=True, padx=16)

        def _stat(text_var_name):
            lbl = tk.Label(inner, text="--", font=("Consolas", 9), fg=COLOR_TEXT_DIM, bg=COLOR_PANEL)
            lbl.pack(side="left", padx=(0, 26))
            setattr(self, text_var_name, lbl)

        _stat("batt_bottom_label")
        _stat("net_bottom_label")
        _stat("disk_bottom_label")
        _stat("uptime_bottom_label")

    def _make_stat_row(self, parent, label_text):
        row = tk.Frame(parent, bg=COLOR_PANEL)
        row.pack(fill="x", pady=6)
        tk.Label(row, text=label_text, font=("Consolas", 9), fg=COLOR_TEXT_DIM,
                 bg=COLOR_PANEL).pack(side="left")
        value_label = tk.Label(row, text="0%", font=("Consolas", 9, "bold"), fg=COLOR_CYAN,
                                bg=COLOR_PANEL)
        value_label.pack(side="right")
        bar_bg = tk.Frame(parent, bg="#1a2230", height=6)
        bar_bg.pack(fill="x", pady=(0, 4))
        bar_fill = tk.Frame(bar_bg, bg=COLOR_CYAN, height=6, width=0)
        bar_fill.place(x=0, y=0, relheight=1)
        return {"value_label": value_label, "bar_bg": bar_bg, "bar_fill": bar_fill}

    def _make_action_button(self, parent, icon, label, command, row, col):
        btn_frame = tk.Frame(parent, bg="#141c28", highlightbackground=COLOR_PANEL_BORDER,
                              highlightthickness=1, cursor="hand2")
        btn_frame.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
        icon_label = tk.Label(btn_frame, text=icon, font=("Segoe UI Emoji", 16),
                               fg=COLOR_CYAN, bg="#141c28")
        icon_label.pack(pady=(8, 0))
        text_label = tk.Label(btn_frame, text=label, font=("Consolas", 7),
                               fg=COLOR_TEXT_DIM, bg="#141c28")
        text_label.pack(pady=(0, 6))
        for widget in (btn_frame, icon_label, text_label):
            widget.bind("<Button-1>", lambda e: self._beeped_click(command))
        return {"frame": btn_frame, "icon": icon_label, "text": text_label}

    def _beeped_click(self, fn):
        """Every quick-action button routes its click through here first -
        a short HUD-style confirmation click, same spirit as the reference
        UI's playSoundEffect() on every button press."""
        ui_beep(850, 45)
        fn()

    def _set_action_button_active(self, btn, active):
        """Recolors a quick-action button to show its real on/off state -
        without this, toggling one (Mute/Vision/Ad-Skip/Productive) gave no
        visible feedback at all, so a click looked like it did nothing."""
        bg = COLOR_CYAN if active else "#141c28"
        fg = COLOR_BG if active else COLOR_CYAN
        btn["frame"].config(bg=bg)
        btn["icon"].config(bg=bg, fg=fg)
        btn["text"].config(bg=bg, fg=(COLOR_BG if active else COLOR_TEXT_DIM))

    def _update_action_buttons(self):
        self._set_action_button_active(self.mute_button, getattr(self, "_muted", False))
        self._set_action_button_active(self.vision_button, vision_active)
        self._set_action_button_active(self.adskip_button, _ad_skipper_active)
        self._set_action_button_active(self.productive_button, PRODUCTIVE_MODE_ACTIVE)

    # -----------------------------------------------------------------
    # Real actions - every button here calls an actual Nova function
    # -----------------------------------------------------------------
    def _lang_set_listen(self, code):
        set_languages(listen=code)
        log_activity(f"Listen language set to {LANGUAGES[code]['name'] if code in LANGUAGES else 'Auto'} via dashboard")

    def _lang_set_reply(self, code):
        set_languages(reply=code)
        label = "Match speaker" if code == "match" else LANGUAGES[code]["name"]
        log_activity(f"Reply language set to {label} via dashboard")

    def _lang_listen_more_chosen(self, _event=None):
        choice = self.lang_listen_more.get()
        if choice in self._lang_more_map:
            self._lang_set_listen(self._lang_more_map[choice])

    def _lang_reply_more_chosen(self, _event=None):
        choice = self.lang_reply_more.get()
        if choice in self._lang_more_map:
            self._lang_set_reply(self._lang_more_map[choice])

    def _update_language_panel(self):
        for code, btn in self.lang_listen_buttons.items():
            active = (LANG_STATE["listen"] == code)
            btn.config(bg=COLOR_CYAN if active else "#141c28", fg=COLOR_BG if active else COLOR_TEXT)
        for code, btn in self.lang_reply_buttons.items():
            active = (LANG_STATE["reply"] == code)
            btn.config(bg=COLOR_CYAN if active else "#141c28", fg=COLOR_BG if active else COLOR_TEXT)
        # if the active language was picked by voice (e.g. "speak Arabic") and
        # isn't one of the four quick buttons, reflect it in the dropdown too
        listen_label = next((n for n, c in self._lang_more_map.items() if c == LANG_STATE["listen"]), "More...")
        if self.lang_listen_more.get() != listen_label:
            self.lang_listen_more.set(listen_label)
        reply_label = next((n for n, c in self._lang_more_map.items() if c == LANG_STATE["reply"]), "More...")
        if self.lang_reply_more.get() != reply_label:
            self.lang_reply_more.set(reply_label)

        heard_name = LANGUAGES.get(LANG_STATE["last_heard"], LANGUAGES["en"])["name"]
        reply_name = LANGUAGES[resolve_reply_lang()]["name"]
        snapshot = (LANG_STATE["listen"], LANG_STATE["reply"], LANG_STATE["heard_text"],
                    LANG_STATE["reply_text"], LANG_STATE["translator_ok"])
        if snapshot == self._lang_last_shown:
            return
        self._lang_last_shown = snapshot

        heard = LANG_STATE["heard_text"] or "\u2014"
        said = LANG_STATE["reply_text"] or "\u2014"
        text = f"Heard ({heard_name}): {heard}\nSaid ({reply_name}): {said}"
        if LANG_STATE["translator_ok"] is False:
            text += "\nTranslation unreachable - check your internet connection."
        self.lang_status_label.config(text=text)

    def _toggle_mute(self):
        self._muted = not getattr(self, "_muted", False)
        mute_volume(self._muted)
        log_activity(f"Muted via dashboard" if self._muted else "Unmuted via dashboard")

    def _toggle_vision(self):
        if vision_active:
            stop_vision()
            log_activity("Vision turned off via dashboard")
        else:
            start_vision(speak_async, camera_index=get_preferred_camera_index(), mode="interpretation")
            log_activity("Vision (interpretation) started via dashboard")

    def _toggle_adskip(self):
        if _ad_skipper_active:
            stop_ad_skipper()
            log_activity("Ad-skipper turned off via dashboard")
        else:
            start_ad_skipper()
            log_activity("Ad-skipper turned on via dashboard")

    def _screen_help_click(self):
        log_activity("Screen help requested via dashboard")
        reply_lang = resolve_reply_lang()
        ack, ack_lang = localize_for_speech("On it \u2014 let me take a look at your screen.", reply_lang)
        speak_async(ack, voice=LANGUAGES[ack_lang]["voice"])
        _start_screen_help_async(
            "Look at my screen. If there is an error, explain it and fix it. "
            "If there is a problem to solve, solve it step by step. "
            "Otherwise explain what I am looking at and what to do next.",
            with_screen=True, context_label="[screen help via dashboard]")

    def _pick_area_click(self):
        log_activity("Screen picker opened via dashboard")
        activate_screen_picker()

    def _update_flood_panel(self):
        area = WEATHER_STATE["area"]
        if area != self._flood_area or time.time() >= self._flood_next_refresh:  # area change or every 30 min
            self._flood_area = area
            self._flood_next_refresh = time.time() + 1800
            refresh_flood_async(area)
        d, err = FLOOD_STATE["data"], FLOOD_STATE["error"]
        snapshot = (id(d), err, area)
        if snapshot == self._flood_last_shown:
            return
        self._flood_last_shown = snapshot
        if d is None:
            self.flood_level_label.config(text=(err or "CHECKING..."), fg=COLOR_TEXT_DIM)
            return
        level = d["level"]
        self.flood_level_label.config(text=level, fg=FLOOD_COLORS[level])
        days = "  ".join(f"{mm:.0f}mm" for _date, mm, _p in d["days"])
        self.flood_detail_label.config(text=f"{d['area']} \u00b7 3-day rain: {days}\n{FLOOD_MEANING[level]}")
        # Proactive alert: speak once when the level rises to ORANGE or RED
        if level in ("ORANGE", "RED") and level != FLOOD_STATE["alerted_level"]:
            speak_async(f"Flood watch alert for {d['area']}. Level {level}. {FLOOD_MEANING[level]} "
                        "Say flood checklist to hear what to prepare.")
            log_activity(f"FLOOD WATCH {level} for {d['area']}")
        FLOOD_STATE["alerted_level"] = level

    def _briefing_click(self):
        log_activity("Daily briefing requested via dashboard")
        threading.Thread(target=lambda: speak_async(daily_briefing()), daemon=True).start()

    def _update_heal_panel(self):
        ev = HEAL_STATE["events"][-1] if HEAL_STATE["events"] else None
        snapshot = (HEAL_STATE["status"], round(HEAL_STATE["cpu"]), round(HEAL_STATE["ram"]),
                    HEAL_STATE["fixes"], ev, SELF_HEAL_ENABLED)
        if snapshot == self._heal_last_shown:
            return
        self._heal_last_shown = snapshot
        status = HEAL_STATE["status"] if SELF_HEAL_ENABLED else "PAUSED"
        color = {"HEALTHY": COLOR_GOOD, "WATCHING": COLOR_SPEAKING, "HEALING": "#ff9f43"}.get(status, COLOR_TEXT_DIM)
        self.heal_status_label.config(text=status, fg=color)
        self.heal_stats_label.config(
            text=f"CPU {HEAL_STATE['cpu']:.0f}%  RAM {HEAL_STATE['ram']:.0f}%  Fixes {HEAL_STATE['fixes']}")
        if ev:
            self.heal_event_label.config(text=f"[{ev[0]}] {ev[2]}"[:190])

    def _heal_scan_click(self):
        log_activity("Self-heal scan requested via dashboard")
        threading.Thread(target=lambda: speak_async(health_report(scan=True)), daemon=True).start()

    # ---- Time tools / schedule panel actions ----
    def _sw_toggle(self):
        if STOPWATCH["running"]:
            sw_stop()
        else:
            sw_start()

    def _sw_lap(self):
        sw_lap()

    def _cd_start(self):
        secs = parse_countdown_input(self.cd_entry.get())
        if secs:
            cd_add(secs)
        else:
            self._sched_msg, self._sched_msg_color = "Countdown: try 5m, 90s, 1h30m or 1:30", COLOR_WARN

    def _sched_entry_focus_in(self, _event=None):
        if self.sched_entry.get() == self._sched_placeholder:
            self.sched_entry.delete(0, "end")
            self.sched_entry.config(fg=COLOR_TEXT)

    def _sched_add_click(self):
        text = self.sched_entry.get().strip()
        if not text or text == self._sched_placeholder:
            return
        ok, msg = sched_add_from_text(text)
        self._sched_msg, self._sched_msg_color = (("Added: " if ok else "") + msg), (COLOR_GOOD if ok else COLOR_WARN)
        if ok:
            self.sched_entry.delete(0, "end")
        self._sched_shown_key = None

    def _sched_delete_click(self):
        sel = self.sched_list.curselection()
        if sel and sel[0] < len(self._sched_list_ids):
            sched_delete(self._sched_list_ids[sel[0]])

    def _sched_snooze_click(self):
        ok, msg = snooze_last(10)
        self._sched_msg, self._sched_msg_color = msg, (COLOR_GOOD if ok else COLOR_TEXT_DIM)

    def _sched_clear_click(self):
        n = sched_clear_all()
        self._sched_msg, self._sched_msg_color = f"Cleared {n} item(s).", COLOR_TEXT_DIM

    def _refresh_sched_list(self):
        items = sched_upcoming(limit=30)
        self._sched_list_ids = [i["id"] for i in items]
        self.sched_list.delete(0, "end")
        for i in items:
            mark = "\u21BB " if i["repeat"] != "none" else ""
            self.sched_list.insert("end", f"{_short_when(i['when']):<13} {mark}{i['title']}")
        if not items:
            self.sched_list.insert("end", "  Nothing scheduled")

    def _timer_render_loop(self):
        """10 fps - the stopwatch shows tenths of a second, so the slower dashboard
        loop (5 fps) isn't smooth enough for it."""
        try:
            now = time.time()
            self.sw_label.config(text=fmt_clock(sw_elapsed()))
            want = "Pause" if STOPWATCH["running"] else ("Resume" if STOPWATCH["elapsed"] > 0 else "Start")
            if want != self._sw_btn_text:
                self._sw_btn_text = want
                self.sw_start_btn.config(text=want)
            laps = STOPWATCH["laps"]
            lap_text = "\n".join(
                f"Lap {n}: {fmt_clock(t)}" + (f"  (+{fmt_clock(t - laps[n - 2])})" if n > 1 else "")
                for n, t in list(enumerate(laps, 1))[-2:])
            self.sw_laps_label.config(text=lap_text)

            with SCHED_LOCK:
                cds = list(COUNTDOWNS)
            lines = [f"{c['label'] or 'Timer'}  " + ("DONE" if c["done"] else fmt_clock(c["end"] - now, tenths=False))
                     for c in cds[:4]]
            self.cd_label.config(text="\n".join(lines) or "No timers running",
                                 fg=(COLOR_SPEAKING if any(c["done"] for c in cds)
                                     else COLOR_CYAN if lines else COLOR_TEXT_DIM))

            key = (SCHED_STATE["version"], int(now // 30))
            if key != self._sched_shown_key:
                self._sched_shown_key = key
                self._refresh_sched_list()
            if now - SCHED_STATE["last_alert_time"] < 20:
                self.sched_status_label.config(text="\u23F0 " + SCHED_STATE["last_alert"], fg=COLOR_SPEAKING)
            else:
                self.sched_status_label.config(text=self._sched_msg, fg=self._sched_msg_color)
        except Exception as e:
            print("timer render error:", e)
        self.root.after(100, self._timer_render_loop)

    def _stop_talking_click(self):
        if is_speaking():
            stop_speaking()
            log_activity("Interrupted via dashboard")

    # ---- News panel ----
    def _news_set_tab(self, tab):
        self.news_tab_var.set(tab)
        is_india = (tab == "india")
        self.news_india_btn.config(bg=COLOR_CYAN if is_india else "#141c28",
                                   fg=COLOR_BG if is_india else COLOR_TEXT)
        self.news_world_btn.config(bg=COLOR_CYAN if not is_india else "#141c28",
                                   fg=COLOR_BG if not is_india else COLOR_TEXT)
        self._news_last_shown = None  # force the list to redraw for the new tab

    def _news_open_link(self, index):
        if index < len(self._news_item_links) and self._news_item_links[index]:
            webbrowser.open(self._news_item_links[index])

    def _news_read_digest(self):
        text = NEWS_STATE["digest"]
        if not text:
            items = NEWS_STATE[self.news_tab_var.get()]["items"][:5]
            text = ("Here's what's making headlines: " + " ".join(f"{i['title']}." for i in items)
                   if items else "I don't have any news fetched yet.")
        threading.Thread(target=lambda: speak_async(text), daemon=True).start()

    def _update_news_panel(self):
        tab = self.news_tab_var.get()
        state = NEWS_STATE[tab]
        snapshot = (tab, id(state["items"]), state["updated"], state["error"], NEWS_STATE["loading"])
        if snapshot == self._news_last_shown:
            return
        self._news_last_shown = snapshot
        self.news_list.config(state="normal")
        self.news_list.delete("1.0", "end")
        for tag in self.news_list.tag_names():
            if tag.startswith("link"):
                self.news_list.tag_delete(tag)
        self._news_item_links = []
        items = state["items"]
        if not items:
            self.news_list.insert("end", "  " + (state["error"] or "Fetching the latest..."))
            self._news_item_links.append(None)
        else:
            for idx, i in enumerate(items):
                tag = f"link{idx}"
                self.news_list.insert("end", i["title"], ("headline", tag))
                self.news_list.insert("end", f"  \u2014 {i['source']}\n\n", "source")
                self.news_list.tag_configure(tag, underline=False)
                self.news_list.tag_bind(tag, "<Button-1>", lambda e, i=idx: self._news_open_link(i))
                self.news_list.tag_bind(tag, "<Enter>", lambda e: self.news_list.config(cursor="hand2"))
                self._news_item_links.append(i["link"])
        self.news_list.config(state="disabled")
        if state["updated"]:
            self.news_updated_label.config(
                text=f"Updated {state['updated']} \u00b7 refreshes hourly \u00b7 double-click to open")
        elif state["error"]:
            self.news_updated_label.config(text=state["error"])
        else:
            self.news_updated_label.config(text="Fetching the latest...")

    def _toggle_swarm_mode(self):
        set_swarm_mode(not SWARM_MODE_ACTIVE)
        log_activity("Swarm mode turned on via dashboard" if SWARM_MODE_ACTIVE
                      else "Swarm mode turned off via dashboard")

    def _talk_to_agent(self, key):
        self.command_entry.delete(0, tk.END)
        self.command_entry.insert(0, f"{AGENTS[key]['name']}, ")
        self.command_entry.focus_set()
        self.command_entry.icursor(tk.END)

    def _update_swarm_panel(self):
        text = f"SWARM MODE: {'ON' if SWARM_MODE_ACTIVE else 'OFF'}"
        if text == self._swarm_last_shown:
            return
        self._swarm_last_shown = text
        self.swarm_mode_label.config(text=text, fg=COLOR_GOOD if SWARM_MODE_ACTIVE else COLOR_TEXT_DIM)

    def _toggle_productive(self):
        set_productive_mode(not PRODUCTIVE_MODE_ACTIVE)
        log_activity("Productive mode turned on via dashboard" if PRODUCTIVE_MODE_ACTIVE
                      else "Productive mode turned off via dashboard")

    # ---- Phone panel actions ----
    def _phone_entry_focus_in(self, event):
        if self.phone_entry.get() == "Name or number":
            self.phone_entry.delete(0, tk.END)

    def _entry_clear_placeholder(self, entry, placeholder):
        if entry.get() == placeholder:
            entry.delete(0, tk.END)

    def _whatsapp_send(self):
        target = self.whatsapp_to_entry.get().strip()
        message = self.whatsapp_msg_entry.get().strip()
        if not target or target == "Name or number" or not message or message == "Message":
            return
        self.whatsapp_msg_entry.delete(0, tk.END)
        log_activity(f"You (dashboard): whatsapp {target}: {message}")
        self._phone_run_async(whatsapp_reply, target, message)

    def _whatsapp_voice_note(self):
        global pending_whatsapp_voice
        target = self.whatsapp_to_entry.get().strip()
        if not target or target == "Name or number":
            return
        pending_whatsapp_voice = {"target": target, "time": time.time()}
        log_activity(f"You (dashboard): whatsapp voice note for {target}")
        speak_async(f"Sure - what should I tell {target}?")

    def _phone_run_async(self, fn, *args):
        def _process():
            try:
                reply = fn(*args)
                log_activity(f"Nova: {reply}")
                speak_async(reply)
            except Exception as e:
                log_activity(f"Phone error: {e}")
        threading.Thread(target=_process, daemon=True).start()

    def _phone_dial(self):
        text = self.phone_entry.get().strip()
        if not text or text == "Name or number":
            return
        self.phone_entry.delete(0, tk.END)
        log_activity(f"You (dashboard): call {text}")
        self._phone_run_async(call_target, text)

    def _phone_hangup(self):
        log_activity("You (dashboard): hang up")
        self._phone_run_async(hang_up_call)

    def _phone_sync(self):
        log_activity("You (dashboard): sync contacts")
        self._phone_run_async(sync_contacts_reply)

    def _phone_setup_popup(self):
        import tkinter.messagebox as messagebox
        messagebox.showinfo("Phone setup", PHONE_HELP_TEXT)

    def _phone_accept_incoming(self):
        global pending_incoming_call
        label = pending_incoming_call["name"] if pending_incoming_call else "the call"
        pending_incoming_call = None
        log_activity(f"You (dashboard): accept call from {label}")
        self._phone_run_async(answer_incoming_call)

    def _phone_decline_incoming(self):
        global pending_incoming_call
        label = pending_incoming_call["name"] if pending_incoming_call else "the call"
        pending_incoming_call = None
        log_activity(f"You (dashboard): decline call from {label}")
        self._phone_run_async(hang_up_call)

    # ---- DJ Mode panel actions ----
    def _dj_entry_focus_in(self, event):
        if self.dj_activity_entry.get() == "studying/gaming/chilling":
            self.dj_activity_entry.delete(0, tk.END)

    def _dj_run_async(self, fn, *args):
        def _process():
            try:
                reply = fn(*args)
                log_activity(f"Nova: {reply}")
                speak_async(reply)
            except Exception as e:
                log_activity(f"DJ Mode error: {e}")
        threading.Thread(target=_process, daemon=True).start()

    def _dj_start(self):
        activity = self.dj_activity_entry.get().strip()
        if not activity or activity == "studying/gaming/chilling":
            return
        if DJ_ACTIVITY_ALIASES.get(activity.lower()) is None:
            log_activity("Nova: I don't recognize that activity - try studying, gaming, chilling, workout, or party.")
            return
        log_activity(f"You (dashboard): dj mode for {activity}")
        self._dj_run_async(start_dj_mode, activity)

    def _dj_skip(self):
        log_activity("You (dashboard): skip track")
        self._dj_run_async(dj_next_track)

    def _dj_stop(self):
        log_activity("You (dashboard): stop dj mode")
        self._dj_run_async(stop_dj_mode)

    # ---- Orbital Sim panel actions ----
    def _orbital_entry_focus_in(self, event):
        if self.orbital_mode_entry.get() == "iss/rocket/asteroid":
            self.orbital_mode_entry.delete(0, tk.END)

    def _orbital_run_async(self, fn, *args):
        def _process():
            try:
                reply = fn(*args)
                log_activity(f"Nova: {reply}")
                speak_async(reply)
            except Exception as e:
                log_activity(f"Orbital Sim error: {e}")
        threading.Thread(target=_process, daemon=True).start()

    def _orbital_start(self):
        mode = self.orbital_mode_entry.get().strip().lower()
        if not mode or mode == "iss/rocket/asteroid":
            return
        fn = {"iss": iss_reply, "rocket": rocket_reply, "asteroid": asteroid_reply}.get(mode)
        if fn is None:
            log_activity("Nova: Say 'iss', 'rocket', or 'asteroid'.")
            return
        log_activity(f"You (dashboard): track {mode}")
        self._orbital_run_async(fn)

    def _orbital_stop(self):
        log_activity("You (dashboard): stop orbital sim")
        self._orbital_run_async(stop_orbital_sim)

    # ---- Kochi Weather panel ----
    def _weather_area_chosen(self, _event=None):
        log_activity(f"Weather area: {self.weather_area_var.get()}")
        refresh_weather_async(self.weather_area_var.get())

    def _update_weather_panel(self):
        # A voice command may have changed the area - keep the dropdown in sync.
        if WEATHER_STATE["area"] != self._weather_synced_area:
            self._weather_synced_area = WEATHER_STATE["area"]
            self.weather_area_var.set(WEATHER_STATE["area"])
        if time.time() >= self._weather_next_refresh:  # first run + every 10 minutes
            self._weather_next_refresh = time.time() + 600
            refresh_weather_async()
        d, err = WEATHER_STATE["data"], WEATHER_STATE["error"]
        snapshot = (id(d), err, WEATHER_STATE["loading"], WEATHER_STATE["area"])
        if snapshot == self._weather_last_shown:
            return
        self._weather_last_shown = snapshot
        if WEATHER_STATE["loading"] and d is None:
            self.weather_cond_label.config(text="Loading...")
        elif err and d is None:
            self.weather_cond_label.config(text=err)
        elif d:
            self.weather_temp_label.config(text=f"{d['temp']:.0f}\u00b0C")
            self.weather_cond_label.config(text=f"{d['condition']} \u00b7 feels {d['feels']:.0f}\u00b0")
            self.weather_detail_label.config(
                text=(f"Humidity {d['humidity']:.0f}%   Wind {d['wind']:.0f} km/h\n"
                      f"Rain chance {d['rain_chance']:.0f}%   H {d['high']:.0f}\u00b0 L {d['low']:.0f}\u00b0\n"
                      f"Updated {WEATHER_STATE['updated']}"))

    # ---- Kochi Intelligence Map panel action ----
    def _open_kochi_map(self):
        log_activity("You (dashboard): open kochi map")

        def _process():
            ok, message = open_kochi_map()
            log_activity(f"Nova: {message}")

        threading.Thread(target=_process, daemon=True).start()

    def _submit_typed_command(self):
        heard_text = self.command_entry.get().strip()
        if not heard_text:
            return
        self.command_entry.delete(0, tk.END)
        if is_speaking():  # typing a new command while Nova's talking should cut it off, same as voice barge-in
            stop_speaking()
        heard_lang = detect_script_language(heard_text)  # typed input isn't tied to LISTEN mode -
                                                           # you can type in any of the three scripts anytime
        log_activity(f"You (typed, {LANGUAGES[heard_lang]['name']}): {heard_text}")

        def _process():
            try:
                text, translated_ok = translate_command_to_english(heard_text, heard_lang)
                if not translated_ok:
                    log_activity(f"Translation unavailable ({LANGUAGES[heard_lang]['name']}): {heard_text}")
                    speak_async("Sorry, translation isn't reachable right now. Please try again in a moment.")
                    return
                set_last_heard(heard_lang, heard_text, text)

                # Multi-Agent Swarm Mode - see the matching block in callback() above
                if _awaiting_followup():
                    agent_key = None
                else:
                    agent_key, text = detect_addressed_agent(text)
                    if agent_key is None and SWARM_MODE_ACTIVE:
                        agent_key = guess_agent_for(text)
                bare_agent_wake = bool(agent_key) and not text.strip()

                register = detect_register(text)
                user_emotion = detect_emotion(text)  # text-only signal (no mic audio for typed input)
                if bare_agent_wake:
                    reply, action = f"{AGENTS[agent_key]['name']} here. Go ahead.", None
                else:
                    lang_reply = handle_language_command(text)
                    if lang_reply is not None:
                        reply, action = lang_reply, None
                    else:
                        reply, action = choose_action_and_reply(text.lower())
                    if agent_key and reply:
                        reply = agent_intro_prefix(agent_key) + reply
                reply = adapt_reply_tone(reply, register) if reply else reply
                add_exchange(text, strip_pause_markers(reply))

                reply_lang = resolve_reply_lang()
                spoken, spoken_lang = localize_for_speech(reply, reply_lang)
                voice = agent_voice_for(agent_key, spoken_lang)
                tag = f" [{AGENTS[agent_key]['name']}]" if agent_key and spoken_lang == "en" else ""
                log_activity(f"Nova ({LANGUAGES[spoken_lang]['name']}){tag}: {strip_pause_markers(spoken)}")
                speak_async(spoken, emotion=response_emotion_for(user_emotion), voice=voice)
                if action == "exit":
                    stop_nova_engine()
            except Exception as e:
                log_activity(f"Error handling typed command: {e}")

        threading.Thread(target=_process, daemon=True).start()

    def _tray_open_log_from_gui(self):
        _tray_open_log(None, None)

    def _show_memory_popup(self):
        import tkinter.messagebox as messagebox
        facts = get_all_facts()
        if not facts:
            messagebox.showinfo("Nova remembers", "Nothing saved yet - tell Nova your name or what you like.")
            return
        lines = [f"{k}: {v}" for k, v in facts.items()]
        messagebox.showinfo("Nova remembers", "\n".join(lines))

    def on_start_stop(self):
        if stop_listening is None:
            ui_beep_sequence((440, 660, 880), 90)  # rising tone - powering up
            threading.Thread(target=start_nova_engine, daemon=True).start()
        else:
            ui_beep_sequence((880, 660, 440), 90)  # falling tone - powering down
            stop_nova_engine()

    def on_close(self):
        stop_nova_engine(enter_sleep_mode=False)
        stop_sleep_listener()
        stop_system_monitor()
        hide_screen_picker()
        close_browser()
        stop_vision()
        self.root.destroy()

    def _update_start_button(self):
        """Reflects the TRUE engine state, not a separately-tracked flag -
        so the button stays correct no matter whether 'exit'/'enter' by
        voice, a click, or the sleep/wake cycle changed things."""
        self.start_button.config(text="Stop" if stop_listening is not None else "Start")

    # -----------------------------------------------------------------
    # Render loop - every panel refreshed from real, live data
    # -----------------------------------------------------------------
    def _render_loop(self):
        try:
            self._draw_visualizer()
            self._update_status_text()
            self._update_top_bar()
            self._update_status_badges()
            self._update_language_panel()
            self._update_resource_panel()
            self._update_bottom_bar()
            self._update_cpu_history_panel()
            self._update_subsystem_panel()
            self._update_action_buttons()
            self._update_dj_panel()
            self._update_orbital_panel()
            self._update_browser_panel()
            self._update_mic_panel()
            self._update_phone_panel()
            self._update_whatsapp_panel()
            self._update_notif_panel()
            self._update_swarm_panel()
            self._update_news_panel()
            self._update_log_panel()
            self._update_start_button()
            self._update_cube()
            self._update_weather_panel()
            self._update_flood_panel()
            self._update_heal_panel()
        except Exception as e:
            print("Render loop error (skipping this frame):", e)
        self.root.after(200, self._render_loop)  # dashboard panels don't need 30fps; the core canvas below does

    def _video_render_loop(self):
        try:
            self._update_video_panel()
        except Exception as e:
            print("Video render loop error (skipping this frame):", e)
        self.root.after(50, self._video_render_loop)  # ~20fps - smoother than the dashboard's stats refresh

    def _hologram_render_loop(self):
        try:
            self._draw_hologram()
        except Exception as e:
            print("Hologram render loop error (skipping this frame):", e)
        self.root.after(50, self._hologram_render_loop)  # ~20fps - smooth drag-rotation needs this, not 5fps

    def _radar_render_loop(self):
        try:
            self._draw_radar()
        except Exception as e:
            print("Radar render loop error (skipping this frame):", e)
        self.root.after(50, self._radar_render_loop)  # ~20fps - smooth sweep rotation needs this, not 5fps

    def _draw_radar(self):
        canvas = self.radar_canvas
        canvas.delete("all")
        size = RADAR_SIZE
        cx, cy = size / 2, size / 2
        r_outer = size / 2 - 12

        canvas.create_oval(cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
                            outline=COLOR_CYAN_DIM, width=1)
        for frac in (0.66, 0.33):
            r = r_outer * frac
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=COLOR_CYAN_DIM, width=1)
        canvas.create_line(cx - r_outer, cy, cx + r_outer, cy, fill=COLOR_CYAN_DIM)
        canvas.create_line(cx, cy - r_outer, cx, cy + r_outer, fill=COLOR_CYAN_DIM)

        # Sweeping line with a short fading tail behind it, same idea as a
        # real radar scope - drawn as several short segments blended from
        # COLOR_CYAN down to COLOR_CYAN_DIM going back from the live edge.
        angle_deg = (time.time() % RADAR_SWEEP_PERIOD_SEC) / RADAR_SWEEP_PERIOD_SEC * 360.0
        tail_steps = 14
        for i in range(tail_steps, 0, -1):
            a = math.radians(angle_deg - i * 2.2)
            t = 1.0 - i / tail_steps
            color = _hologram_depth_color(1.0 - t, 0.0, 1.0)  # reuse the same dim->bright blend helper
            x2, y2 = cx + r_outer * math.sin(a), cy - r_outer * math.cos(a)
            canvas.create_line(cx, cy, x2, y2, fill=color)
        a0 = math.radians(angle_deg)
        canvas.create_line(cx, cy, cx + r_outer * math.sin(a0), cy - r_outer * math.cos(a0),
                            fill=COLOR_CYAN, width=2)

        # Friendly contacts: the same five real subsystem states the
        # SUBSYSTEMS panel shows, plotted as green blips when active.
        states = {
            "MIC": stop_listening is not None, "VISION": vision_active,
            "AD-SKIP": _ad_skipper_active, "PHONE": phone_state["backend"] != "none",
            "PRODUCTIVE": PRODUCTIVE_MODE_ACTIVE,
        }
        friendly_count = 0
        for name, active in states.items():
            if not active:
                continue
            friendly_count += 1
            ang = math.radians(_radar_stable_angle(name))
            rad = r_outer * (0.4 + 0.1 * (_radar_stable_angle(name, salt=13) % 3))
            bx, by = cx + rad * math.sin(ang), cy - rad * math.cos(ang)
            canvas.create_oval(bx - 4, by - 4, bx + 4, by + 4, fill=COLOR_GOOD, outline="")
            canvas.create_text(bx, by - 11, text=name, fill=COLOR_TEXT_DIM, font=("Consolas", 6))

        # A real alert contact: only shown when last_error_time says
        # something actually went wrong recently - fades off after
        # RADAR_ALERT_FADE_SEC rather than lingering forever.
        age = time.time() - last_error_time
        alert_count = 0
        if age < RADAR_ALERT_FADE_SEC:
            alert_count = 1
            if self._radar_pinged_error_time != last_error_time:
                self._radar_pinged_error_time = last_error_time
                ui_beep(1100, 70)
            drift = (time.time() * 15) % 360  # a slow drift so it reads as an unknown, moving contact
            ang = math.radians(_radar_stable_angle("alert") + drift)
            rad = r_outer * 0.8
            fade = max(0.25, 1.0 - age / RADAR_ALERT_FADE_SEC)
            bx, by = cx + rad * math.sin(ang), cy - rad * math.cos(ang)
            rpx = 5 * fade + 2
            canvas.create_oval(bx - rpx, by - rpx, bx + rpx, by + rpx, fill=COLOR_WARN, outline="")
            canvas.create_text(bx, by - 11, text="UNKNOWN", fill=COLOR_WARN, font=("Consolas", 6, "bold"))

        self.radar_friendly_label.config(text=f"{friendly_count} ACTIVE")
        self.radar_contacts_label.config(text=f"{alert_count} ALERT{'S' if alert_count != 1 else ''}",
                                         fg=COLOR_WARN if alert_count else COLOR_TEXT_DIM)

    def _update_top_bar(self):
        self.time_label.config(text=time.strftime("%H:%M:%S"))
        self.date_label.config(text=time.strftime("%d %b, %A").upper())
        # Real health signal: red if an error happened in the last 10 seconds
        if time.time() - last_error_time < 10:
            self.status_dot_label.config(text="\u25CF ATTENTION", fg=COLOR_WARN)
        else:
            self.status_dot_label.config(text="\u25CF OPTIMAL", fg=COLOR_GOOD)

    def _update_resource_panel(self):
        # Read from the background-refreshed cache, not a live call - see
        # SYSTEM_STATS_STATE / start_system_monitor() for why.
        cpu = SYSTEM_STATS_STATE["cpu"]
        mem = SYSTEM_STATS_STATE["mem"]
        disk = SYSTEM_STATS_STATE["disk"]
        gpu = SYSTEM_STATS_STATE["gpu"]
        for stat, value in ((self.cpu_label, cpu), (self.mem_label, mem), (self.disk_label, disk)):
            stat["value_label"].config(text=f"{value:.0f}%")
            bar_width = stat["bar_bg"].winfo_width() or 1
            stat["bar_fill"].place(width=max(2, bar_width * value / 100))
        if gpu is None:
            self.gpu_label["value_label"].config(text="N/A")
            self.gpu_label["bar_fill"].place(width=0)
        else:
            self.gpu_label["value_label"].config(text=f"{gpu:.0f}%")
            bar_width = self.gpu_label["bar_bg"].winfo_width() or 1
            self.gpu_label["bar_fill"].place(width=max(2, bar_width * gpu / 100))

    def _update_cpu_history_panel(self):
        """Real sparkline of the last ~40 samples of actual CPU usage - not
        the mic waveform, an honest-to-goodness CPU load trend line."""
        c = self.cpu_history_canvas
        c.delete("all")
        w = c.winfo_width() or 280
        h = c.winfo_height() or 70
        history = list(CPU_HISTORY)
        if len(history) < 2:
            return
        step = w / (len(history) - 1)
        points = []
        for i, v in enumerate(history):
            x = i * step
            y = h - (v / 100) * (h - 8) - 4
            points.extend([x, y])
        c.create_line(*points, fill=COLOR_CYAN, width=2, smooth=True)
        c.create_text(w - 4, 4, anchor="ne", text=f"{history[-1]:.0f}%",
                       fill=COLOR_TEXT_DIM, font=("Consolas", 8))

    def _update_subsystem_panel(self):
        """Real on/off state for each subsystem, read straight from the
        actual global flags/objects that drive them - not cosmetic."""
        states = {
            "Mic": stop_listening is not None,
            "Vision": vision_active,
            "Ad-Skip": _ad_skipper_active,
            "Phone": phone_state["backend"] != "none",
            "Productive": PRODUCTIVE_MODE_ACTIVE,
        }
        for name, active in states.items():
            dot, state_lbl = self.subsystem_rows[name]
            color = COLOR_GOOD if active else COLOR_TEXT_DIM
            dot.config(fg=color)
            state_lbl.config(text="ON" if active else "OFF", fg=color)

    def _update_dj_panel(self):
        if dj_state["active"]:
            current = dj_state["queue"][dj_state["index"]] if dj_state["queue"] else "..."
            self.dj_status_label.config(
                text=f"\u25CF {dj_state['activity'].upper()}\nNow: {current}", fg=COLOR_GOOD)
        else:
            self.dj_status_label.config(text="Off - type an activity and hit Start", fg=COLOR_TEXT_DIM)

    def _update_orbital_panel(self):
        with _orbital_lock:
            mode = orbital_state["mode"]
            active = orbital_state["active"]
            label = orbital_state["label"]
            detail = orbital_state["detail"]
            lat, lon = orbital_state["lat"], orbital_state["lon"]

        if active:
            self.orbital_status_label.config(text=f"\u25CF {label}\n{detail}", fg=COLOR_GOOD)
        else:
            self.orbital_status_label.config(
                text="Inactive - try 'iss', 'rocket', or 'asteroid'", fg=COLOR_TEXT_DIM)

        self._draw_orbital_canvas(mode, active, lat, lon)

    def _draw_orbital_canvas(self, mode, active, lat, lon):
        """Real orthographic globe projection - the same spherical trig
        used for a cartographer's 'globe view' map, not a literal 3D
        render. Points on the far side of the globe are correctly hidden
        by the math (z < 0), which is what makes the spin actually read
        as three-dimensional rather than just a flat spinning circle."""
        c = self.orbital_canvas
        c.delete("all")
        w = c.winfo_width() or 264
        h = c.winfo_height() or 100
        cx, cy = w / 2, h / 2
        R = min(cx, cy) - 6

        with _orbital_lock:
            orbital_state["rotation"] = (orbital_state["rotation"] + 1.5) % 360
            rotation = orbital_state["rotation"]

        # Globe outline
        c.create_oval(cx - R, cy - R, cx + R, cy + R, outline=COLOR_CYAN_DIM, width=1)
        # Latitude reference rings (static horizontal ellipses - a common,
        # honest simplification: true meridians below DO rotate with spin,
        # these are just a fixed reference grid, not meant to imply motion)
        for lat_deg in (-60, -30, 0, 30, 60):
            ry = R * math.sin(math.radians(lat_deg))
            rx = R * math.cos(math.radians(lat_deg))
            c.create_oval(cx - rx, cy - ry - 2, cx + rx, cy - ry + 2, outline=COLOR_CYAN_DIM, width=1)
        # Rotating meridians - genuinely foreshorten with spin
        for lon_deg in (0, 60, 120, 180, 240, 300):
            eff = math.radians(lon_deg + rotation)
            half_w = abs(R * math.cos(eff))
            c.create_oval(cx - half_w, cy - R, cx + half_w, cy + R, outline=COLOR_CYAN_DIM, width=1)

        if not active or mode is None:
            c.create_text(cx, cy, text="STANDBY", font=("Consolas", 9), fill=COLOR_TEXT_DIM)
            return

        if mode in ("iss", "rocket"):
            lat_r = math.radians(lat)
            lon_r = math.radians(lon) + math.radians(rotation)
            x = R * math.cos(lat_r) * math.sin(lon_r)
            y = -R * math.sin(lat_r)
            z = math.cos(lat_r) * math.cos(lon_r)
            if z >= 0:  # front-facing hemisphere only - the back is genuinely hidden
                px, py = cx + x, cy + y
                color = COLOR_GOOD if mode == "iss" else COLOR_SPEAKING
                r = 4
                c.create_oval(px - r, py - r, px + r, py + r, fill=color, outline="")
                c.create_text(px, py - 10, text=mode.upper(), font=("Consolas", 7), fill=color)
        elif mode == "asteroid":
            with _orbital_lock:
                orbital_state["asteroid_phase"] = (orbital_state["asteroid_phase"] + 0.006) % 1.0
                phase = orbital_state["asteroid_phase"]
            angle = math.radians(-70 + phase * 140)
            arc_r = R * 1.7
            ax = cx + arc_r * math.cos(angle)
            ay = cy + arc_r * math.sin(angle) * 0.5
            c.create_oval(ax - 4, ay - 4, ax + 4, ay + 4, fill=COLOR_WARN, outline="")
            c.create_text(ax, ay - 10, text="AST", font=("Consolas", 7), fill=COLOR_WARN)

    def _update_bottom_bar(self):
        # Same cache as _update_resource_panel - battery/network are cheap,
        # but drives ride along here too since they share one refresh cycle.
        batt_pct, plugged = SYSTEM_STATS_STATE["batt_pct"], SYSTEM_STATS_STATE["batt_plugged"]
        if batt_pct is None:
            self.batt_bottom_label.config(text="BATTERY: N/A (desktop)")
        else:
            icon = "\u26A1" if plugged else ""
            self.batt_bottom_label.config(
                text=f"BATTERY: {batt_pct:.0f}% {icon}",
                fg=COLOR_WARN if (batt_pct < 20 and not plugged) else COLOR_TEXT_DIM)

        up, down = SYSTEM_STATS_STATE["net_up"], SYSTEM_STATS_STATE["net_down"]
        self.net_bottom_label.config(text=f"NET: \u2191 {up:.1f} KB/s   \u2193 {down:.1f} KB/s")

        drives = SYSTEM_STATS_STATE["drives"]
        if drives:
            self.disk_bottom_label.config(text="DISK: " + "  ".join(f"{label} {pct:.0f}%" for label, pct in drives))
        else:
            self.disk_bottom_label.config(text="DISK: N/A")

        self.uptime_bottom_label.config(text=f"UPTIME: {SYSTEM_STATS_STATE['uptime']}")

    def _update_browser_panel(self):
        lines = []
        lines.append(f"Session: {'OPEN' if _driver is not None else 'CLOSED'}")
        lines.append(f"Ad-skipper: {'ON' if _ad_skipper_active else 'OFF'}")
        lines.append(f"Last platform: {last_platform or '-'}")
        lines.append(f"Last results: {len(last_results)}")
        self.browser_status_label.config(text="\n".join(lines))

    def _update_mic_panel(self):
        self.mic_canvas.delete("all")
        w = self.mic_canvas.winfo_width() or 260
        h = self.mic_canvas.winfo_height() or 130
        history = list(VISUALIZER_HISTORY)[-20:]
        if not history:
            return
        bar_w = w / len(history)
        for i, level in enumerate(history):
            bar_h = max(2, level * (h - 10))
            x = i * bar_w
            self.mic_canvas.create_rectangle(x, h - bar_h, x + bar_w * 0.7, h,
                                              fill=COLOR_CYAN, outline="")
        self.mic_canvas.create_text(6, 8, anchor="nw", text=f"{int(current_volume_level * 100)}%",
                                     fill=COLOR_TEXT_DIM, font=("Consolas", 8))
        self.mic_canvas.create_text(w - 6, 8, anchor="ne", text=f"Mood: {last_detected_emotion}",
                                     fill=COLOR_TEXT_DIM, font=("Consolas", 8))

    def _update_phone_panel(self):
        s = phone_state
        if s["backend"] == "adb":
            self.phone_dot.config(text="\u25CF ADB", fg=COLOR_GOOD)
            lines = [f"Device: {s['device']}", f"Call: {s['call_state'].upper()}"]
        elif s["backend"] == "phonelink":
            self.phone_dot.config(text="\u25CF PHONE LINK", fg=COLOR_GOOD)
            lines = ["Bluetooth via Phone Link", "Call state unavailable"]
        else:
            self.phone_dot.config(text="\u25CF DISCONNECTED", fg=COLOR_WARN)
            lines = [s["note"] or "No phone connected"]
        lines.append(f"Contacts: {s['contacts']}")
        if s["last_call"]:
            lines.append(f"Last call: {s['last_call']}")
        self.phone_detail_label.config(text="\n".join(lines))

        if pending_incoming_call:
            self.phone_incoming_label.config(text=f"\u260E Incoming call: {pending_incoming_call['name']}")
            if not self.phone_incoming_frame.winfo_ismapped():
                self.phone_incoming_frame.pack(fill="x", pady=(0, 8), before=self.phone_dial_row)
        else:
            if self.phone_incoming_frame.winfo_ismapped():
                self.phone_incoming_frame.pack_forget()

    def _update_whatsapp_panel(self):
        ready = phone_state["backend"] == "adb"
        self.whatsapp_dot.config(text="\u25CF READY" if ready else "\u25CF NEEDS ADB",
                                  fg=COLOR_GOOD if ready else COLOR_WARN)
        if pending_whatsapp_voice and time.time() - pending_whatsapp_voice["time"] < 30:
            self.whatsapp_status_label.config(
                text=f"Listening for what to tell {pending_whatsapp_voice['target']}...", fg=COLOR_SPEAKING)
        elif ready:
            self.whatsapp_status_label.config(
                text="Type a contact and message, or say \"WhatsApp <name>: <message>\".", fg=COLOR_TEXT_DIM)
        else:
            self.whatsapp_status_label.config(
                text="Connect your phone over ADB to send (see Phone \u2192 Setup).", fg=COLOR_TEXT_DIM)

    # ---- Notifications panel ----
    def _notif_read_selected(self):
        sel = self.notif_list.curselection()
        if not sel or sel[0] >= len(self._notif_list_ids):
            return
        nid = self._notif_list_ids[sel[0]]
        entry = next((n for n in WHATSAPP_NOTIFICATIONS if n["id"] == nid), None)
        if entry:
            entry["read"] = True
            threading.Thread(target=lambda: speak_async(f"{entry['sender']} says: {entry['text']}"),
                             daemon=True).start()

    def _notif_read_all(self):
        unread = [n for n in WHATSAPP_NOTIFICATIONS if not n["read"]]
        if not unread:
            return
        for n in unread:
            n["read"] = True
        text = " ".join(f"{n['sender']} says: {n['text']}." for n in unread[:5])
        threading.Thread(target=lambda: speak_async(text), daemon=True).start()

    def _notif_clear(self):
        WHATSAPP_NOTIFICATIONS.clear()

    def _update_notif_panel(self):
        items = list(WHATSAPP_NOTIFICATIONS)[::-1]  # newest first
        snapshot = tuple((n["id"], n["read"]) for n in items)
        if snapshot == self._notif_last_shown:
            return
        self._notif_last_shown = snapshot
        self._notif_list_ids = [n["id"] for n in items]
        self.notif_list.delete(0, "end")
        for n in items:
            mark = "  " if n["read"] else "\u25CF "
            when = time.strftime("%H:%M", time.localtime(n["time"]))
            preview = n["text"] if len(n["text"]) <= 40 else n["text"][:37] + "..."
            self.notif_list.insert("end", f"{mark}{when} {n['sender']}: {preview}")
        if not items:
            self.notif_list.insert("end", "  No notifications yet")
        unread = sum(1 for n in items if not n["read"])
        self.notif_status_label.config(text=(f"{unread} unread" if unread else "All caught up."),
                                       fg=(COLOR_SPEAKING if unread else COLOR_TEXT_DIM))

    def _update_video_panel(self):
        with _vision_frame_lock:
            frame = latest_vision_frame

        if frame is None:
            if self.video_label.cget("image") != "":
                self.video_label.config(image="", text="Camera inactive", fg=COLOR_TEXT_DIM)
                self._video_photo = None
            return

        target_w = VISUALIZER_SIZE
        target_h = 190
        img = Image.fromarray(frame).resize((target_w, target_h))
        photo = ImageTk.PhotoImage(image=img)
        self._video_photo = photo  # keep a real reference - Tkinter silently drops the image otherwise
        self.video_label.config(image=photo, text="")

    def _update_log_panel(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        if activity_log:
            self.log_text.insert("1.0", "\n".join(activity_log))
        else:
            self.log_text.insert("1.0", "No activity yet.")
        self.log_text.config(state="disabled")

    def _update_status_text(self):
        if app_state == "listening":
            self.status_label.config(text="Listening...", fg=COLOR_CYAN)
        elif app_state == "speaking":
            self.status_label.config(text="Speaking...", fg=COLOR_SPEAKING)
        else:
            self.status_label.config(text="Idle", fg=COLOR_TEXT_DIM)

    # -----------------------------------------------------------------
    # Adaptive Smart Cube
    # -----------------------------------------------------------------
    def _cube_trigger_flip(self, new_index):
        """Starts a flip animation toward new_index. The face content
        itself swaps at the midpoint of the animation (see _update_cube),
        when the cube is edge-on and nothing is visible to swap under."""
        self.cube_pending_face_index = new_index
        self.cube_flip_frames_left = 6

    def _cube_face_content(self, face_type):
        """Returns (title, [lines]) of live data for one cube face."""
        mood = last_detected_emotion
        color, icon, label = MOOD_STYLE.get(mood, MOOD_STYLE["neutral"])

        if face_type == "mood":
            return "MOOD", [f"{icon} {label}", CUBE_MOOD_NOTES.get(mood, "")]

        if face_type == "time":
            now = time.localtime()
            return "TIME", [time.strftime("%H:%M:%S", now), time.strftime("%a %d %b", now)]

        if face_type == "system":
            return "SYSTEM", [f"CPU {SYSTEM_STATS_STATE['cpu']:.0f}%", f"MEM {SYSTEM_STATS_STATE['mem']:.0f}%"]

        if face_type == "subsystems":
            active = []
            if stop_listening is not None:
                active.append("Mic")
            if vision_active:
                active.append("Vision")
            if _ad_skipper_active:
                active.append("Ad-Skip")
            if phone_state["backend"] != "none":
                active.append("Phone")
            if PRODUCTIVE_MODE_ACTIVE:
                active.append("Productive")
            count_line = f"{len(active)}/5 online"
            names_line = ", ".join(active) if active else "All quiet"
            return "ACTIVE", [count_line, names_line]

        return "", [""]

    # -----------------------------------------------------------------
    # Hologram: mouse interaction + the actual 3D wireframe draw
    # -----------------------------------------------------------------
    def _hologram_drag_start_rotate(self, event):
        self._hologram_drag = {"mode": "rotate", "x": event.x, "y": event.y}

    def _hologram_drag_start_pan(self, event):
        self._hologram_drag = {"mode": "pan", "x": event.x, "y": event.y}

    def _hologram_drag_move(self, event):
        mode = self._hologram_drag.get("mode")
        if mode is None:
            return
        dx, dy = event.x - self._hologram_drag["x"], event.y - self._hologram_drag["y"]
        self._hologram_drag["x"], self._hologram_drag["y"] = event.x, event.y
        if mode == "rotate":
            HOLOGRAM_STATE["rot_y"] = (HOLOGRAM_STATE["rot_y"] + dx * 0.6) % 360
            HOLOGRAM_STATE["rot_x"] = max(-85, min(85, HOLOGRAM_STATE["rot_x"] - dy * 0.6))
            HOLOGRAM_STATE["last_interact"] = time.time()
        elif mode == "pan":
            hologram_pan(dx, dy)

    def _hologram_drag_end(self, _event):
        self._hologram_drag = {"mode": None, "x": 0, "y": 0}

    def _hologram_scroll_zoom(self, event):
        self._hologram_zoom_click(1.15 if event.delta > 0 else 1 / 1.15)

    def _hologram_zoom_click(self, factor):
        hologram_zoom(factor)

    def _hologram_pick_shape(self, name):
        hologram_show_shape(name)
        log_activity(f"Hologram: showing {name}")

    def _hologram_text_go(self):
        text = self.hologram_text_entry.get().strip()
        if text and text != "Hologram anything...":
            hologram_show_text(text)
            log_activity(f"Hologram: showing text '{text}'")
            self.hologram_text_entry.delete(0, tk.END)

    def _hologram_reset_click(self):
        hologram_reset_view()

    def _hologram_toggle_spin(self):
        HOLOGRAM_STATE["auto_rotate"] = not HOLOGRAM_STATE["auto_rotate"]

    def _hologram_pulse_click(self):
        """A quick punch-zoom + chirp on whatever's currently shown -
        doesn't change the shape, just a one-off 'pulse wave' flourish."""
        ui_beep(1500, 90)
        base_zoom = HOLOGRAM_STATE["zoom"]
        HOLOGRAM_STATE["zoom"] = min(HOLOGRAM_ZOOM_MAX, base_zoom * 1.35)
        self.root.after(260, lambda bz=base_zoom: HOLOGRAM_STATE.__setitem__("zoom", bz))

    def _draw_hologram(self):
        canvas = self.hologram_canvas
        canvas.delete("all")
        size = HOLOGRAM_SIZE
        state = HOLOGRAM_STATE

        # Auto-rotate, but pause it for a bit right after a manual drag so
        # a drag doesn't immediately get overridden - same idea as most
        # 3D viewers (CAD tools, model viewers, etc).
        if state["auto_rotate"] and time.time() - state["last_interact"] > HOLOGRAM_AUTOROTATE_RESUME_SEC:
            state["rot_y"] = (state["rot_y"] + 0.6) % 360

        # Faint radial glow backdrop + a slowly sweeping scan line - the
        # two cheap touches that read as "hologram" rather than "wireframe
        # viewer": a soft glow field and a moving horizontal scan band.
        canvas.create_oval(size * 0.08, size * 0.08, size * 0.92, size * 0.92,
                            outline=COLOR_CYAN_DIM, width=1)
        scan_y = (size * 0.15) + (size * 0.7) * (0.5 + 0.5 * math.sin(time.time() * 0.9))
        canvas.create_line(size * 0.1, scan_y, size * 0.9, scan_y, fill=COLOR_CYAN_DIM, width=1)

        if state["mode"] == "text":
            glyphs = _hologram_text_glyphs(state["text"])
            projected = [(_hologram_project(pt, size, state), ch) for ch, pt in glyphs]
            if projected:
                z_vals = [p[0][2] for p in projected]
                z_min, z_max = min(z_vals), max(z_vals)
                for (sx, sy, z2), ch in sorted(projected, key=lambda p: -p[0][2]):  # far-to-near paint order
                    color = _hologram_depth_color(z2, z_min, z_max)
                    font_size = 15 + int(9 * (1 - (z2 - z_min) / max(0.001, z_max - z_min)))
                    canvas.create_text(sx, sy, text=ch, fill=color, font=("Consolas", font_size, "bold"))
        else:
            pts, edges = _hologram_geometry(state["shape"])
            projected = [_hologram_project(p, size, state) for p in pts]
            z_vals = [p[2] for p in projected] or [0.0]
            z_min, z_max = min(z_vals), max(z_vals)
            # Edges painted back-to-front by midpoint depth, so nearer lines
            # overlap farther ones correctly instead of in arbitrary order.
            edge_list = sorted(edges, key=lambda e: -(projected[e[0]][2] + projected[e[1]][2]))
            for i, j in edge_list:
                (x1, y1, z1), (x2, y2, z2) = projected[i], projected[j]
                color = _hologram_depth_color((z1 + z2) / 2, z_min, z_max)
                canvas.create_line(x1, y1, x2, y2, fill=color, width=2)
            referenced = {i for e in edges for i in e}
            for idx, (x, y, z2) in enumerate(projected):
                if idx not in referenced:  # standalone points (e.g. the atom's nucleus) as a glowing dot
                    color = _hologram_depth_color(z2, z_min, z_max)
                    canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")

        label = state["text"] if state["mode"] == "text" else state["shape"].upper()
        canvas.create_text(size / 2, size - 10, text=label, font=("Consolas", 8), fill=COLOR_TEXT_DIM)

        shown = (state["mode"], state["shape"], state["text"])
        if shown != self._hologram_last_shape_shown:
            self._hologram_last_shape_shown = shown
        spin_icon = "\u23F8" if state["auto_rotate"] else "\u25B6"
        if self.hologram_spin_button["text"] != spin_icon:
            self.hologram_spin_button.config(text=spin_icon)

    def _update_cube(self):
        global last_detected_emotion
        # A mood change pre-empts the clock and jumps straight to the mood
        # face, so the cube visibly reacts to you rather than only ticking
        # on a timer. Ignored mid-flip so animations never overlap.
        if last_detected_emotion != self.cube_last_mood and self.cube_flip_frames_left == 0:
            self.cube_last_mood = last_detected_emotion
            self._cube_trigger_flip(CUBE_FACE_TYPES.index("mood"))
            self.cube_last_switch_time = time.time()
            log_activity(f"Cube: mood shifted to {last_detected_emotion}")
        elif self.cube_flip_frames_left == 0 and time.time() - self.cube_last_switch_time >= 60:
            next_index = (self.cube_face_index + 1) % len(CUBE_FACE_TYPES)
            self._cube_trigger_flip(next_index)
            self.cube_last_switch_time = time.time()

        if self.cube_flip_frames_left > 0:
            self.cube_flip_frames_left -= 1
            if self.cube_flip_frames_left == 3:  # edge-on midpoint - safe to swap content
                self.cube_face_index = self.cube_pending_face_index

        self._draw_cube()

    def _draw_cube(self):
        self.cube_canvas.delete("all")
        w = self.cube_canvas.winfo_width() or (VISUALIZER_SIZE + 20)
        h = self.cube_canvas.winfo_height() or 170

        mood = last_detected_emotion
        color, icon, label = MOOD_STYLE.get(mood, MOOD_STYLE["neutral"])

        s, dx, dy = 108, 34, -20
        fx = (w - (s + dx)) / 2
        fy = (h - (s - dy)) / 2 - 4

        # Top and right faces give the isometric-cube illusion; stipple
        # dithers the fill to fake light/shadow without needing alpha.
        self.cube_canvas.create_polygon(
            fx, fy, fx + dx, fy + dy, fx + s + dx, fy + dy, fx + s, fy,
            fill=color, stipple="gray50", outline=color, width=1,
        )
        self.cube_canvas.create_polygon(
            fx + s, fy, fx + s + dx, fy + dy, fx + s + dx, fy + dy + s, fx + s, fy + s,
            fill=color, stipple="gray25", outline=color, width=1,
        )

        # Front face: the one that actually rotates through the live faces.
        # During a flip it's squashed toward zero width at the midpoint
        # (an edge-on cube) then back out - a real flip, not a fade.
        if self.cube_flip_frames_left > 0:
            progress = (6 - self.cube_flip_frames_left) / 6
            flip_scale = abs(math.cos(math.pi * progress))
        else:
            flip_scale = 1.0
        front_w = max(2, s * flip_scale)
        fx2 = fx + (s - front_w) / 2
        self.cube_canvas.create_rectangle(fx2, fy, fx2 + front_w, fy + s,
                                           fill=COLOR_PANEL, outline=color, width=2)

        if flip_scale > 0.3:
            title, lines = self._cube_face_content(CUBE_FACE_TYPES[self.cube_face_index])
            cx = fx2 + front_w / 2
            self.cube_canvas.create_text(cx, fy + 16, text=title, font=("Consolas", 9, "bold"), fill=color)
            ty = fy + 36
            for i, ln in enumerate(lines):
                if not ln:
                    continue
                # A long line (e.g. several active subsystems joined together)
                # wraps to 2+ lines at this width. Advancing by a FIXED gap
                # here assumed every line was always one line tall, so the
                # next item started drawing before a wrapped line actually
                # finished - that's what caused the overlap. Measuring the
                # real rendered bounding box after drawing fixes it for any
                # content length, wrapped or not.
                if i == 0:  # headline value - big, one line
                    item = self.cube_canvas.create_text(cx, ty, text=ln, font=("Consolas", 12, "bold"),
                                                         fill=COLOR_TEXT, anchor="n", width=front_w - 10)
                else:       # supporting detail - smaller, wraps downward from its own top edge
                    item = self.cube_canvas.create_text(cx, ty, text=ln, font=("Consolas", 9),
                                                         fill=COLOR_TEXT_DIM, anchor="n", justify="center",
                                                         width=front_w - 14)
                bbox = self.cube_canvas.bbox(item)
                ty = (bbox[3] if bbox else ty + 20) + 8  # real bottom edge of what was just drawn, plus a small gap

        remaining = max(0, 60 - int(time.time() - self.cube_last_switch_time))
        self.cube_canvas.create_text(w / 2, h - 10, text=f"{icon} {label}  \u00b7  next flip {remaining}s",
                                      font=("Consolas", 8), fill=COLOR_TEXT_DIM)

    def _load_color(self, pct):
        """Cyan (calm) -> amber (busy) -> red (heavy load) - the Arc Core's
        whole color scheme reacts to this, not just a text number."""
        if pct < 40:
            return COLOR_CYAN
        if pct < 75:
            return COLOR_SPEAKING
        return COLOR_WARN

    def _draw_visualizer(self):
        """The 'Nova Visor' - a stylized HUD-visor silhouette with a rotating
        radar rim, standing in for a literal helmet render. Being upfront:
        tkinter's canvas only draws flat 2D shapes, so this is a vector
        silhouette + animated sweep line evoking the visor look, not a
        photorealistic 3D render - there's no 3D engine or image asset
        behind it. It's still fully live: rim rotation, the load gauge,
        the visor's glow color, and the scan line are all driven by real
        CPU/RAM and mic data, not just decoration."""
        import math
        self.canvas.delete("all")
        cx = cy = VISUALIZER_SIZE / 2
        VISUALIZER_HISTORY.append(current_volume_level)

        # Static targeting-bracket frame around the whole visor canvas,
        # matching the panels' corner-bracket styling
        bracket = 22
        for cx0, cy0, sx, sy in (
            (2, 2, 1, 1), (VISUALIZER_SIZE - 2, 2, -1, 1),
            (2, VISUALIZER_SIZE - 2, 1, -1), (VISUALIZER_SIZE - 2, VISUALIZER_SIZE - 2, -1, -1),
        ):
            self.canvas.create_line(cx0, cy0 + sy * bracket, cx0, cy0, cx0 + sx * bracket, cy0,
                                     fill=COLOR_CYAN_DIM, width=2)

        cpu, mem = SYSTEM_STATS_STATE["cpu"], SYSTEM_STATS_STATE["mem"]
        load = (cpu + mem) / 2
        load_color = self._load_color(load)
        speaking = app_state == "speaking"
        core_color = COLOR_SPEAKING if speaking else load_color

        self.core_rotation = (self.core_rotation + 3) % 360

        # Outer rotating radar rim - color reacts to system load
        outer_radius = 118
        for i in range(24):
            angle = math.radians(i * 15 + self.core_rotation)
            x1 = cx + (outer_radius - 8) * math.cos(angle)
            y1 = cy + (outer_radius - 8) * math.sin(angle)
            x2 = cx + outer_radius * math.cos(angle)
            y2 = cy + outer_radius * math.sin(angle)
            self.canvas.create_line(x1, y1, x2, y2, fill=COLOR_CYAN_DIM, width=2)

        # Load gauge ring - partial arc filled proportionally to (CPU+RAM)/2
        gauge_radius = 100
        self.canvas.create_oval(cx - gauge_radius, cy - gauge_radius,
                                 cx + gauge_radius, cy + gauge_radius,
                                 outline=COLOR_CYAN_DIM, width=2)
        self.canvas.create_arc(
            cx - gauge_radius, cy - gauge_radius, cx + gauge_radius, cy + gauge_radius,
            start=90 - self.core_rotation * 0.3, extent=-max(6, load * 3.6),
            style=tk.ARC, outline=load_color, width=6,
        )
        # Small radar blips at the rim, one per subsystem, lit up only when
        # that subsystem is actually active - a real status readout, not
        # random decoration, doubling as the "constellation of dots" look
        blip_states = [
            vision_active, _ad_skipper_active, stop_listening is not None,
            phone_state["backend"] != "none",
        ]
        for i, active in enumerate(blip_states):
            angle = math.radians(i * 90 + 45 + self.core_rotation * 0.5)
            bx = cx + (gauge_radius + 12) * math.cos(angle)
            by = cy + (gauge_radius + 12) * math.sin(angle)
            r = 4
            self.canvas.create_oval(bx - r, by - r, bx + r, by + r,
                                     fill=(COLOR_GOOD if active else COLOR_PANEL_BORDER), outline="")

        # ---- The visor itself: a flattened hexagon silhouette ----
        half_w, half_h = 100, 52
        visor_pts = [
            cx - half_w, cy,
            cx - half_w * 0.55, cy - half_h,
            cx + half_w * 0.55, cy - half_h,
            cx + half_w, cy,
            cx + half_w * 0.55, cy + half_h,
            cx - half_w * 0.55, cy + half_h,
        ]
        self.canvas.create_polygon(visor_pts, outline=core_color, width=3,
                                    fill=COLOR_PANEL, smooth=True, joinstyle=tk.ROUND)

        # "Temple" struts flaring out from the visor tips - helmet-adjacent
        # silhouette cue without depicting an actual helmet/face
        for side in (-1, 1):
            tip_x = cx + side * half_w
            self.canvas.create_line(tip_x, cy, tip_x + side * 22, cy - 14, fill=COLOR_CYAN_DIM, width=2)
            self.canvas.create_line(tip_x, cy, tip_x + side * 22, cy + 14, fill=COLOR_CYAN_DIM, width=2)

        # Scanning sweep line inside the visor - a real back-and-forth
        # triangle-wave animation driven by core_rotation, not a static image
        phase = (self.core_rotation % 360) / 360
        triangle = abs((phase * 2) % 2 - 1)  # 0 -> 1 -> 0, smooth back and forth
        scan_y = cy - half_h * 0.75 + (half_h * 1.5) * triangle
        self.canvas.create_line(cx - half_w * 0.5, scan_y, cx + half_w * 0.5, scan_y,
                                 fill=core_color, width=2)

        # Core pulse ring, still reacting to live mic input level, drawn
        # centered on the visor for a glowing-eye effect
        base_radius = 30
        pulse = current_volume_level * 14
        self.canvas.create_oval(cx - base_radius - pulse, cy - 6 - base_radius - pulse,
                                 cx + base_radius + pulse, cy - 6 + base_radius + pulse,
                                 outline=core_color, width=1)

        self.canvas.create_text(cx, cy - 8, text="NOVA", font=("Consolas", 14, "bold"), fill=core_color)
        self.canvas.create_text(cx, cy + 14, text=f"{load:.0f}% LOAD", font=("Consolas", 8), fill=COLOR_TEXT_DIM)


def _setup_crash_logging():
    """
    Redirect stdout/stderr to a log file, and install hooks that catch
    ANY otherwise-uncaught exception - main thread or background thread -
    and log it instead of letting it vanish silently. Essential once
    there's no console window to show errors in.
    """
    import traceback

    log_f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_f
    sys.stderr = log_f
    print(f"\n--- Nova started {time.ctime()} ---")

    def log_uncaught(exc_type, exc_value, exc_tb):
        print("UNCAUGHT EXCEPTION (main thread):")
        print("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def log_uncaught_thread(args):
        print(f"UNCAUGHT EXCEPTION (thread: {args.thread.name}):")
        print("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = log_uncaught
    threading.excepthook = log_uncaught_thread


def run_gui_app():
    _setup_crash_logging()
    start_phone_monitor()
    start_news_watch()
    try:
        root = tk.Tk()
        NovaGUI(root)
        root.mainloop()
    except Exception as e:
        import traceback
        print("Fatal error in GUI:", traceback.format_exc())
    finally:
        stop_phone_monitor()


# =====================================================================
# ---------- Main ----------
# =====================================================================
if __name__ == "__main__":
    run_gui_app()
