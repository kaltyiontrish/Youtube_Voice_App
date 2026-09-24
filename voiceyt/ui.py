"""Minimal overlay UI + tray icon (voiceyt.ui).

The microphone stays the input; this module is only feedback and control:

* one frameless, always-on-top window shows a status dot (green listening,
  yellow busy, orange paused, red error), what was heard in large type, the
  last action / now-playing title with its pool position and volume, plus the
  result pool as five clickable rows (click = jump to that track),
* a tray icon offers Pause/Resume, a microphone picker and Quit.

Threading contract (as in the rest of the project):

* the tkinter mainloop runs in its own thread and is the only thread that
  touches widgets; every other thread talks to it through :class:`UiState`,
* the tray is a hidden win32 window with its own message loop (raw ctypes, no
  pystray/pywin32 dependency) running in a second thread,
* the player is polled, so nothing was added to the daemon's hot path beyond
  the ``UiState`` setters called from the existing callbacks.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

from .config import atomic_write_text, save_config_sections
from .ui_theme import get_theme, theme_names

LOGGER = logging.getLogger(__name__)

# ---- look & feel -----------------------------------------------------------

FADE_AFTER_S = 30.0      # quiet time before the overlay dims
HIDE_AFTER_S = 120.0     # quiet time before the overlay goes away
FADE_ALPHA = 0.35        # opacity while dimmed
POLL_MS = 250            # overlay refresh period
PLAYER_POLL_EVERY = 8    # 250 ms x 8 = 2 s between mpv volume IPC calls
HEARD_LIFETIME_S = 8.0   # how long the big "heard" line stays on screen
ERROR_LIFETIME_S = 8.0   # how long an error keeps the dot red
ACTION_LIFETIME_S = 9.0  # how long the last action label stays
HEARD_MAX_CHARS = 110    # about two wrapped lines
LEVEL_SPEAK = 0.02       # RMS above this = "you are speaking"
LEVEL_FULL = 0.10        # RMS that fills the meter bar
TITLE_MAX_CHARS = 46
POOL_MAX_CHARS = 44
RECENT_MAX_CHARS = 22    # E1: strip button label width
POOL_ROWS = 5
RECENT_MAX = 10          # E1: how many play queries the history strip keeps
WINDOW_WIDTH = 400
WINDOW_HEIGHT = 214
WINDOW_POS = "+40+40"

BG = "#101216"
SURFACE = "#181b20"
SURFACE_ALT = "#20242a"
FG = "#f4f6f8"
FG_DIM = "#9aa1aa"
ACCENT = "#1db954"
ACCENT_DIM = "#168a43"
DANGER = "#f05d5e"
HOVER = "#303840"

# ---- win32 plumbing --------------------------------------------------------
# Everything is bound lazily so importing this module stays cheap (and works
# even without a desktop session): nothing here runs until the tray starts.


class WNDCLASSW(ctypes.Structure):
    """``WNDCLASSW`` is not part of ``ctypes.wintypes``."""

    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]


class NOTIFYICONDATAW(ctypes.Structure):
    """``NOTIFYICONDATAW`` is not part of ``ctypes.wintypes``."""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

SEPARATOR = "#2a2f36"
DOT_IDLE = "#7bd88f"     # listening
DOT_BUSY = "#ffd166"     # heard something
DOT_PAUSED = "#f4a261"   # paused from the tray
DOT_ERROR = "#ef5350"    # last command failed
DOT_OFF = "#4a4f57"      # starting / shutting down



class _Win32:
    """Bindings with explicit prototypes (ctypes defaults truncate handles)."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        u = self.user32
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.RegisterClassW.restype = wt.WORD
        u.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
        ]
        u.CreateWindowExW.restype = wt.HWND
        u.DestroyWindow.argtypes = [wt.HWND]
        u.DestroyWindow.restype = wt.BOOL
        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.PostMessageW.restype = wt.BOOL
        u.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
        u.LoadIconW.restype = wt.HICON
        u.CreatePopupMenu.argtypes = []
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        u.AppendMenuW.restype = wt.BOOL
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.TrackPopupMenuEx.argtypes = [
            wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p
        ]
        u.TrackPopupMenuEx.restype = wt.UINT
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        self.kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wt.HMODULE
        self.shell32.Shell_NotifyIconW.argtypes = [
            wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)
        ]
        self.shell32.Shell_NotifyIconW.restype = wt.BOOL


_WIN32: "_Win32 | None" = None
_WIN32_LOCK = threading.Lock()


def _win32() -> _Win32:
    """The single api table, built on first use (never at import time)."""
    global _WIN32
    with _WIN32_LOCK:
        if _WIN32 is None:
            _WIN32 = _Win32()
    return _WIN32


def _resource(value: int) -> wt.LPCWSTR:
    """``MAKEINTRESOURCE``: a small integer dressed up as a string pointer."""
    return ctypes.cast(ctypes.c_void_p(value), wt.LPCWSTR)


# ---- shared state ----------------------------------------------------------


class UiState:
    """Thread-safe mailbox between the daemon threads and the UI threads.

    ``player`` and ``capture`` are only read (mpv IPC locks internally and the
    capture exposes plain attributes), everything else goes through the lock
    or through the two :class:`threading.Event` flags.
    """

    def __init__(self, player=None, capture=None, state_path: Path | None = None,
                 playback=None, preview_max: int = POOL_ROWS):
        self.player = player        # MpvPlayer | None - polled for titles/volume
        self.playback = playback    # PlaybackService | None - shared snapshot source
        self.capture = capture      # AudioCapture | None - marked in the mic menu
        self.state_path = state_path
        self._lock = threading.Lock()
        self._heard = ""
        self._heard_ts = 0.0
        self._action = ""
        self._action_ts = 0.0
        self._error = ""
        self._error_ts = 0.0
        self._volume = 50           # current volume (0-100)
        self._playing = False       # whether a track is currently playing
        self._aec = ""              # B6: one-line AEC state for the status bar
        self._backend_name = ""     # current ASR backend name
        self._language = ""         # current language
        self._recent: list[str] = []  # E1: newest-first spoken play queries
        self._search_results: list[dict] = []
        self._search_results_ts = 0.0
        self._voice_previews: list[str] = []
        self._voice_preview_urls: list[str] = []
        self._last_browse_query = ""
        self._last_voice_query = ""
        self._preview_max = max(1, int(preview_max))
        self._playback_snapshot = None
        self._load_saved_state()
        self.paused = threading.Event()
        self.shutdown = threading.Event()
        self.overlay_done = threading.Event()

    def _load_saved_state(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._recent = [str(item) for item in data.get("recent", [])][:RECENT_MAX]
            self._search_results = [dict(item) for item in data.get("browse_results", [])]
            self._search_results_ts = float(data.get("browse_results_ts", 0.0))
            self._last_browse_query = str(data.get("browse_query", ""))
            self._last_voice_query = str(data.get("voice_query", ""))
            self._voice_previews = [str(item) for item in data.get("voice_previews", [])][:self._preview_max]
            self._voice_preview_urls = [str(item) for item in data.get("voice_preview_urls", [])][:self._preview_max]
            if "volume" in data:
                self._volume = max(0, min(100, int(data["volume"])))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("could not load UI state %s: %s", self.state_path, exc)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "recent": self._recent,
            "browse_query": self._last_browse_query,
            "browse_results": self._search_results,
            "browse_results_ts": self._search_results_ts,
            "voice_query": self._last_voice_query,
            "voice_previews": self._voice_previews,
            "voice_preview_urls": self._voice_preview_urls,
            "volume": self._volume,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.state_path)
        except OSError as exc:
            LOGGER.warning("could not save UI state %s: %s", self.state_path, exc)

    # -- written by the daemon threads -----------------------------------

    def set_heard(self, text: str) -> None:
        with self._lock:
            self._heard, self._heard_ts = text, time.monotonic()

    def set_action(self, label: str) -> None:
        with self._lock:
            self._action, self._action_ts = label, time.monotonic()

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error, self._error_ts = message, time.monotonic()

    def set_volume(self, volume: int) -> None:
        with self._lock:
            self._volume = max(0, min(100, volume))
            self._save_state()

    def set_playing(self, playing: bool) -> None:
        with self._lock:
            self._playing = playing

    def set_playback_snapshot(self, snapshot) -> None:
        """Cache the latest service-owned playback snapshot for UI consumers."""
        with self._lock:
            self._playback_snapshot = snapshot

    def set_aec(self, label: str) -> None:
        """B6: one-line AEC state shown in the expanded status bar."""
        with self._lock:
            self._aec = label

    def set_backend_name(self, name: str) -> None:
        with self._lock:
            self._backend_name = name

    def set_language(self, language: str) -> None:
        with self._lock:
            self._language = language

    def note_query(self, query: str) -> None:
        """E1: remember one spoken ``play`` query for the history strip.

        Newest first, duplicates of the previous entry collapsed (saying the
        same thing twice in a row should not fill the strip), capped at
        :data:`RECENT_MAX`.
        """
        text = (query or "").strip()
        if not text:
            return
        with self._lock:
            if self._recent[:1] == [text]:
                return
            self._recent.insert(0, text)
            del self._recent[RECENT_MAX:]
            self._last_voice_query = text
            self._save_state()

    def set_voice_previews(self, titles: list[str], urls: list[str] | None = None) -> None:
        """Show and persist the latest voice results and their canonical URLs."""
        with self._lock:
            self._voice_previews = [str(title) for title in titles[:self._preview_max]]
            self._voice_preview_urls = [str(url) for url in (urls or [])[:self._preview_max]]
            self._save_state()

    def set_search_results(self, results: list[dict], query: str = "") -> None:
        with self._lock:
            self._search_results = [dict(result) for result in results]
            self._search_results_ts = time.monotonic()
            if query.strip():
                self._last_browse_query = query.strip()
            self._save_state()

    def recent_queries(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    # -- read by the ui threads ------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "heard": self._heard,
                "heard_ts": self._heard_ts,
                "action": self._action,
                "action_ts": self._action_ts,
                "error": self._error,
                "error_ts": self._error_ts,
                "volume": self._volume,
                "playing": self._playing,
                "aec": self._aec,
                "backend_name": self._backend_name,
                "language": self._language,
                "voice_previews": list(self._voice_previews),
                "voice_preview_urls": list(self._voice_preview_urls),
                "recent": list(self._recent),
                "search_results": [dict(result) for result in self._search_results],
                "search_results_ts": self._search_results_ts,
                "browse_query": self._last_browse_query,
                "voice_query": self._last_voice_query,
                "playback": self._playback_snapshot,
            }


def list_mics() -> list[tuple[int, str]]:
    """Input devices as ``(index, name)``; safe to call from the tray thread."""
    try:
        from .audio import list_input_devices

        return [(device.index, device.name) for device in list_input_devices()]
    except Exception as exc:  # sounddevice can fail when there is no device
        LOGGER.warning("cannot list microphones: %s", exc)
        return []


def _format_duration(value) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "--:--"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _backend_names() -> tuple[str, ...]:
    """E3: the registry's backend names, for the settings dropdown.

    Reuses ``available_backends()`` so a new backend module shows up in the
    picker without touching the UI.
    """
    try:
        from .asr.registry import available_backends

        return available_backends()
    except Exception:  # a broken optional dependency must not kill the tab
        LOGGER.warning("cannot list ASR backends", exc_info=True)
        return ("whisper",)


def _mic_choice(cfg, mics: list[tuple[int, str]]) -> str:
    """D1: render ``audio.device`` as one of the combobox choices."""
    device = getattr(getattr(cfg, "audio", None), "device", None)
    if device is not None:
        for index, name in mics:
            if index == device:
                return f"{index}: {name}"
    return "default"


def _mic_index(choice: str, mics: list[tuple[int, str]]) -> int | None:
    """D1: parse a combobox choice back into a device index (None = default)."""
    if not choice or choice == "default":
        return None
    head = choice.split(":", 1)[0].strip()
    try:
        index = int(head)
    except ValueError:
        return None
    return index if any(index == i for i, _ in mics) else None


def _project_root() -> Path:
    """The checkout root (``ui.py`` lives in ``<root>/voiceyt/``)."""
    return Path(__file__).resolve().parents[1]


def _coerce(raw, kind: str):
    """D1: turn one form string into the value its config key expects.

    Raises ``ValueError``/``TypeError`` so the caller can name the key; the
    value never reaches the file unvalidated (``config.load_config`` re-checks
    the whole document afterwards anyway).
    """
    if kind == "bool":
        return bool(raw)
    text = "" if raw is None else str(raw).strip()
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "list":
        items = [part.strip() for part in text.split(",") if part.strip()]
        if not items:
            raise ValueError("needs at least one value")
        return items
    if kind == "str_or_null":
        return text or None
    return text


def _config_section(cfg, section: str):
    """The dataclass behind a YAML section name (``ui`` is ``ui_config``)."""
    if section == "ui":
        return getattr(cfg, "ui_config", None)
    return getattr(cfg, section, None)


def _no_display(exc: BaseException) -> bool:
    """True when tkinter exists but there is no window station (headless run)."""
    return "no display" in str(exc).lower() or "couldn't connect" in str(exc).lower()


def _ask_string(parent, title: str, prompt: str, initial: str = "") -> str | None:
    """C2: modal one-line prompt.  Returns None on cancel *or* headless."""
    try:
        from tkinter import simpledialog
        answer = simpledialog.askstring(title, prompt, initialvalue=initial,
                                        parent=parent)
    except Exception as exc:                # no display, or Tk torn down
        if not _no_display(exc):
            LOGGER.exception("%s dialog failed", title)
        return None
    return answer if answer and answer.strip() else None


def _ask_yes_no(parent, title: str, prompt: str) -> bool:
    """C2: modal confirmation.  Conservative default is False."""
    try:
        from tkinter import messagebox
        return bool(messagebox.askyesno(title, prompt, parent=parent))
    except Exception as exc:
        if not _no_display(exc):
            LOGGER.exception("%s dialog failed", title)
        return False


def _run_in_background(label: str, function, *args) -> None:
    """Keep slow mpv/yt-dlp work off Tk's event thread."""
    def worker() -> None:
        try:
            function(*args)
        except Exception:
            LOGGER.exception("%s failed", label)

    threading.Thread(target=worker, name=f"voiceyt-ui-{label}", daemon=True).start()


def _player_transport(player, state: UiState, action: str) -> None:
    """Transport buttons use the shared service when available, else the player."""
    playback = getattr(state, "playback", None)
    if player is None and playback is None:
        return
    try:
        if action == "play_pause":
            playing = (
                playback.snapshot().state.value == "playing"
                if playback is not None
                else bool(getattr(player, "is_playing", lambda: getattr(player, "playing", False))())
            )
            if playing:
                (playback.pause() if playback is not None else player.pause())
            else:
                (playback.unpause() if playback is not None else player.unpause())
        elif action == "pause":
            playback.pause() if playback is not None else player.pause()
        elif action == "stop":
            playback.stop() if playback is not None else player.stop()
        elif action == "prev":
            playback.prev_track() if playback is not None else player.prev_track()
        elif action == "next":
            playback.next_track() if playback is not None else player.next_track()
        elif action == "resume":
            playback.unpause() if playback is not None else player.unpause()
    except Exception as exc:  # mpv may be restarting; report, don't crash
        LOGGER.warning("player %s failed: %s", action, exc)
        state.set_error(f"player: {exc}")


def _player_toggle_mute(player, state: UiState, previous: float | None = None) -> None:
    """Worker-safe mute toggle; use service snapshot/volume when present."""
    playback = getattr(state, "playback", None)
    if playback is not None:
        current = float(state.snapshot().get("volume", 50))
    elif player is not None:
        current = player.volume()
    else:
        return
    if current > 0:
        _player_set_volume(player, state, 0.0)
        state.set_action("muted")
        return
    _player_set_volume(player, state, previous if previous is not None else 50.0)
    state.set_action("unmuted")


def _player_set_volume(player, state: UiState, raw_value) -> None:
    """B4: commit volume through the service when present, with player fallback."""
    playback = getattr(state, "playback", None)
    if player is None and playback is None:
        return
    try:
        applied = playback.set_volume(float(raw_value)) if playback is not None else player.set_volume(float(raw_value))
    except Exception as exc:
        LOGGER.warning("player set_volume failed: %s", exc)
        state.set_error(f"player: {exc}")
        return
    state.set_volume(int(round(applied)))


def _player_step_volume(player, state: UiState, delta: float) -> None:
    """Send UI volume changes directly to the service/mpv, never voice dispatch."""
    playback = getattr(state, "playback", None)
    if player is None and playback is None:
        return
    try:
        current = float(state.snapshot().get("volume", 50))
        target = max(0.0, min(130.0, current + float(delta)))
        applied = playback.set_volume(target) if playback is not None else player.set_volume(target)
        state.set_volume(int(round(applied)))
    except Exception as exc:
        LOGGER.warning("player volume step failed: %s", exc)
        state.set_error(f"player: {exc}")



def _player_jump_to(player, state: UiState, index: int) -> bool:
    """B5: expanded listbox click -> the same jump the compact rows use.

    Imported decisions: click an index, jump to it.  No extra guards here -
    the shared ``_ui_jump_to_pool`` in ``__main__`` owns the bounds check,
    so a click on a stale row (pool shrank since the last paint) is a no-op.
    """
    if player is None:
        return False
    try:
        playback = getattr(state, "playback", None)
        titles = player.pool_titles() if player is not None else []
        if index < 0 or index >= len(titles):
            return False
        return bool(playback.jump_to(index) if playback is not None else player.jump_to(index))
    except Exception as exc:  # loud jump failure lands on the overlay
        LOGGER.warning("player jump_to(%d) failed: %s", index, exc)
        state.set_error(f"player: {exc}")
        return False


def _player_status_line(player, state: UiState, snap: dict) -> str:
    """B6: mic name + ASR backend + mpv alive + AEC state for the status bar.

    Anything without an answer is simply skipped, so the bar never shows
    ``unknown`` placeholders - it shows what it knows.
    """
    parts: list[str] = []
    capture = state.capture
    mic = getattr(capture, "device_name", "") if capture is not None else ""
    parts.append(f"mic: {mic}" if mic else "mic: none")
    if snap.get("backend_name"):
        parts.append(str(snap["backend_name"]))
    if player is None:
        parts.append("mpv: none")
    else:
        try:
            parts.append("mpv: on" if player.alive else "mpv: off")
        except Exception:
            parts.append("mpv: ?")
    aec = snap.get("aec") or "AEC off"
    parts.append(str(aec))
    return "  |  ".join(parts)


# ---- overlay ---------------------------------------------------------------


def _window_size(config) -> tuple[int, int]:
    """Window pixel size: expanded reads the config, compact keeps 400x214."""
    if config is not None and config.mode == "expanded":
        return config.overlay_width, config.overlay_height
    return WINDOW_WIDTH, WINDOW_HEIGHT


class Overlay(tk.Tk):
    """Frameless always-on-top window that renders a :class:`UiState`.

    Built and ticked on one thread (see :func:`start_ui`); it only ever reads
    the state snapshot, so daemon threads never touch a widget.
    """

    def __init__(self, state: UiState, config=None, on_pool_click=None,
                 app_config=None, on_play_query=None, on_play_playlist=None,
                 on_browse_query=None, on_quit=None):
        super().__init__()
        self.ui_state = state
        self.ui_config = config
        self.app_config = app_config        # D: live Config the tabs read/write
        self.on_pool_click = on_pool_click
        self.on_play_query = on_play_query  # E2: player-tab search box
        self.on_play_playlist = on_play_playlist  # saved playlists use shared queue
        self._on_browse_query = on_browse_query  # search is browse-only
        self._on_quit = on_quit
        self.expanded = config is not None and config.mode == "expanded"
        self._hidden = True
        self._hidden_for_playback = False    # hide_while_playing hid us
        self._meter_frac = 0.0               # smoothed mic level (0..1)
        self._tick = 0                       # decimated mpv IPC counter (B6)
        self._volume_dragging = False        # poll must not fight the slider
        self._win_width, self._win_height = _window_size(config)
        self._fade_after_s = config.fade_after_s if config is not None else FADE_AFTER_S
        self._hide_after_s = config.hide_after_s if config is not None else HIDE_AFTER_S
        self._hide_while_playing = bool(config.hide_while_playing) if config is not None else False
        self._voice_preview_list: tk.Listbox | None = None
        self._preview_titles: list[str] = []
        self._voice_preview_scroll: ttk.Scrollbar | None = None
        self._vol_var = tk.DoubleVar(value=50.0)  # compatibility helper; no slider is shown
        self._vol_label: tk.StringVar | None = None  # volume readout is in the status line
        self._mute_before: float | None = None
        self._volume_dragging = False
        self._play_pause_button: tk.Button | None = None
        self._seek_var: tk.DoubleVar | None = None
        self._seek_label: tk.StringVar | None = None
        self._seek_dragging = False
        self._seek_duration: float | None = None
        self._seek_last_committed: float | None = None
        self._now_var: tk.StringVar | None = None   # persistent now-playing title
        self._queue_var: tk.StringVar | None = None
        self._status_var: tk.StringVar | None = None  # B6: status bar text
        self._browse_list: tk.Listbox | None = None
        self._browse_results: list[dict] = []
        self._browse_msg: tk.StringVar | None = None
        # E1/E2/E5: player-tab extras (all expanded-only, all optional)
        self._search_var: tk.StringVar | None = None   # E2: query entry
        self._recent_box: tk.Widget | None = None      # E1: history strip frame
        self._recent_buttons: list[tk.Widget] = []     # E1: rebuilt on change
        self._recent_shown: list[str] = []             # E1: what is on screen
        self._cheat_window: tk.Toplevel | None = None  # E5: single cheat-sheet
        # C2: playlists tab (only built when player.playlist_path is set)
        self._playlist_store = None                    # PlaylistStore | None
        self._pl_combo: tk.Widget | None = None
        self._pl_list: tk.Listbox | None = None
        self._pl_var: tk.StringVar | None = None
        self._pl_msg: tk.StringVar | None = None
        self._pl_url_var: tk.StringVar | None = None
        self._pl_tracks: list[dict] = []
        self._pl_drag_hint = tk.StringVar(value="")
        self._browse_results_ts = 0.0
        self._pl_drag_index: int | None = None
        self._pl_drag_last: int | None = None
        self._pl_drag_origin_y: int | None = None
        # Which music surface most recently received a user selection.  Keeping
        # this explicit prevents the generic Add action from silently choosing
        # a different list when several rows are selected at once.
        self._active_music_source: str | None = None
        self._add_button: tk.Button | None = None
        self._current_url: str | None = None
        self.theme_name = config.theme if config is not None else "midnight"

        # D: settings tab (StringVar per dotted key + a kind for coercion)
        self._set_vars: dict[tuple[str, str], tk.Variable] = {}
        self._set_kinds: dict[tuple[str, str], str] = {}
        self._set_dirty = False
        self._settings_loaded_values: dict[tuple[str, str], object] = {}
        self._set_msg: tk.StringVar | None = None
        self._cmd_vars: dict[str, tuple[tk.StringVar, tk.BooleanVar]] = {}
        self._set_backend_var: tk.StringVar | None = None
        self._set_mic_var: tk.StringVar | None = None
        self._set_mics: list[tuple[int, str]] = []
        self._pages: dict[str, tk.Widget] = {}
        self._nav_buttons: dict[str, tk.Widget] = {}
        self._active_page = "Playlists"
        self._setup_theme()
        self._build_ui()
        self.after(POLL_MS, self._poll_ui)
    def _setup_theme(self) -> None:
        self.theme_name = self.theme_name if self.theme_name in theme_names() else "midnight"
        palette = get_theme(self.theme_name)
        global BG, SURFACE, SURFACE_ALT, FG, FG_DIM, ACCENT, ACCENT_DIM, DANGER, HOVER
        BG = palette["background"]
        SURFACE = palette["surface"]
        SURFACE_ALT = palette["surface_alt"]
        HOVER = palette["hover"]
        FG = palette["text"]
        FG_DIM = palette["text_dim"]
        ACCENT = palette["accent"]
        ACCENT_DIM = palette["accent_dim"]
        DANGER = palette["danger"]
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Modern.Horizontal.TScale", background=SURFACE,
                        troughcolor=SURFACE_ALT, bordercolor=SURFACE,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Modern.Vertical.TScrollbar", background=SURFACE_ALT,
                        troughcolor=BG, bordercolor=BG, arrowcolor=FG_DIM)
        style.configure(
            "Modern.TCombobox", fieldbackground=SURFACE_ALT, background=SURFACE_ALT,
            foreground=FG, arrowcolor=FG, bordercolor=SURFACE_ALT,
            lightcolor=SURFACE_ALT, darkcolor=SURFACE_ALT, padding=(8, 6),
        )
        style.map(
            "Modern.TCombobox",
            fieldbackground=[("readonly", SURFACE_ALT)],
            foreground=[("readonly", FG)], selectbackground=[("readonly", SURFACE_ALT)],
            selectforeground=[("readonly", FG)],
        )
        style.map("Modern.Horizontal.TScale", background=[("active", ACCENT)])

    def _show_page(self, name: str) -> None:
        page = self._pages.get(name)
        if page is None:
            return
        for other in self._pages.values():
            other.pack_forget()
        page.pack(fill="both", expand=True)
        self._active_page = name
        for key, button in self._nav_buttons.items():
            active = key == name
            button.configure(
                bg=ACCENT if active else SURFACE,
                fg="#07130b" if active else FG_DIM,
                activebackground=ACCENT_DIM if active else HOVER,
                activeforeground="#07130b" if active else FG,
            )

    # -- construction ----------------------------------------------------


    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.overrideredirect(True)
        self.attributes(
            "-topmost",
            self.ui_config.always_on_top if self.ui_config is not None else True,
        )
        self.configure(bg=BG)
        self._born = time.monotonic()
        self.geometry(f"{self._win_width}x{self._win_height}{WINDOW_POS}")
        self.show()
        self.bind_all("<Control-Escape>", lambda _event: self._close_app())
        self.bind_all("<Alt-F4>", lambda _event: self._close_app())
        self._drag_xy: tuple[int, int] | None = None
        self._drag_moved = False
        if self.expanded:
            self._build_expanded_ui()
        else:
            self._build_compact_ui()

    def _build_voice_header(self) -> None:
        """Expanded top status: transcript, mic gain, and five voice previews."""
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(10, 4))
        header.bind("<ButtonPress-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)
        header.bind("<ButtonRelease-1>", self._drag_end)
        row = tk.Frame(header, bg=BG)
        row.pack(fill="x")
        for widget in (row,):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self._close_button = tk.Button(
            row, text="X", width=3, relief="flat", bd=0,
            bg=BG, fg=FG_DIM, activebackground=DANGER, activeforeground=FG,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
            command=self._close_app,
        )
        self._close_button.pack(side="right", padx=(8, 0))
        self._dot = tk.Canvas(row, width=14, height=14, bg=BG, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 10))
        for widget in (header, self._dot):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self._dot_id = self._dot.create_oval(2, 2, 12, 12, fill=DOT_OFF, outline="")
        self._heard_var = tk.StringVar(value="Say: youtube play <music>")
        self._heard_label = tk.Label(
            row, textvariable=self._heard_var, fg=FG, bg=BG, anchor="w",
            font=("Segoe UI", 12, "bold"), wraplength=self._win_width - 90,
        )
        self._heard_label.pack(side="left", fill="x", expand=True)
        for widget in (self._heard_label,):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self._meter = tk.Canvas(header, height=6, bg=BG, highlightthickness=0)
        self._meter.pack(fill="x", pady=(6, 3))
        self._meter.create_rectangle(
            0, 0, self._win_width - 36, 6, fill=SURFACE_ALT, width=0
        )
        self._meter_id = self._meter.create_rectangle(0, 0, 0, 6, fill=ACCENT, width=0)
        self._action_var = tk.StringVar(value="Listening")
        tk.Label(header, textvariable=self._action_var, fg=FG_DIM, bg=BG,
                 anchor="w", font=("Segoe UI", 8)).pack(fill="x")
        preview_box = tk.Frame(self, bg=BG)
        preview_box.pack(fill="x", padx=18, pady=(3, 6))
        self._voice_preview_scroll = ttk.Scrollbar(
            preview_box, orient="vertical", style="Modern.Vertical.TScrollbar"
        )
        self._voice_preview_list = tk.Listbox(
            preview_box, bg=SURFACE, fg=FG, bd=0, highlightthickness=0,
            height=POOL_ROWS, selectmode="browse", activestyle="none",
            selectbackground=ACCENT_DIM, selectforeground=FG,
            yscrollcommand=self._voice_preview_scroll.set,
            font=("Segoe UI", 8),
        )
        self._voice_preview_scroll.config(command=self._voice_preview_list.yview)
        self._voice_preview_scroll.pack(side="right", fill="y")
        self._voice_preview_list.pack(side="left", fill="both", expand=True)
        self._voice_preview_list.bind("<Double-Button-1>", self._voice_preview_double_click)
        self._voice_preview_list.bind("<Return>", lambda _e: self._voice_preview_double_click(None))
        self._voice_preview_list.bind("<space>", lambda _e: self._toggle_playback())
        self._voice_preview_list.bind(
            "<Button-1>", lambda _e: setattr(self, "_active_music_source", "preview")
        )
        self._voice_preview_list.bind(
            "<<ListboxSelect>>", lambda _e: setattr(self, "_active_music_source", "preview")
        )
        for widget in (self._voice_preview_list,):
            widget.configure(takefocus=1, highlightthickness=1, highlightbackground=SURFACE_ALT,
                              highlightcolor=ACCENT)
        # Keep the old attribute for compact-mode compatibility; the expanded
        # preview surface is a real listbox and owns its selection.
        self._pool_labels: list[tk.Widget] = []

    def _build_compact_ui(self) -> None:
        """Today's 400x214 single panel, byte for byte the old _build_ui."""
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=14, pady=(10, 4))

        self._dot = tk.Canvas(header, width=16, height=16, bg=BG, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 10))
        self._dot_id = self._dot.create_oval(2, 2, 14, 14, fill=DOT_OFF, outline="")

        self._heard_var = tk.StringVar()
        self._heard_label = tk.Label(
            header,
            textvariable=self._heard_var,
            fg=FG,
            bg=BG,
            justify="left",
            anchor="nw",
            font=tkfont.Font(family="Segoe UI", size=15),
            wraplength=self._win_width - 60,
        )
        self._heard_label.pack(side="left", fill="x", expand=True)

        # live microphone meter: width follows the RMS level, colour flips
        # to "busy" while you speak (basic "is audio arriving?" feedback)
        self._meter = tk.Canvas(self, height=4, bg=BG, highlightthickness=0)
        self._meter.pack(fill="x", padx=14, pady=(0, 2))
        # dim full-width track first (always visible), fill on top of it
        self._meter.create_rectangle(
            0, 0, self._win_width - 28, 4, fill=SEPARATOR, width=0
        )
        self._meter_id = self._meter.create_rectangle(
            0, 0, 0, 4, fill=DOT_IDLE, width=0
        )

        self._action_var = tk.StringVar()
        self._action_label = tk.Label(
            self,
            textvariable=self._action_var,
            fg=FG_DIM,
            bg=BG,
            justify="left",
            anchor="w",
            font=tkfont.Font(family="Segoe UI", size=9),
            wraplength=self._win_width - 28,
        )
        self._action_label.pack(fill="x", padx=14, pady=(0, 6))

        tk.Frame(self, bg=SEPARATOR, height=1).pack(fill="x", padx=14)

        pool_font = tkfont.Font(family="Segoe UI", size=9)
        self._pool_labels: list[tk.Label] = []
        for row in range(POOL_ROWS):
            label = tk.Label(
                self, text="", fg=FG_DIM, bg=BG, anchor="w",
                cursor="hand2", font=pool_font,
            )
            label.pack(fill="x", padx=14, pady=1)
            # Release (not press): a press that turns into a drag must not
            # change tracks; _on_pool_click checks the _drag_moved flag.
            label.bind("<ButtonRelease-1>", lambda _event, index=row: self._on_pool_click(index))
            self._pool_labels.append(label)

    def _build_expanded_ui(self) -> None:
        """Modern shell: sidebar pages with a persistent player bar."""
        self._build_voice_header()
        self._build_persistent_player_bar()
        shell = tk.Frame(self, bg=BG)
        shell.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        sidebar = tk.Frame(shell, bg="#0c0e11", width=176)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="VOICEYT", bg="#0c0e11", fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=18, pady=(10, 18))
        content = tk.Frame(shell, bg=BG)
        content.pack(side="left", fill="both", expand=True, padx=(18, 0))

        pages = []
        if self._playlist_path() is not None:
            playlists = tk.Frame(content, bg=BG)
            self._pages["Playlists"] = playlists
            self._build_playlists_tab(playlists)
            self._build_browse_area(playlists)
            pages.append(("Playlists", playlists))
        settings = tk.Frame(content, bg=BG)
        self._pages["Settings"] = settings
        self._build_settings_tab(settings)
        pages.append(("Settings", settings))

        for name, _page in pages:
            button = tk.Button(
                sidebar, text=name, anchor="w", padx=18, pady=8,
                bg=SURFACE, fg=FG_DIM, activebackground=ACCENT,
                activeforeground="#07130b", relief="flat", bd=0,
                font=("Segoe UI", 10), cursor="hand2",
                command=lambda n=name: self._show_page(n),
            )
            button.pack(fill="x", pady=2)
            self._nav_buttons[name] = button
        self._show_page("Playlists" if "Playlists" in self._pages else "Settings")
        self._status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status_var, bg=SURFACE, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8), padx=12).pack(
                     fill="x", padx=12, pady=(4, 8), ipady=4)
    def _build_persistent_player_bar(self) -> None:
        """Global transport with a real 0-100% song-position slider."""
        bar = tk.Frame(self, bg=SURFACE, padx=12, pady=6)
        bar.pack(fill="x", padx=12, pady=(4, 0), side="bottom")
        row = tk.Frame(bar, bg=SURFACE)
        row.pack(side="left")
        for text, action in (("⏮", "prev"), ("▶", "play_pause"),
                             ("⏭", "next"), ("■", "stop")):
            primary = action == "play_pause"
            button = tk.Button(
                row, text=text, width=3, bg=ACCENT if primary else SURFACE_ALT,
                fg="#07130b" if primary else FG, activebackground=ACCENT_DIM,
                relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 10),
                command=lambda a=action: _run_in_background(
                    "transport", _player_transport,
                    self.ui_state.player, self.ui_state, a,
                ),
            )
            button.pack(side="left", padx=2)
            if action == "play_pause":
                self._play_pause_button = button

        seek = tk.Frame(bar, bg=SURFACE)
        seek.pack(side="left", fill="x", expand=True, padx=(18, 0))
        info = tk.Frame(seek, bg=SURFACE)
        info.pack(fill="x", pady=(0, 2))
        self._now_var = tk.StringVar(value="Nothing playing")
        self._queue_var = tk.StringVar(value="Queue empty")
        tk.Label(info, textvariable=self._now_var, bg=SURFACE, fg=FG,
                 anchor="w", font=("Segoe UI", 9, "bold")).pack(
                     side="left", fill="x", expand=True)
        tk.Label(info, textvariable=self._queue_var, bg=SURFACE, fg=FG_DIM,
                 anchor="e", font=("Segoe UI", 8)).pack(side="right")
        self._seek_label = tk.StringVar(value="0:00 / --:--")
        tk.Label(seek, textvariable=self._seek_label, bg=SURFACE, fg=FG_DIM,
                 width=13, font=("Segoe UI", 8)).pack(side="left")
        self._seek_var = tk.DoubleVar(value=0.0)
        self._seek_scale = tk.Canvas(
            seek, height=18, bg=SURFACE, highlightthickness=0,
            cursor="hand2", takefocus=1,
        )
        self._seek_scale.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._seek_track_id = self._seek_scale.create_rectangle(
            0, 8, 100, 11, fill=SURFACE_ALT, outline=""
        )
        self._seek_fill_id = self._seek_scale.create_rectangle(
            0, 8, 0, 11, fill=ACCENT, outline=""
        )
        self._seek_handle_id = self._seek_scale.create_oval(
            93, 4, 107, 18, fill=ACCENT, outline=""
        )
        self._seek_scale.bind("<Configure>", lambda _e: self._draw_seek())
        self._seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self._seek_scale.bind("<B1-Motion>", self._on_seek_motion)
        self._seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)
        self._seek_scale.bind("<Return>", lambda _e: self._toggle_playback())
        self._seek_scale.bind("<space>", lambda _e: self._toggle_playback())

        volume = tk.Frame(bar, bg=SURFACE)
        volume.pack(side="right")
        step = getattr(self.ui_state.player, "volume_step", 10)
        tk.Button(volume, text="−", width=3, bg=SURFACE_ALT, fg=FG,
                  activebackground=ACCENT_DIM, relief="flat", bd=0,
                  command=lambda: self._ui_step_volume(-step)).pack(side="left")
        tk.Button(volume, text="+", width=3, bg=SURFACE_ALT, fg=FG,
                  activebackground=ACCENT_DIM, relief="flat", bd=0,
                  command=lambda: self._ui_step_volume(step)).pack(side="left", padx=(2, 0))
        self._vol_label = tk.StringVar(value="")
        tk.Label(volume, textvariable=self._vol_label, bg=SURFACE, fg=FG_DIM,
                 width=5, font=("Segoe UI", 8)).pack(side="left", padx=6)
        tk.Button(volume, text="Mute", width=6, bg=SURFACE_ALT, fg=FG,
                  activebackground=ACCENT_DIM, relief="flat", bd=0,
                  command=self._on_mute_toggle).pack(side="left", padx=(4, 0))

    # -- C2: playlists tab -------------------------------------------------

    def _playlist_path(self):
        """C4: the store only exists when player.playlist_path is configured."""
        cfg = self.app_config
        if cfg is None:
            return None
        value = getattr(cfg.player, "playlist_path", None)
        if value is None:
            return None
        try:
            return Path(value)
        except TypeError:      # pragma: no cover - config validates this
            return None

    def _store(self):
        """Lazy PlaylistStore; only built when the tab is actually shown."""
        if self._playlist_store is None:
            path = self._playlist_path()
            if path is None:
                return None
            from .playlists import PlaylistStore
            self._playlist_store = PlaylistStore(path)
        return self._playlist_store

    def _build_playlists_tab(self, parent: tk.Widget) -> None:
        """C2: pick a playlist, then play/reorder/add/remove its tracks."""
        pick = tk.Frame(parent, bg=BG)
        pick.pack(fill="x", padx=6, pady=(8, 2))
        self._pl_var = tk.StringVar(value="")
        self._pl_combo = ttk.Combobox(pick, textvariable=self._pl_var,
                                      values=[], width=30, state="readonly",
                                      style="Modern.TCombobox")
        self._pl_combo.pack(side="left")
        self._pl_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self._pl_reload_tracks())
        for text, command in (("New", self._pl_new), ("Rename", self._pl_rename),
                              ("Delete", self._pl_delete)):
            tk.Button(pick, text=text, bg=SURFACE_ALT, fg=FG,
                      activebackground=ACCENT_DIM, relief="flat", bd=0,
                      command=command).pack(side="left", padx=(6, 0))

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        track_area = tk.Frame(body, bg=BG)
        track_area.pack(side="left", fill="both", expand=True)
        self._pl_list = tk.Listbox(track_area, bg=SURFACE, fg=FG, bd=0,
                                   highlightthickness=0,
                                   selectbackground=ACCENT_DIM,
                                   selectforeground=FG,
                                   font=("Segoe UI", 9), activestyle="none")
        self._pl_drag_hint_label = tk.Label(
            body, textvariable=self._pl_drag_hint, bg=BG, fg=ACCENT,
            anchor="w", font=("Segoe UI", 8),
        )
        self._pl_drag_hint_label.pack(side="top", fill="x", pady=(0, 2))
        self._pl_scroll = ttk.Scrollbar(track_area, orient="vertical", command=self._pl_list.yview)
        self._pl_list.configure(yscrollcommand=self._pl_scroll.set)
        self._pl_scroll.pack(side="right", fill="y", padx=(4, 0))
        self._pl_list.pack(side="left", fill="both", expand=True)
        self._pl_list.configure(takefocus=1, highlightthickness=1,
                                highlightbackground=SURFACE_ALT, highlightcolor=ACCENT)
        self._pl_list.bind(
            "<Button-1>", lambda _e: setattr(self, "_active_music_source", "playlist")
        )
        self._pl_list.bind("<<ListboxSelect>>",
                           lambda _e: setattr(self, "_active_music_source", "playlist"))
        self._pl_list.bind("<Double-Button-1>", lambda _e: self._pl_play())
        self._pl_list.bind("<Return>", lambda _e: self._pl_play())
        self._pl_list.bind("<space>", lambda _e: self._toggle_playback())
        self._pl_list.bind("<Delete>", lambda _e: self._pl_remove())
        self._pl_list.bind("<Control-a>", lambda _e: self._add_selected_music())
        self._pl_list.bind("<Control-Up>", lambda _e: self._pl_move_key(-1))
        self._pl_list.bind("<Control-Down>", lambda _e: self._pl_move_key(1))
        self._pl_list.bind("<ButtonPress-1>", self._pl_drag_start)
        self._pl_list.bind("<B1-Motion>", self._pl_drag_motion)
        self._pl_list.bind("<ButtonRelease-1>", self._pl_drag_end)

        side = tk.Frame(body, bg=BG)
        side.pack(side="right", fill="y", padx=(6, 0))
        for text, command in (("Play", self._pl_play),
                              ("Add", self._add_selected_music),
                              ("Remove", self._pl_remove)):
            button = tk.Button(side, text=text, width=11, bg=SURFACE_ALT, fg=FG,
                       activebackground=ACCENT_DIM, relief="flat", bd=0,
                       command=command)
            button.pack(fill="x", pady=(0, 4))
            if text == "Add":
                self._add_button = button

        self._pl_url_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._pl_url_var, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(
                     fill="x", padx=6, pady=(4, 8))
        self._pl_msg = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._pl_msg, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(fill="x", padx=6)
        self._pl_refresh()

    def _sync_playlist_highlight(self) -> None:
        """Show the active saved track even when Playback lives on Player."""
        if self._pl_list is None or self.ui_state.player is None:
            return
        try:
            playback = self.ui_state.playback
            current = playback.current_url() if playback is not None else self.ui_state.player.current_url()
        except Exception:
            return
        for index, track in enumerate(self._pl_tracks):
            if current and track.get("url") == current:
                self._pl_list.selection_clear(0, "end")
                self._pl_list.selection_set(index)
                self._pl_list.activate(index)
                self._pl_list.see(index)
                return

    def _build_browse_area(self, parent: tk.Widget) -> None:
        """Browse search lists music; playback starts only on explicit selection."""
        tk.Frame(parent, bg=SURFACE, height=1).pack(fill="x", padx=6, pady=(8, 4))
        tk.Label(parent, text="Browse music", bg=BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=6)
        search = tk.Frame(parent, bg=BG)
        search.pack(fill="x", padx=6, pady=(4, 2))
        self._search_var = tk.StringVar(value=self.ui_state.snapshot().get("browse_query", ""))
        entry = tk.Entry(search, textvariable=self._search_var, bg=SURFACE_ALT,
                         fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=0, font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.bind("<Return>", lambda _event: self._submit_browse())
        tk.Button(search, text="Search", width=9, bg=ACCENT, fg="#07130b",
                  activebackground=ACCENT_DIM, relief="flat", bd=0,
                  command=self._submit_browse).pack(side="left", padx=(8, 0), ipady=4)
        self._browse_msg = tk.StringVar(value="Search does not start playback")
        tk.Label(parent, textvariable=self._browse_msg, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(fill="x", padx=6)
        browse_box = tk.Frame(parent, bg=BG)
        browse_box.pack(fill="both", expand=True, padx=6, pady=(3, 6))
        self._browse_scroll = ttk.Scrollbar(browse_box, orient="vertical")
        self._browse_list = tk.Listbox(browse_box, bg=SURFACE, fg=FG, bd=0,
                                       highlightthickness=0, height=10,
                                       selectbackground=ACCENT_DIM,
                                       selectforeground=FG, activestyle="none",
                                       yscrollcommand=self._browse_scroll.set)
        self._browse_scroll.pack(side="right", fill="y", padx=(4, 0))
        self._browse_list.pack(side="left", fill="both", expand=True, padx=6, pady=(3, 6))
        self._browse_list.configure(takefocus=1, highlightthickness=1,
                                     highlightbackground=SURFACE_ALT, highlightcolor=ACCENT)
        self._browse_scroll.config(command=self._browse_list.yview)
        self._browse_list.bind("<<ListboxSelect>>",
                               lambda _e: setattr(self, "_active_music_source", "browse"))
        self._browse_list.bind("<Double-Button-1>", self._browse_double_click)
        self._browse_list.bind("<Return>", lambda _e: self._browse_play())
        self._browse_list.bind("<space>", lambda _e: self._toggle_playback())
        self._browse_list.bind(
            "<Button-1>", lambda _e: setattr(self, "_active_music_source", "browse")
        )

    def _voice_preview_double_click(self, event) -> str:
        if self._voice_preview_list is None:
            return "break"
        if event is not None:
            index = self._voice_preview_list.nearest(event.y)
        else:
            selected = self._voice_preview_list.curselection()
            index = int(selected[0]) if selected else -1
        snap = self.ui_state.snapshot()
        titles = snap.get("voice_previews", [])
        urls = snap.get("voice_preview_urls", [])
        if not 0 <= index < len(titles):
            return "break"
        self._voice_preview_list.selection_clear(0, "end")
        self._voice_preview_list.selection_set(index)
        self._active_music_source = "preview"
        title = titles[index]
        url = urls[index] if index < len(urls) else ""
        if url and self.on_play_playlist:
            track = {"title": title, "url": url}
            _run_in_background("voice-preview-play", self.on_play_playlist, [track], 0)
        elif self.on_play_query:
            # Older saved UI state may contain titles without URLs. Re-run the
            # exact voice query so double-click remains useful instead of being
            # a silent no-op.
            _run_in_background("voice-preview-search", self.on_play_query, title)
        if self._browse_msg is not None:
            self._browse_msg.set(f"Playing {title}")
        return "break"

    def _submit_browse(self) -> None:
        text = self._search_var.get().strip() if self._search_var is not None else ""
        if not text or self._on_browse_query is None:
            return
        self._browse_msg.set(f"Searching for “{text}”…")
        _run_in_background("browse", self._on_browse_query, text)

    def _browse_double_click(self, event) -> str:
        if self._browse_list is not None and event is not None:
            index = self._browse_list.nearest(event.y)
            if 0 <= index < len(self._browse_results):
                self._browse_list.selection_clear(0, "end")
                self._browse_list.selection_set(index)
        self._browse_play()
        return "break"

    def _browse_add_to_playlist(self) -> None:
        selection = self._browse_list.curselection() if self._browse_list else ()
        store = self._store()
        name = self._pl_selected_name()
        if not selection or store is None or name is None:
            self._browse_msg.set("Select a song and a playlist first")
            return
        track = dict(self._browse_results[int(selection[0])])
        try:
            store.add(name, str(track.get("title") or "Unknown"), str(track.get("url") or ""))
        except ValueError as exc:
            self._browse_msg.set(f"could not add: {exc}")
            return
        self._pl_reload_tracks()
        self._browse_msg.set(f"Added to {name}")

    def _browse_play(self) -> None:
        selection = self._browse_list.curselection() if self._browse_list else ()
        if not selection or self.on_play_playlist is None:
            return
        self._active_music_source = "browse"
        track = dict(self._browse_results[int(selection[0])])
        _run_in_background("browse-play", self.on_play_playlist, [track], 0)
        self._browse_msg.set(f"Playing {track.get('title', 'track')}")

    def _refresh_browse(self, results: list[dict]) -> None:
        if self._browse_list is None:
            return
        self._browse_results = [dict(result) for result in results]
        self._browse_list.delete(0, "end")
        for result in self._browse_results:
            title = _truncate(str(result.get("title") or "Unknown"), 70)
            duration = result.get("duration")
            suffix = f"  ·  {_format_duration(duration)}" if duration else ""
            self._browse_list.insert("end", f"{title}{suffix}")
        if self._browse_msg is not None:
            count = len(results)
            self._browse_msg.set(
                f"{count} result{'s' if count != 1 else ''} · double-click to play"
                if count else "No music found · try another search"
            )

    def _pl_note(self, text: str, error: bool = False) -> None:
        if self._pl_msg is not None:
            self._pl_msg.set(text)
        if self._pl_url_var is not None:
            self._pl_url_var.set("")
        if error:
            LOGGER.warning("playlists: %s", text)

    def _pl_selected_name(self) -> str | None:
        if self._pl_combo is None:
            return None
        index = self._pl_combo.current()
        values = list(self._pl_combo.cget("values"))
        if 0 <= index < len(values):
            return values[index]
        return None

    def _pl_refresh(self, select: str | None = None) -> None:
        """Repaint the name list, then the tracks of the chosen playlist."""
        store = self._store()
        if store is None or self._pl_combo is None:
            return
        names = store.names()
        self._pl_combo.configure(values=names)
        if select and select in names:
            self._pl_combo.set(select)
        elif self._pl_var is not None and self._pl_var.get() not in names:
            self._pl_combo.set(names[0] if names else "")
        self._pl_reload_tracks()

    def _pl_reload_tracks(self) -> None:
        if self._pl_list is None:
            return
        self._pl_list.delete(0, "end")
        store = self._store()
        name = self._pl_selected_name()
        self._pl_tracks = store.tracks(name) if store and name else []
        for index, track in enumerate(self._pl_tracks, start=1):
            self._pl_list.insert(
                "end", f"{index}.  {_truncate(track.get('title', ''), POOL_MAX_CHARS)}"
            )
        if self._pl_var is not None:
            count = len(self._pl_tracks)
            unit = "track" if count == 1 else "tracks"
            empty = " · no songs yet — browse or say youtube add" if count == 0 else ""
            self._pl_note(f"{name or 'No playlist'} · {count} {unit}{empty}")

    def _pl_selected_index(self) -> int | None:
        if self._pl_list is None:
            return None
        selection = self._pl_list.curselection()
        return int(selection[0]) if selection else None

    def _pl_ask(self, title: str, prompt: str, initial: str = "") -> str | None:
        from tkinter import simpledialog
        value = simpledialog.askstring(title, prompt, initialvalue=initial,
                                       parent=self)
        return value.strip() if isinstance(value, str) else None

    def _pl_new(self) -> None:
        store = self._store()
        name = self._pl_ask("New playlist", "name:")
        if store is None or not name:
            return
        try:
            store.create(name)
        except ValueError as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_refresh(select=name)
        self._pl_note(f"created {name!r}")

    def _pl_rename(self) -> None:
        store = self._store()
        old = self._pl_selected_name()
        if store is None or old is None:
            return
        new = self._pl_ask("Rename playlist", "new name:", initial=old)
        if not new or new == old:
            return
        try:
            store.rename(old, new)
        except ValueError as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_refresh(select=new)
        self._pl_note(f"renamed {old!r} to {new!r}")

    def _pl_delete(self) -> None:
        from tkinter import messagebox
        store = self._store()
        name = self._pl_selected_name()
        if store is None or name is None:
            return
        if not messagebox.askyesno("Delete playlist", f"delete {name!r}?",
                                   parent=self):
            return
        try:
            store.delete(name)
        except ValueError as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_refresh()
        self._pl_note(f"deleted {name!r}")

    def _pl_play(self) -> None:
        """Play a saved playlist as the real shared queue, off the Tk thread."""
        index = self._pl_selected_index()
        if index is None or not self._pl_tracks:
            return
        if self.on_play_playlist is None:
            self._pl_note("playlist playback is unavailable", error=True)
            return
        tracks = [dict(track) for track in self._pl_tracks]
        title = tracks[index].get("title") or "track"
        _run_in_background("playlist", self.on_play_playlist, tracks, index)
        self._pl_note(f"loading {title!r}")

    def _add_selected_music(self) -> None:
        """Add the selected row from the active music surface to the playlist."""
        store = self._store()
        name = self._pl_selected_name()
        if store is None or name is None:
            self._pl_note("select a playlist first", error=True)
            return

        source, index = self._active_music_selection()
        # Clicking a global Add button can move focus without changing the
        # selection. If the explicit source was lost, recover the selected row
        # from the three visible music surfaces instead of silently doing
        # nothing.
        if source is None or index is None:
            if self._voice_preview_list is not None and self._voice_preview_list.curselection():
                source, index = "preview", int(self._voice_preview_list.curselection()[0])
            elif self._browse_list is not None and self._browse_list.curselection():
                source, index = "browse", int(self._browse_list.curselection()[0])
            elif self._pl_list is not None and self._pl_list.curselection():
                source, index = "playlist", int(self._pl_list.curselection()[0])
        if source is None or index is None:
            self._pl_note("select a song first", error=True)
            return
        if source == "browse":
            track = dict(self._browse_results[index])
        elif source == "preview":
            previews = self.ui_state.snapshot()
            title = previews.get("voice_previews", [])[index]
            urls = previews.get("voice_preview_urls", [])
            track = {"title": title, "url": urls[index] if index < len(urls) else ""}
        else:
            track = dict(self._pl_tracks[index])
        url = str(track.get("url") or "")
        if not url:
            self._pl_note("voice previews do not contain a playable URL; use Browse music", error=True)
            return
        try:
            store.add(name, str(track.get("title") or url), url)
        except ValueError as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_reload_tracks()
        self._pl_note(f"added {track.get('title') or url!r} to {name!r}")

    def _sync_current_rows(self) -> None:
        """Mark the real current track without changing user selection."""
        player = self.ui_state.player
        playback = self.ui_state.snapshot().get("playback")
        if playback is not None:
            try:
                current_url = playback.current_url
            except AttributeError:
                current_url = None
        else:
            current_url = None
            try:
                current_url = player.current_url() if player else None
            except Exception:
                pass
        self._current_url = current_url
        if self._voice_preview_list is not None:
            snap = self.ui_state.snapshot()
            urls = snap.get("voice_preview_urls", [])
            for index, url in enumerate(urls[:self._voice_preview_list.size()]):
                marker = "▶ " if current_url and url == current_url else ""
                self._voice_preview_list.itemconfig(
                    index, fg=ACCENT if marker else FG
                )
        if self._browse_list is not None:
            for index, track in enumerate(self._browse_results[:self._browse_list.size()]):
                marker = "▶ " if current_url and track.get("url") == current_url else ""
                self._browse_list.itemconfig(index, fg=ACCENT if marker else FG)
        if self._pl_list is not None:
            for index, track in enumerate(self._pl_tracks[:self._pl_list.size()]):
                marker = "▶ " if current_url and track.get("url") == current_url else ""
                self._pl_list.itemconfig(index, fg=ACCENT if marker else FG)

    def _active_music_selection(self) -> tuple[str | None, int | None]:
        """Return the row from the list the user most recently selected."""
        source = self._active_music_source
        if self._add_button is not None:
            self._add_button.configure(text={
                "browse": "Add Browse",
                "preview": "Add Voice",
                "playlist": "Add Track",
            }.get(source, "Add"))
        if source == "browse" and self._browse_list is not None:
            selected = self._browse_list.curselection()
            return ("browse", int(selected[0])) if selected else (None, None)
        if source == "preview" and self._voice_preview_list is not None:
            selected = self._voice_preview_list.curselection()
            return ("preview", int(selected[0])) if selected else (None, None)
        if source == "playlist" and self._pl_list is not None:
            selected = self._pl_list.curselection()
            return ("playlist", int(selected[0])) if selected else (None, None)
        return None, None

    def _pl_add_current(self) -> None:
        store = self._store()
        name = self._pl_selected_name()
        player = self.ui_state.player
        playback = self.ui_state.playback
        if store is None or name is None or (player is None and playback is None):
            self._pl_note("nothing is playing", error=True)
            return
        title = playback.current_title() if playback is not None else player.current_title()
        try:
            url = playback.current_url() if playback is not None else player.current_url()
        except Exception:
            url = None
        if not isinstance(url, str) or not url:
            self._pl_note("nothing is playing", error=True)
            return
        try:
            store.add(name, title or url, url)
        except ValueError as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_reload_tracks()
        self._pl_note(f"added {title or url!r} to {name!r}")

    def _pl_remove(self) -> None:
        store = self._store()
        name = self._pl_selected_name()
        index = self._pl_selected_index()
        if store is None or name is None or index is None:
            return
        try:
            store.remove(name, index)
        except (ValueError, IndexError) as exc:
            self._pl_note(str(exc), error=True)
            return
        self._pl_reload_tracks()
        self._pl_note("removed one track")

    def _pl_move_key(self, delta: int) -> str:
        index = self._pl_selected_index()
        target = None if index is None else index + delta
        if index is None or target is None or not 0 <= target < len(self._pl_tracks):
            return "break"
        store = self._store()
        name = self._pl_selected_name()
        if store is None or name is None:
            return "break"
        try:
            store.reorder(name, index, target)
        except (ValueError, IndexError):
            return "break"
        self._pl_reload_tracks()
        self._pl_list.selection_set(target)
        return "break"

    def _pl_drag_start(self, event) -> None:
        self._pl_drag_origin_y = int(event.y)
        self._pl_drag_index = self._pl_index_at(event.y)
        self._active_music_source = "playlist"
        if self._pl_drag_index is not None:
            self._pl_drag_last = self._pl_drag_index
            self._pl_list.selection_clear(0, "end")
            self._pl_list.selection_set(self._pl_drag_index)
            self._pl_list.activate(self._pl_drag_index)
            self._pl_drag_hint.set(f"Moving track {self._pl_drag_index + 1}")

    def _pl_drag_motion(self, event) -> None:
        if self._pl_drag_index is None or self._pl_drag_origin_y is None:
            return
        if abs(int(event.y) - self._pl_drag_origin_y) < 5:
            return
        index = self._pl_index_at(event.y)
        if index is None or index == self._pl_drag_last:
            return
        self._pl_drag_last = index
        self._pl_list.selection_clear(0, "end")
        self._pl_list.selection_set(index)
        self._pl_list.activate(index)
        self._pl_list.see(index)
        self._pl_drag_hint.set(f"Drop at position {index + 1}")

    def _pl_drag_end(self, _event) -> None:
        source = self._pl_drag_index
        target = self._pl_drag_last
        moved = self._pl_drag_origin_y is not None and self._pl_drag_last is not None \
            and self._pl_drag_last != source
        self._pl_drag_index = None
        self._pl_drag_last = None
        self._pl_drag_origin_y = None
        self._pl_drag_hint.set("")
        if not moved or source is None or target is None or source == target:
            return
        store = self._store()
        name = self._pl_selected_name()
        if store is None or name is None:
            return
        try:
            store.reorder(name, source, target)
        except (ValueError, IndexError):
            return
        self._pl_reload_tracks()
        if self._pl_list is not None:
            self._pl_list.selection_set(target)

    def _pl_index_at(self, y: int) -> int | None:
        if self._pl_list is None or not self._pl_tracks:
            return None
        index = self._pl_list.nearest(y)
        return index if 0 <= index < len(self._pl_tracks) else None

    # -- D: settings tab ---------------------------------------------------

    def _build_settings_tab(self, parent: tk.Widget) -> None:
        """Settings uses themed cards while preserving the living Config form."""
        cfg = self.app_config
        if cfg is None:
            tk.Label(parent, text="no config file is attached to this run",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(pady=20)
            return
        # The form is taller than the tab, so it scrolls (classic recipe:
        # Canvas + inner Frame; no new dependency).
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        inner.bind("<Control-s>", lambda _e: self._settings_save())
        inner.bind("<Escape>", lambda _e: self._settings_reload())
        inner.bind("<F6>", lambda _e: self._settings_focus_next())

        # E3: backend picker.  Restart to take effect (a loaded model cannot
        # be swapped under a running capture loop).
        self._set_backend_var = tk.StringVar(value=str(cfg.asr.backend))
        self._combo_row(inner, "ASR backend", self._set_backend_var,
                        list(_backend_names()), restart=True)

        # D1: microphone.  "default" means "let sounddevice choose".
        self._set_mics = list_mics()
        choices = ["default"] + [f"{i}: {n}" for i, n in self._set_mics]
        self._set_mic_var = tk.StringVar(value=_mic_choice(cfg, self._set_mics))
        self._combo_row(inner, "microphone", self._set_mic_var, choices)

        # D1: every other editable number/flag, grouped by its YAML section.
        self._section(inner, "audio")
        self._ro_row(inner, "sample_rate (fixed)", cfg.audio.sample_rate)

        self._section(inner, "vad")
        self._row(inner, "vad", "threshold", cfg.vad.threshold, "float")
        self._row(inner, "vad", "min_speech_ms", cfg.vad.min_speech_ms, "int")
        self._row(inner, "vad", "min_silence_ms", cfg.vad.min_silence_ms, "int")
        self._row(inner, "vad", "max_utterance_s", cfg.vad.max_utterance_s, "float")

        self._section(inner, "trigger")
        self._row(inner, "trigger", "words", ", ".join(cfg.trigger.words), "list")
        self._row(inner, "trigger", "trigger_window_ms",
                  cfg.trigger.trigger_window_ms, "int")
        self._row(inner, "trigger", "silence_end_ms", cfg.trigger.silence_end_ms, "int")
        self._row(inner, "trigger", "query_max_ms", cfg.trigger.query_max_ms, "int")

        self._section(inner, "player")
        self._row(inner, "player", "volume_min", cfg.player.volume_min, "int")
        self._row(inner, "player", "volume_max", cfg.player.volume_max, "int")
        self._row(inner, "player", "volume_step", cfg.player.volume_step, "int")

        self._section(inner, "search")
        self._row(inner, "search", "results", cfg.search.results, "int")
        self._row(inner, "search", "browse_results", cfg.search.browse_results, "int")
        self._row(inner, "search", "voice_preview_results", cfg.search.voice_preview_results, "int")
        self._row(inner, "search", "cookies_from_browser",
                  cfg.search.cookies_from_browser or "", "str_or_null")

        self._section(inner, "behaviour")
        self._row(inner, "behaviour", "log_transcripts",
                  cfg.behaviour.log_transcripts, "bool")
        self._row(inner, "behaviour", "ignore_trigger_ms_after_play",
                  cfg.behaviour.ignore_trigger_ms_after_play, "int")
        self._row(inner, "behaviour", "require_same_utterance_while_playing",
                  cfg.behaviour.require_same_utterance_while_playing, "bool")
        self._row(inner, "behaviour", "ui", cfg.behaviour.ui, "bool")

        ui = cfg.ui_config
        self._section(inner, "ui")
        self._set_theme_var = tk.StringVar(value=ui.theme if ui else "midnight")
        self._combo_row(inner, "theme", self._set_theme_var, list(theme_names()))
        self._set_theme_var.trace_add("write", self._theme_selected)
        self._row(inner, "ui", "mode", ui.mode if ui else "compact", "str")
        self._row(inner, "ui", "overlay_width", ui.overlay_width if ui else 400, "int")
        self._row(inner, "ui", "overlay_height", ui.overlay_height if ui else 214, "int")
        self._row(inner, "ui", "always_on_top", ui.always_on_top if ui else True, "bool")
        self._row(inner, "ui", "show_playlist", ui.show_playlist if ui else True, "bool")
        self._row(inner, "ui", "hide_while_playing",
                  ui.hide_while_playing if ui else False, "bool")

        self._build_commands_editor(inner)   # D2
        self._build_settings_actions(inner)  # D3/D4/E4
        self._snapshot_settings_values()
        for var in list(self._set_vars.values()):
            var.trace_add("write", self._settings_changed)

    def _snapshot_settings_values(self) -> None:
        self._settings_loaded_values = {
            key: var.get() for key, var in self._set_vars.items()
        }
        self._set_dirty = False

    def _settings_changed(self, *_args) -> None:
        dirty = any(
            self._set_vars[key].get() != value
            for key, value in self._settings_loaded_values.items()
            if key in self._set_vars
        )
        self._set_dirty = dirty
        if self._set_msg is not None:
            self._settings_msg("Unsaved changes" if dirty else "No changes")

    def _settings_focus_next(self) -> str:
        focusables = []
        for parent in self.winfo_children():
            focusables.extend(parent.winfo_children())
        for widget in focusables:
            try:
                if widget.winfo_takesfocus() and widget.winfo_ismapped():
                    widget.focus_set()
                    return "break"
            except tk.TclError:
                pass
        return "break"

    def _theme_selected(self, *_args) -> None:
        name = self._set_theme_var.get()
        if name in theme_names() and name != self.theme_name:
            self.theme_name = name
            self._setup_theme()
            self.configure(bg=BG)
            self._apply_theme_to_tree(self)
            self._show_page(self._active_page)
            self._sync_current_rows()
            self._settings_msg(f"theme preview: {name}; Save to keep it")

    def _apply_theme_to_tree(self, widget) -> None:
        try:
            widget.configure(bg=BG)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            try:
                options = {}
                for key in ("bg", "activebackground"):
                    if key in child.keys():
                        options[key] = BG if key == "bg" else (
                            ACCENT_DIM if str(child.cget("bg")) == ACCENT else HOVER
                        )
                for key in ("fg", "activeforeground"):
                    if key in child.keys():
                        options[key] = FG if key == "fg" else (
                            "#07130b" if str(child.cget("bg")) == ACCENT else FG
                        )
                if options: child.configure(**options)
            except tk.TclError:
                pass
            self._apply_theme_to_tree(child)

    def _section(self, parent: tk.Widget, title: str) -> None:
        header = tk.Frame(parent, bg=SURFACE, padx=8, pady=5)
        header.pack(fill="x", padx=6, pady=(10, 2))
        tk.Label(header, text=title.upper(), bg=SURFACE, fg=FG,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

    def _row(self, parent: tk.Widget, section: str, key: str, value,
             kind: str) -> None:
        """One labelled field; registers its variable for collection on save."""
        line = tk.Frame(parent, bg=BG, padx=6, pady=1)
        line.pack(fill="x", padx=6)
        tk.Label(line, text=key.replace("_", " "), bg=BG, fg=FG_DIM, width=34, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        if kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=bool(value))
            tk.Checkbutton(line, variable=var, bg=BG, activebackground=BG,
                           selectcolor="#1a2027", highlightthickness=0,
                           ).pack(side="left")
        else:
            var = tk.StringVar(value=str(value))
            tk.Entry(line, textvariable=var, bg="#1a2027", fg=FG,
                     insertbackground=FG, relief="flat", highlightthickness=0,
                     font=("Segoe UI", 9), width=30).pack(
                         side="left", fill="x", expand=True, ipady=2)
        self._set_vars[(section, key)] = var
        self._set_kinds[(section, key)] = kind

    def _ro_row(self, parent: tk.Widget, label: str, value) -> None:
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x", padx=6, pady=1)
        tk.Label(line, text=label, bg=BG, fg=FG_DIM, width=34, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(line, text=str(value), bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")

    def _combo_row(self, parent: tk.Widget, label: str, var: tk.StringVar,
                   choices: list[str], restart: bool = False) -> None:
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x", padx=6, pady=(2 if restart else 1, 1))
        text = label + (" (restart)" if restart else "")
        tk.Label(line, text=text, bg=BG, fg=FG_DIM, width=34, anchor="w",
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Combobox(line, textvariable=var, values=choices, width=30,
                     style="Modern.TCombobox").pack(side="left", ipady=2)

    def _build_commands_editor(self, parent: tk.Widget) -> None:
        """D2: one row per action - editable verbs + takes_query."""
        self._section(parent, "commands (restart to apply)")
        specs = list(getattr(self.app_config, "commands", ()))
        for spec in specs:
            line = tk.Frame(parent, bg=BG)
            line.pack(fill="x", padx=6, pady=1)
            tk.Label(line, text=spec.action, bg=BG, fg=FG_DIM, width=14,
                     anchor="w", font=("Segoe UI", 9)).pack(side="left")
            verb_var = tk.StringVar(value=", ".join(spec.verbs))
            tk.Entry(line, textvariable=verb_var, bg="#1a2027", fg=FG,
                     insertbackground=FG, relief="flat", highlightthickness=0,
                     font=("Segoe UI", 9)).pack(
                         side="left", fill="x", expand=True, ipady=2)
            query_var = tk.BooleanVar(value=bool(spec.takes_query))
            tk.Checkbutton(line, text="query", variable=query_var, bg=BG,
                           fg=FG_DIM, activebackground=BG, selectcolor="#1a2027",
                           highlightthickness=0, font=("Segoe UI", 8),
                           ).pack(side="left", padx=(6, 0))
            self._cmd_vars[spec.action] = (verb_var, query_var)
        # A handler with no verbs is unreachable by voice: say so, but do not
        # invent an entry (validate_actions() warns about this at startup).
        try:
            from .commands import known_actions
            missing = [a for a in known_actions() if a not in self._cmd_vars]
        except Exception:
            missing = []
        if missing:
            tk.Label(parent, text="no verbs configured for: " + ", ".join(missing),
                     bg=BG, fg="#e8a33d", anchor="w",
                     font=("Segoe UI", 8)).pack(fill="x", padx=6, pady=(2, 0))

    def _build_settings_actions(self, parent: tk.Widget) -> None:
        """D3 save/reload, D4 diagnostics, E4 shortcut + autostart."""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=6, pady=(12, 2))
        for text, command in (("Save", self._settings_save),
                              ("Reload", self._settings_reload),
                              ("Copy diagnostics", self._settings_diagnostics),
                              ("Desktop shortcut", self._settings_shortcut),
                              ("Auto-start", self._settings_autostart)):
            primary = text == "Save"
            tk.Button(row, text=text, bg=ACCENT if primary else SURFACE_ALT,
                      fg="#07130b" if primary else FG, activebackground=ACCENT_DIM,
                      relief="flat", bd=0, command=command).pack(
                          side="left", padx=(0, 6))
        self._set_msg = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._set_msg, bg=BG, fg=FG_DIM,
                 justify="left", anchor="w", wraplength=640,
                 font=("Segoe UI", 8)).pack(fill="x", padx=6, pady=(2, 12))
        self._settings_msg(
            "comments are not preserved by the YAML round-trip; "
            "voice-command edits need a daemon restart")

    def _settings_msg(self, text: str) -> None:
        if self._set_msg is not None:
            self._set_msg.set(text)

    # -- D3: save / reload -------------------------------------------------

    def _collect_sections(self) -> dict:
        """D3: form -> ``{section: {key: coerced}}``; raises on a bad value.

        Only the keys the form shows are collected, so untouched siblings
        (``asr.whisper.*``, ``player.extra_args``, ...) survive the
        one-level-deep merge inside ``save_config_sections``.
        """
        sections: dict[str, dict] = {}
        for (section, key), var in self._set_vars.items():
            kind = self._set_kinds[(section, key)]
            try:
                value = _coerce(var.get(), kind)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{section}.{key}: {exc}") from exc
            sections.setdefault(section, {})[key] = value
        # D1: the two dropdowns are not plain text keys.
        if self._set_backend_var is not None:
            sections.setdefault("asr", {})["backend"] = self._set_backend_var.get()
        if self._set_mic_var is not None:
            index = _mic_index(self._set_mic_var.get(), self._set_mics)
            sections.setdefault("audio", {})["device"] = index
        if self._set_theme_var is not None:
            sections.setdefault("ui", {})["theme"] = self._set_theme_var.get()
        return sections

    def _collect_commands(self) -> list[dict]:
        """D2/D5: form -> the ``commands`` list, validated like startup does.

        Rejects exactly what ``config.load_config`` rejects, so a save either
        writes a file the daemon can load or changes nothing at all.
        """
        from .commands import known_actions

        specs: list[dict] = []
        seen_verbs: dict[str, str] = {}
        for action, (verb_var, query_var) in self._cmd_vars.items():
            raw = (verb_var.get() or "").strip()
            verbs = [part.strip().lower() for part in raw.split(",") if part.strip()]
            if not verbs:
                raise ValueError(f"commands.{action}: needs at least one verb")
            for verb in verbs:
                if verb in seen_verbs:
                    raise ValueError(
                        f"commands: verb {verb!r} is used by both "
                        f"{seen_verbs[verb]} and {action}")
                seen_verbs[verb] = action
            takes_query = bool(query_var.get())
            if takes_query and action != "play":
                # Same rule as config._build: one query slot, one search path.
                # The save refuses instead of writing a file that bricks voice.
                raise ValueError(
                    f"commands.{action}: takes_query may only be true for 'play'")
            specs.append({
                "action": action,
                "verbs": verbs,
                "takes_query": takes_query,
            })
        unknown = [s["action"] for s in specs if s["action"] not in known_actions()]
        if unknown:
            raise ValueError("commands: no handler for " + ", ".join(unknown))
        return specs

    def _settings_save(self) -> None:
        """D3/D5: validate, write, re-read; restore the file on any doubt."""
        cfg = self.app_config
        if cfg is None:
            return
        try:
            sections = self._collect_sections()
            if self._cmd_vars:
                sections["commands"] = self._collect_commands()
        except ValueError as exc:
            self._settings_msg(f"not saved - {exc}")
            return

        path = Path(cfg.source)
        backup = None
        try:
            if path.is_file():
                backup = path.read_text(encoding="utf-8")
            save_config_sections(path, sections)
            # Re-validate the file just written; on failure put the old text
            # back so a mistake never leaves an unloadable config behind.
            from .config import load_config

            load_config(path)
        except Exception as exc:
            if backup is not None:
                try:
                    atomic_write_text(path, backup)
                except OSError:
                    LOGGER.exception("could not restore %s", path)
            self._settings_msg(f"not saved - {exc}")
            return
        self._settings_msg(
            f"saved to {path} - theme applied live; restart the daemon for voice-command, "
            "backend and microphone changes")
        self._snapshot_settings_values()
        self._set_dirty = False

    def _settings_reload(self) -> None:
        """D3: re-read the file into the form (discards unsaved edits)."""
        cfg = self.app_config
        if cfg is None:
            return
        try:
            import yaml

            doc = yaml.safe_load(Path(cfg.source).read_text(encoding="utf-8")) or {}
        except Exception as exc:
            self._settings_msg(f"reload failed - {exc}")
            return
        for (section, key), var in self._set_vars.items():
            value = (doc.get(section) or {}).get(key)
            if value is None:
                continue
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set(", ".join(value) if isinstance(value, list) else str(value))
        if self._set_theme_var is not None:
            self._set_theme_var.set(str((doc.get("ui") or {}).get("theme", "midnight")))
        if self._set_backend_var is not None:
            self._set_backend_var.set(str((doc.get("asr") or {}).get(
                "backend", cfg.asr.backend)))
        if self._set_mic_var is not None:
            self._set_mics = list_mics()
            self._set_mic_var.set(_mic_choice(cfg, self._set_mics))
        for spec in (doc.get("commands") or []):
            pair = self._cmd_vars.get(spec.get("action"))
            if pair is None:
                continue
            pair[0].set(", ".join(spec.get("verbs") or []))
            pair[1].set(bool(spec.get("takes_query")))
        self._settings_msg("reloaded from disk")
        self._snapshot_settings_values()

    # -- D4/E4: diagnostics, shortcut, autostart ----------------------------

    def _settings_diagnostics(self) -> None:
        """D4: the block a bug report needs, straight to the clipboard."""
        cfg = self.app_config
        lines = ["voiceyt diagnostics"]
        try:
            from . import __version__

            lines.append(f"version: {__version__}")
        except Exception:
            pass
        if cfg is not None:
            lines.append(f"config: {cfg.source}")
            lines.append(f"backend: {cfg.asr.backend} device={cfg.asr.device} "
                         f"language={cfg.asr.language}")
            lines.append(f"models_dir: {cfg.asr.models_dir}")
            lines.append(f"mpv_path: {cfg.player.mpv_path}")
            lines.append(f"aec: enabled={cfg.aec.enabled} backend={cfg.aec.backend}")
        snap = self.ui_state.snapshot()
        lines.append(f"last action: {snap.get('action') or '(none)'}")
        lines.append(f"last error: {snap.get('error') or '(none)'}")
        mics = ", ".join(f"{i}={n}" for i, n in list_mics()) or "(none)"
        lines.append(f"microphones: {mics}")
        text = "\n".join(lines)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._settings_msg("diagnostics copied to the clipboard")
        except tk.TclError:
            self._settings_msg(text)

    def _settings_shortcut(self) -> None:
        """E4: run the shipped ``create_shortcut.ps1`` (desktop shortcut)."""
        script = _project_root() / "create_shortcut.ps1"
        if not script.is_file():
            self._settings_msg(f"no {script.name} in the project root")
            return
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                cwd=str(_project_root()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._settings_msg("shortcut script started")
        except OSError as exc:
            self._settings_msg(f"shortcut failed - {exc}")

    def _settings_autostart(self) -> None:
        """E4: copy the launcher into the per-user Startup folder (toggle).

        Same ``voiceyt.bat`` the desktop shortcut points at; no registry
        writes, and the user can undo it by deleting one file from
        shell:startup.
        """
        appdata = os.environ.get("APPDATA")
        if not appdata:
            self._settings_msg("cannot find the Startup folder")
            return
        startup = Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"
        target = startup / "voiceyt.bat"
        if target.is_file():
            try:
                target.unlink()
                self._settings_msg(f"auto-start removed ({target})")
            except OSError as exc:
                self._settings_msg(f"could not remove - {exc}")
            return
        source = _project_root() / "voiceyt.bat"
        if not source.is_file():
            self._settings_msg("no voiceyt.bat in the project root")
            return
        try:
            startup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self._settings_msg(f"auto-start added ({target})")
        except OSError as exc:
            self._settings_msg(f"could not copy - {exc}")

    # -- events ----------------------------------------------------------

    def _ui_step_volume(self, delta: float) -> None:
        player = self.ui_state.player
        if player is None:
            return
        current = float(self.ui_state.snapshot().get("volume", 50))
        target = max(0.0, min(130.0, current + float(delta)))
        self.ui_state.set_volume(int(round(target)))
        if self._vol_label is not None:
            self._vol_label.set(f"{target:.0f}%")
        try:
            applied = player.set_volume(target)
            self.ui_state.set_volume(int(round(applied)))
            if self._vol_label is not None:
                self._vol_label.set(f"{applied:.0f}%")
        except Exception as exc:
            self.ui_state.set_error(f"player: {exc}")

    def _set_volume_local(self, value: float) -> str:
        if self._vol_var is not None:
            self._vol_var.set(value)
            self._vol_label.set(f"{value:.0f}%")
        return "break"

    def _nudge_volume(self, delta: float) -> str:
        value = float(self._vol_var.get() or 0) if self._vol_var is not None else 0.0
        return self._set_volume_local(max(0.0, min(130.0, value + delta)))

    def _on_volume_wheel(self, event) -> str:
        step = getattr(self.ui_state.player, "volume_step", 10)
        return self._nudge_volume(step if event.delta > 0 else -step)

    def _on_volume_wheel_and_commit(self, event) -> str:
        self._on_volume_wheel(event)
        return self._commit_local_volume()

    def _commit_local_volume(self, _event=None) -> str:
        """Commit keyboard/wheel changes immediately with a plain float."""
        value = float(self._vol_var.get() or 0) if self._vol_var is not None else 0.0
        _run_in_background(
            "volume-keyboard", _player_set_volume,
            self.ui_state.player, self.ui_state, value,
        )
        return "break"

    @staticmethod
    def _toggle_playback(self) -> str:
        _run_in_background(
            "transport", _player_transport,
            self.ui_state.player, self.ui_state, "play_pause",
        )
        return "break"

    def _format_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _seek_percent_from_event(self, event) -> float:
        width = max(1, self._seek_scale.winfo_width())
        return max(0.0, min(100.0, float(event.x) * 100.0 / width))

    def _draw_seek(self) -> None:
        if self._seek_scale is None or self._seek_var is None:
            return
        width = max(1, self._seek_scale.winfo_width())
        percent = max(0.0, min(100.0, float(self._seek_var.get())))
        x = width * percent / 100.0
        self._seek_scale.coords(self._seek_track_id, 0, 8, width, 11)
        self._seek_scale.coords(self._seek_fill_id, 0, 8, x, 11)
        self._seek_scale.coords(self._seek_handle_id, x - 7, 4, x + 7, 18)

    def _on_seek_press(self, event) -> str:
        self._seek_dragging = True
        self._seek_var.set(self._seek_percent_from_event(event))
        self._draw_seek()
        return "break"

    def _on_seek_motion(self, event) -> str:
        self._seek_var.set(self._seek_percent_from_event(event))
        self._draw_seek()
        if self._seek_duration and self._seek_label is not None:
            self._seek_label.set(
                f"{self._format_time(self._seek_var.get() / 100 * self._seek_duration)} / "
                f"{self._format_time(self._seek_duration)}"
            )
        return "break"

    def _on_seek_release(self, _event) -> str:
        self._seek_dragging = False
        if self._seek_duration:
            seconds = max(0.0, min(self._seek_duration, self._seek_var.get() / 100 * self._seek_duration))
            self._seek_last_committed = seconds
            _run_in_background("seek", lambda p, s: p.seek(s), self.ui_state.player, seconds)
        return "break"

    def _on_volume_press(self, _event) -> None:
        self._volume_dragging = True
        self._on_volume_motion(_event)

    def _on_volume_motion(self, _event) -> None:
        self._volume_dragging = True
        if self._vol_var is not None:
            self._vol_label.set(f"{self._vol_var.get():.0f}%")

    def _on_volume_release(self, _event) -> None:
        """Capture on Tk thread, then commit a plain float on a worker."""
        self._volume_dragging = False
        value = float(self._vol_var.get()) if self._vol_var is not None else 50.0
        _run_in_background(
            "volume-set", _player_set_volume,
            self.ui_state.player, self.ui_state, value,
        )

    def _on_mute_toggle(self) -> None:
        """Read the current volume on Tk's thread, then mute in the worker."""
        player = self.ui_state.player
        if player is None:
            return
        try:
            current = float(player.volume())
        except Exception as exc:
            LOGGER.warning("player volume read failed: %s", exc)
            self.ui_state.set_error(f"player: {exc}")
            return
        previous = self._mute_before
        if current > 0:
            self._mute_before = current
            target = 0.0
        else:
            target = previous if previous is not None else 50.0
        _run_in_background(
            "mute", _player_toggle_mute, player, self.ui_state, target
        )

    def _on_pool_click(self, index: int) -> None:
        if self._drag_moved or self.on_pool_click is None:
            return
        try:
            self.on_pool_click(index)
        except Exception:
            LOGGER.exception("pool click handler failed")

    # -- E1/E2/E5: player-tab extras --------------------------------------

    def _submit_search(self, query: str | None = None) -> None:
        """E2: hand the typed (or clicked) query to the daemon's play path."""
        if self._search_var is None:
            text = query or ""
        else:
            text = query if query is not None else self._search_var.get()
        text = (text or "").strip()
        if not text or self.on_play_query is None:
            return
        if self._search_var is not None:
            self._search_var.set("")
        _run_in_background("search", self.on_play_query, text)

    def _refresh_recent(self, recent: list[str]) -> None:
        """E1: repaint the strip only when the list actually changed."""
        if self._recent_box is None or recent == self._recent_shown:
            return
        self._recent_shown = list(recent)
        for widget in self._recent_buttons:
            widget.destroy()
        self._recent_buttons = []
        if not recent:
            return
        tk.Label(self._recent_box, text="recent:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 6))
        for query in recent:
            button = tk.Button(
                self._recent_box, text=_truncate(query, RECENT_MAX_CHARS),
                font=("Segoe UI", 8), relief="flat", bg="#1a2027", fg=FG,
                command=lambda q=query: self._submit_search(q),
            )
            button.pack(side="left", padx=(0, 4))
            self._recent_buttons.append(button)

    def _show_cheatsheet(self) -> None:
        """E5: trigger word + every known verb, built from the live Config."""
        if self._cheat_window is not None:
            try:
                self._cheat_window.lift()
                return
            except tk.TclError:          # the user closed it already
                self._cheat_window = None
        top = tk.Toplevel(self)
        self._cheat_window = top
        top.title("voiceyt - what to say")
        top.configure(bg=BG)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self._close_cheatsheet)

        trigger = ""
        lines: list[str] = []
        if self.app_config is not None:
            words = getattr(self.app_config.trigger, "words", ())
            trigger = words[0] if words else ""
            for spec in getattr(self.app_config, "commands", ()):
                verbs = " / ".join(spec.verbs)
                suffix = " <query>" if spec.takes_query else ""
                lines.append(f"{verbs}{suffix}   ->   {spec.action}")
        if not lines:
            lines = ["(no commands configured)"]

        tk.Label(top, text=f"trigger: {trigger or '(none)'}", bg=BG, fg=FG,
                 anchor="w", font=("Segoe UI", 11, "bold")).pack(
                     fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="\n".join(lines), bg=BG, fg=FG_DIM, justify="left",
                 anchor="w", font=("Segoe UI", 10)).pack(fill="x", padx=14)
        tk.Label(top, text=f"say: {trigger} <verb>" if trigger else "", bg=BG,
                 fg=FG_DIM, anchor="w", font=("Segoe UI", 9)).pack(
                     fill="x", padx=14, pady=(8, 12))

    def _close_cheatsheet(self) -> None:
        if self._cheat_window is not None:
            try:
                self._cheat_window.destroy()
            except tk.TclError:
                pass
        self._cheat_window = None

    def _close_app(self) -> None:
        """Request shutdown without blocking Tk while PortAudio closes."""
        self.ui_state.shutdown.set()
        if self._on_quit is not None:
            threading.Thread(
                target=self._on_quit,
                name="voiceyt-ui-shutdown",
                daemon=True,
            ).start()
        self.quit()

    def _drag_start(self, event) -> None:
        self._drag_xy = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())
        self._drag_moved = False

    def _drag_move(self, event) -> None:
        if self._drag_xy is None:  # click without motion: still a pool click
            return
        self._drag_moved = True
        self.geometry(f"+{event.x_root - self._drag_xy[0]}+{event.y_root - self._drag_xy[1]}")

    def _drag_end(self, _event) -> None:
        # A pool-row click fires on press; when the press turned into a drag,
        # no rows change.  The flag only suppresses the pending click.
        self._drag_xy = None

    def show(self) -> None:
        """Reveal the overlay (a new utterance, an error, startup)."""
        if self._hide_while_playing and self._playing_now():
            self.withdraw()          # music is running: stay out of the way
            self._hidden = True
            return
        if self._hidden:
            self.deiconify()
            self._hidden = False
        self.lift()

    # -- polling ---------------------------------------------------------

    def _poll_ui(self) -> None:
        """Refresh every widget from the state snapshot, then re-arm."""
        if self.ui_state.shutdown.is_set():
            self.quit()
            return

        now = time.monotonic()
        snap = self.ui_state.snapshot()
        heard_age = now - snap["heard_ts"]
        error_age = now - snap["error_ts"]
        action_age = now - snap["action_ts"]

        # --- status dot ---
        if self.ui_state.paused.is_set():
            dot = DOT_PAUSED
        elif snap["error"] and error_age < ERROR_LIFETIME_S:
            dot = DOT_ERROR
        elif snap["heard"] and heard_age < HEARD_LIFETIME_S:
            dot = DOT_BUSY
        else:
            dot = DOT_IDLE
        self._dot.itemconfig(self._dot_id, fill=dot)

        # --- mic level meter ---
        capture = self.ui_state.capture
        level = capture.level if capture is not None else 0.0
        frac = min(1.0, max(0.0, (float(level) - 0.001) / (LEVEL_FULL - 0.001)))
        self._meter_frac += 0.55 * (frac - self._meter_frac)   # fast, visible response
        meter_width = max(1, self._meter.winfo_width())
        width = int(meter_width * self._meter_frac)
        self._meter.coords(self._meter_id, 0, 0, width, 4)
        self._meter.itemconfig(
            self._meter_id, fill=DOT_BUSY if level > LEVEL_SPEAK else DOT_IDLE
        )

        # --- big line: a fresh error outranks whatever was heard before ---
        if snap["error"] and error_age < ERROR_LIFETIME_S:
            self._heard_var.set(_truncate(snap["error"], HEARD_MAX_CHARS))
            self._heard_label.configure(fg=DOT_ERROR)
            self.show()
        elif snap["heard"] and heard_age < HEARD_LIFETIME_S:
            self._heard_var.set(_truncate(snap["heard"], HEARD_MAX_CHARS))
            self._heard_label.configure(fg=FG)
            self.show()

        if self._meter_frac > 0.02:
            self._heard_label.configure(fg=ACCENT)

        # --- status line: last action, then what is playing ---
        self._action_var.set(self._status_line(snap, action_age))

        # --- result pool (compact labels) + B5 expanded listbox ---
        titles = self._pool_titles()
        if self.expanded and self._voice_preview_list is not None:
            if titles != self._preview_titles:
                self._preview_titles = list(titles)
                selected = self._voice_preview_list.curselection()
                self._voice_preview_list.delete(0, "end")
                for pos, title in enumerate(titles, start=1):
                    self._voice_preview_list.insert(
                        "end", f"{pos}. {_truncate(title, POOL_MAX_CHARS)}"
                    )
                if selected and selected[0] < len(titles):
                    self._voice_preview_list.selection_set(selected[0])
        else:
            for index, label in enumerate(self._pool_labels):
                if index < len(titles):
                    text = _truncate(titles[index], POOL_MAX_CHARS)
                    label.configure(text=f"{index + 1}. {text}", fg=FG)
                else:
                    label.configure(text="", fg=FG_DIM)

        playback = self.ui_state.playback
        playback_snapshot = None
        if playback is not None:
            try:
                playback_snapshot = playback.snapshot()
                self.ui_state.set_playback_snapshot(playback_snapshot)
            except Exception:
                playback_snapshot = None

        if self.expanded:
            # B2: now-playing line reuses _now_playing() verbatim (no new player
            # code) instead of rebuilding the title/position/volume string.
            if self._now_var is not None:
                self._now_var.set(self._now_playing() or "Nothing playing")
            if self._queue_var is not None:
                try:
                    queue = self.ui_state.playback.pool_titles() if self.ui_state.playback is not None else (
                        self.ui_state.player.pool_titles() if self.ui_state.player else []
                    )
                    position = playback_snapshot.current_index if playback_snapshot is not None else (
                        self.ui_state.playback.current_position() if self.ui_state.playback is not None else (
                            self.ui_state.player.current_position() if self.ui_state.player else None
                        )
                    )
                    self._queue_var.set(
                        f"Next: {queue[position + 1]}" if queue and position is not None and position + 1 < len(queue)
                        else f"Queue: {len(queue)} track{'s' if len(queue) != 1 else ''}"
                    )
                except Exception:
                    self._queue_var.set("Queue unavailable")
            queried = self._tick % PLAYER_POLL_EVERY == 0
            if queried:
                self._sync_playlist_highlight()
                self._sync_current_rows()
            if self._play_pause_button is not None:
                self._play_pause_button.configure(
                    text="⏸" if self._playing_now() else "▶"
                )
            # B4: volume polling is decimated (every PLAYER_POLL_EVERY ticks)
            # so mpv IPC stays off the 250 ms hot path; never fight a drag.
            self._tick += 1
            if self._tick % PLAYER_POLL_EVERY == 0:
                player = self.ui_state.player
                playback = self.ui_state.playback
                if (player is not None or playback is not None) and not self._volume_dragging:
                    try:
                        level = int(round(playback.volume() if playback is not None else player.volume()))
                        if self._vol_var is not None:
                            self._vol_var.set(level)
                        if self._vol_label is not None:
                            self._vol_label.set(f"{level}%")
                    except Exception:
                        pass  # mpv restarting: keep the last slider position
            # B6: mic + backend + mpv + AEC, same tick, no new threads.
            if queried and (self.ui_state.player is not None or self.ui_state.playback is not None):
                try:
                    playback = self.ui_state.playback
                    current_seconds, duration = (playback.playback_position() if playback is not None else self.ui_state.player.playback_position())
                    self._seek_duration = duration
                    if not self._seek_dragging and self._seek_var is not None:
                        self._seek_var.set(
                            current_seconds / duration * 100 if duration else 0.0
                        )
                        self._draw_seek()
                    if self._seek_label is not None:
                        if duration and self._seek_last_committed is not None:
                            if abs(current_seconds - self._seek_last_committed) > 1.0:
                                self._seek_last_committed = None
                        self._seek_label.set(
                            f"{self._format_time(current_seconds)} / "
                            f"{self._format_time(duration) if duration else '--:--'}"
                        )
                except Exception:
                    pass
            if self._status_var is not None:
                self._status_var.set(
                    _player_status_line(self.ui_state.player, self.ui_state, snap))
            # E1: repaint the recent strip only when its list changed.
            self._refresh_recent(snap.get("recent") or [])
            if snap.get("search_results_ts", 0) != self._browse_results_ts:
                self._browse_results_ts = snap.get("search_results_ts", 0)
                self._refresh_browse(snap.get("search_results") or [])

        # --- ui.hide_while_playing: tuck the window away while the music runs ---
        if self._hide_while_playing:
            playing = self._playing_now()
            if playing and not self._hidden:
                self.withdraw()
                self._hidden = True
                self._hidden_for_playback = True
            elif not playing and self._hidden_for_playback:
                self._hidden_for_playback = False
                self.show()

        # The overlay stays fully opaque. It may still hide when configured,
        # but it never fades or changes window alpha.
        busy = max(snap["heard_ts"] if snap["heard"] else 0.0, snap["action_ts"], snap["error_ts"])
        if busy:
            if now - busy > self._hide_after_s and not self._hidden:
                self.withdraw()
                self._hidden = True
        elif now - self._born > self._hide_after_s and not self._hidden:
            self.withdraw()
            self._hidden = True

        self.after(POLL_MS, self._poll_ui)

    # -- helpers ---------------------------------------------------------

    def _status_line(self, snap: dict, action_age: float) -> str:
        """The last action plus what is playing, when there is something."""
        playing = self._now_playing()
        label = ""
        if snap["action"] and action_age < ACTION_LIFETIME_S:
            label = snap["action"]
            if snap["error"] and action_age >= ERROR_LIFETIME_S:
                label = f"{label} (failed)"
        elif self.ui_state.paused.is_set():
            label = "paused"
        if label and playing:
            return f"{label}  \u2022  {playing}"
        if label or playing:
            return label or playing
        # idle: prove which microphone is being listened to
        mic = getattr(self.ui_state.capture, "device_name", "") \
            if self.ui_state.capture is not None else ""
        return f"listening \u2022 {mic}" if mic else "listening (no microphone)"

    def _now_playing(self) -> str:
        playback = self.ui_state.playback
        player = self.ui_state.player
        if playback is None and player is None:
            return ""
        try:
            titles = playback.pool_titles() if playback is not None else player.pool_titles()
            title = (playback.current_title() if playback is not None else player.current_title()) or ""
            if not title:
                return f"{len(titles)} result(s) queued" if titles else ""
            position = titles.index(title) + 1 if title in titles else 0
            volume = int(round(playback.volume() if playback is not None else player.volume()))
            return (
                f"{_truncate(title, TITLE_MAX_CHARS)} "
                f"[{position}/{len(titles)}]  vol {volume}%"
            )
        except Exception:  # mpv may be restarting; the overlay must not care
            return ""

    def _pool_titles(self) -> list[str]:
        # Voice previews are deliberately separate from the player queue. A
        # playlist click replaces the queue, but must not alter the five rows
        # shown under the voice header.
        if self.expanded:
            return list(self.ui_state.snapshot().get("voice_previews") or [])
        player = self.ui_state.player
        if player is None:
            return []
        try:
            return player.pool_titles()
        except Exception:
            return []

    def _playing_now(self) -> bool:
        playback = self.ui_state.playback
        if playback is not None:
            try:
                return playback.snapshot().state.value == "playing"
            except Exception:
                return False
        player = self.ui_state.player
        if player is None:
            return False
        try:
            checker = getattr(player, "is_playing", None)
            return bool(checker() if callable(checker) else player.playing)
        except Exception:  # mpv may be restarting; assume idle
            return False



# ---- tray icon -------------------------------------------------------------

# Window messages used by the tray window (WM_APP + n keeps them private).
WM_TRAY = 0x8000 + 1     # icon callback; lParam is the mouse message
WM_TOGGLE = 0x8000 + 2   # pause / resume
WM_MENU = 0x8000 + 3     # open the context menu
WM_MIC = 0x8000 + 4      # wParam is the input-device index
WM_QUIT = 0x8000 + 5     # leave the daemon


def _wnd_proc(hwnd, msg, wparam, lparam):
    """Window procedure for the tray window (module level on purpose).

    Keep this alive in a variable while the window exists or win32 will call
    freed memory; :class:`Tray` stores it in ``self._proc``.
    """
    apis = _win32()
    tray = Tray._instance
    if tray is None:
        return apis.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    try:
        handled = tray.handle_message(msg, wparam, lparam)
    except Exception:
        LOGGER.exception("tray window procedure failed")
        handled = None
    if handled is None:
        return apis.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return handled


class Tray:
    """System-tray icon with its own message loop (raw ctypes).

    The callbacks run on the tray thread, so they may only flip events or call
    into code that is already thread-safe (mpv IPC, re-opening the capture).
    """

    # --- notifications (Shell_NotifyIconW) ---
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    # --- window messages ---
    WM_NULL = 0x0000
    WM_CLOSE = 0x0010
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205

    # --- menus ---
    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    MF_POPUP = 0x00000010
    MF_GRAYED = 0x00000001
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100
    TPM_NONOTIFY = 0x0080

    MENU_TOGGLE = 1
    MENU_QUIT = 2
    MENU_MIC_BASE = 1000     # MENU_MIC_BASE + device index

    IDI_APPLICATION = 32512
    ERROR_CLASS_ALREADY_EXISTS = 1410
    WINDOW_CLASS = "voiceyt-tray-window"

    _instance: "Tray | None" = None

    def __init__(
        self,
        state: UiState,
        on_pause=None,
        on_resume=None,
        on_mic_pick=None,
        on_quit=None,
    ):
        self.state = state
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_mic_pick = on_mic_pick
        self.on_quit = on_quit
        self._apis: _Win32 | None = None
        self._hwnd = None
        self._proc = None
        self._icon_installed = False
        self._quitting = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()


    @property
    def installed(self) -> bool:
        """True once the icon is on the notification area."""
        return self._icon_installed


    # -- life cycle ------------------------------------------------------

    def start(self) -> "Tray":
        """Create the icon on a background thread and wait until it is up."""
        if self._thread is not None:
            return self
        Tray._instance = self
        self._thread = threading.Thread(target=self._run, name="voiceyt-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self

    def stop(self) -> None:
        """Tear the icon down and join the thread (callable from anywhere)."""
        if self._apis is not None and self._hwnd:
            self._apis.user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # -- tray thread -----------------------------------------------------

    def _run(self) -> None:
        apis = _win32()
        self._apis = apis
        try:
            self._create_window(apis)
            self._add_icon(apis)
            self._ready.set()
            msg = wt.MSG()
            while apis.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                apis.user32.TranslateMessage(ctypes.byref(msg))
                apis.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            LOGGER.exception("tray thread stopped")
            self._ready.set()
        finally:
            self._remove_icon(apis)
            if self._hwnd:
                apis.user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            Tray._instance = None

    def _create_window(self, apis: _Win32) -> None:
        user32 = apis.user32
        self._proc = WNDPROC(_wnd_proc)
        wnd_class = WNDCLASSW()
        wnd_class.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wnd_class.hInstance = apis.kernel32.GetModuleHandleW(None)
        wnd_class.lpszClassName = self.WINDOW_CLASS
        if not user32.RegisterClassW(ctypes.byref(wnd_class)):
            error = ctypes.get_last_error()
            if error != self.ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(error)
        hwnd = user32.CreateWindowExW(
            0, self.WINDOW_CLASS, "voiceyt", 0, 0, 0, 0, 0, None, None,
            wnd_class.hInstance, None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd                       # never shown: message sink

    def _add_icon(self, apis: _Win32) -> None:
        user32 = apis.user32
        icon = user32.LoadIconW(None, _resource(self.IDI_APPLICATION))
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = icon or 0
        data.szTip = "voiceyt - click to pause, right-click for the menu"
        if not apis.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._icon_installed = True

    def _remove_icon(self, apis: _Win32 | None) -> None:
        apis = apis or self._apis
        if not (apis and self._icon_installed and self._hwnd):
            return
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        apis.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
        self._icon_installed = False

    # -- messages --------------------------------------------------------

    def handle_message(self, msg: int, wparam: int, lparam: int):
        """React to the tray window's messages; ``None`` = default handling."""
        if msg == WM_TRAY:
            if lparam == self.WM_LBUTTONUP:
                self.toggle_pause()
            elif lparam == self.WM_RBUTTONUP:
                self.show_menu()
            return 0
        if msg == WM_TOGGLE:
            self.toggle_pause()
            return 0
        if msg == WM_MENU:
            self.show_menu()
            return 0
        if msg == WM_MIC:
            self.pick_microphone(int(wparam))
            return 0
        if msg == WM_QUIT or msg == self.WM_CLOSE:
            self.request_quit()
            return 0
        return None

    # -- actions (all run on the tray thread) ----------------------------

    def toggle_pause(self) -> None:
        """Flip the pause flag and let the daemon react to it."""
        if self.state.paused.is_set():
            self.state.paused.clear()
            self._notify(self.on_resume, "resume callback failed")
        else:
            self.state.paused.set()
            self._notify(self.on_pause, "pause callback failed")

    def pick_microphone(self, index: int) -> None:
        """Ask the daemon to move the capture to *index*."""
        if self.on_mic_pick is None:
            return
        try:
            self.on_mic_pick(index)
        except Exception:
            LOGGER.exception("microphone switch failed")

    def request_quit(self) -> None:
        """Set the shutdown flag, tell the daemon, then leave the loop."""
        if self._quitting:
            return
        self._quitting = True
        self.state.shutdown.set()
        self._notify(self.on_quit, "quit callback failed")
        self._remove_icon(self._apis)
        if self._apis is not None:
            self._apis.user32.PostQuitMessage(0)

    @staticmethod
    def _notify(callback, message: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            LOGGER.exception(message)

    # -- context menu ----------------------------------------------------

    def show_menu(self) -> None:
        """Track the context menu at the cursor (tray thread only)."""
        apis = self._apis
        if apis is None or not self._hwnd:
            return
        user32 = apis.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return

        pause_label = "Resume" if self.state.paused.is_set() else "Pause"
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_TOGGLE, pause_label)
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)

        mics = self._microphone_menu(apis)
        if mics:
            user32.AppendMenuW(menu, self.MF_POPUP, mics, "Microphone")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, self.MENU_QUIT, "Quit voiceyt")

        point = wt.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # a popup menu needs a foreground window or it will not close properly
        user32.SetForegroundWindow(self._hwnd)
        choice = user32.TrackPopupMenuEx(
            menu,
            self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD | self.TPM_NONOTIFY,
            point.x, point.y, self._hwnd, None,
        )
        user32.DestroyMenu(menu)          # also destroys the submenu
        user32.PostMessageW(self._hwnd, self.WM_NULL, 0, 0)

        if choice == self.MENU_TOGGLE:
            self.toggle_pause()
        elif choice == self.MENU_QUIT:
            self.request_quit()
        elif choice >= self.MENU_MIC_BASE:
            self.pick_microphone(choice - self.MENU_MIC_BASE)

    def _microphone_menu(self, apis: _Win32):
        """Submenu listing the input devices, current one ticked."""
        user32 = apis.user32
        submenu = user32.CreatePopupMenu()
        if not submenu:
            return None
        devices = list_mics()
        if not devices:
            user32.AppendMenuW(submenu, self.MF_STRING | self.MF_GRAYED, 0, "no input device")
            return submenu
        current = getattr(self.state.capture, "device_index", None)
        for index, name in devices:
            tick = "\u2713 " if index == current else "   "
            user32.AppendMenuW(
                submenu, self.MF_STRING, self.MENU_MIC_BASE + index,
                f"{tick}{_truncate(name, 50)}",
            )
        return submenu



# ---- entry point -----------------------------------------------------------


def _overlay_mainloop(state: UiState, config, on_pool_click,
                      app_config=None, on_play_query=None,
                      on_play_playlist=None, on_browse_query=None,
                      on_quit=None) -> None:
    """Own the tkinter thread: build the window, run it, tear it down."""
    try:
        overlay = Overlay(
            state, config, on_pool_click, app_config, on_play_query,
            on_play_playlist, on_browse_query, on_quit,
        )
    except Exception:
        LOGGER.exception("cannot create the overlay window")
        return
    try:
        overlay.mainloop()
    except Exception:
        LOGGER.exception("overlay mainloop failed")
    finally:
        try:
            overlay.destroy()
        except Exception:
            LOGGER.debug("overlay destroy failed", exc_info=True)
        finally:
            state.overlay_done.set()


def start_ui(
    state: UiState,
    config,
    on_pool_click=None,
    on_pause=None,
    on_resume=None,
    on_mic_pick=None,
    on_quit=None,
    app_config=None,
    on_play_query=None,
    on_play_playlist=None,
    on_browse_query=None,
) -> "Tray | None":
    """Start the overlay and the tray icon in their own threads.

    Returns the tray handle so the caller can :meth:`Tray.stop` it at
    shutdown, or ``None`` when the tray is unavailable (the overlay still
    works).  Shutting down is driven by ``state.shutdown``: the overlay quits
    its mainloop and the tray leaves its message loop.
    """
    tray = None
    if sys.platform == "win32":
        try:
            tray = Tray(
                state,
                on_pause=on_pause,
                on_resume=on_resume,
                on_mic_pick=on_mic_pick,
                on_quit=on_quit,
            ).start()
            if not tray.installed:
                LOGGER.warning("tray icon unavailable; overlay only")
        except Exception:
            LOGGER.exception("cannot start the tray icon; overlay only")
            tray = None
    else:  # pragma: no cover - the project targets Windows
        LOGGER.warning("the tray icon needs Windows; starting the overlay only")

    threading.Thread(
        target=_overlay_mainloop,
        args=(
            state, config, on_pool_click, app_config, on_play_query,
            on_play_playlist, on_browse_query, on_quit,
        ),
        name="voiceyt-overlay",
        daemon=True,
    ).start()
    return tray

