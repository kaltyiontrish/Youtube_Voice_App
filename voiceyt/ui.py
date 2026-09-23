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

from .config import save_config_sections

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

BG = "#11151a"
FG = "#e8eaed"
FG_DIM = "#9aa0a6"

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

    def __init__(self, player=None, capture=None):
        self.player = player        # MpvPlayer | None - polled for titles/volume
        self.capture = capture      # AudioCapture | None - marked in the mic menu
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
        self.paused = threading.Event()
        self.shutdown = threading.Event()

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

    def set_playing(self, playing: bool) -> None:
        with self._lock:
            self._playing = playing

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
                "recent": list(self._recent),
            }


def list_mics() -> list[tuple[int, str]]:
    """Input devices as ``(index, name)``; safe to call from the tray thread."""
    try:
        from .audio import list_input_devices

        return [(device.index, device.name) for device in list_input_devices()]
    except Exception as exc:  # sounddevice can fail when there is no device
        LOGGER.warning("cannot list microphones: %s", exc)
        return []


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


def _player_transport(player, state: UiState, action: str) -> None:
    """B3: transport buttons call existing player methods only.

    Play/Pause toggles the shared pause flag (``Tray.toggle_pause``
    semantics); ``stop``/``prev``/``next``/``resume`` map to the matching
    :class:`MpvPlayer` method.  Loud failures land on the overlay.
    """
    if player is None:
        return
    try:
        if action == "play_pause":
            if state.paused.is_set():
                state.paused.clear()
                state.set_action("listening again")
            else:
                state.paused.set()
                state.set_action("paused")
        elif action == "stop":
            player.stop()
        elif action == "prev":
            player.prev_track()
        elif action == "next":
            player.next_track()
        elif action == "resume":
            player.unpause()
    except Exception as exc:  # mpv may be restarting; report, don't crash
        LOGGER.warning("player %s failed: %s", action, exc)
        state.set_error(f"player: {exc}")


def _player_set_volume(player, state: UiState, raw_value) -> None:
    """B4: volume slider release -> ``set_volume()`` (clamps inside)."""
    if player is None:
        return
    try:
        applied = player.set_volume(float(raw_value))
    except Exception as exc:  # mpv may be restarting; report, don't crash
        LOGGER.warning("player set_volume failed: %s", exc)
        state.set_error(f"player: {exc}")
        return
    state.set_volume(int(round(applied)))


def _player_step_volume(player, state: UiState, delta: float) -> None:
    """B4: +/- step buttons -> ``volume_delta()`` (clamps inside)."""
    if player is None:
        return
    try:
        player.volume_delta(float(delta))
    except Exception as exc:  # mpv may be restarting; report, don't crash
        LOGGER.warning("player volume_delta failed: %s", exc)
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
        titles = player.pool_titles()
    except Exception:  # mpv restarting: keep the click a silent no-op
        return False
    if index < 0 or index >= len(titles):
        return False
    try:
        return bool(player.jump_to(index))
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
                 app_config=None, on_play_query=None):
        super().__init__()
        self.ui_state = state
        self.ui_config = config
        self.app_config = app_config        # D: live Config the tabs read/write
        self.on_pool_click = on_pool_click
        self.on_play_query = on_play_query  # E2: player-tab search box
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
        self._player_box: tk.Widget | None = None   # B: player tab (expanded only)
        self._player_list: tk.Listbox | None = None  # B5: scrolling pool rows
        self._player_titles: list[str] = []
        self._vol_var: tk.DoubleVar | None = None   # B4: slider value
        self._vol_label: tk.StringVar | None = None  # B4: "75%" readout
        self._mute_before: float | None = None  # B4: pre-mute volume for the toggle
        self._now_var: tk.StringVar | None = None   # B1/B2: now-playing line
        self._status_var: tk.StringVar | None = None  # B6: status bar text
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
        # D: settings tab (StringVar per dotted key + a kind for coercion)
        self._set_vars: dict[tuple[str, str], tk.Variable] = {}
        self._set_kinds: dict[tuple[str, str], str] = {}
        self._set_msg: tk.StringVar | None = None
        self._cmd_vars: dict[str, tuple[tk.StringVar, tk.BooleanVar]] = {}
        self._set_backend_var: tk.StringVar | None = None
        self._set_mic_var: tk.StringVar | None = None
        self._set_mics: list[tuple[int, str]] = []
        self._build_ui()
        self.after(POLL_MS, self._poll_ui)

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.overrideredirect(True)          # no title bar, no taskbar entry
        self.attributes(
            "-topmost",
            self.ui_config.always_on_top if self.ui_config is not None else True,
        )
        self.configure(bg=BG)
        self._born = time.monotonic()        # for the startup quiet timer
        self.geometry(f"{self._win_width}x{self._win_height}{WINDOW_POS}")
        self.show()                          # visible at startup ("listening")
        # Drag a frameless window by holding the left button anywhere and
        # moving: pool rows still get their click when there is no motion.
        self._drag_xy: tuple[int, int] | None = None
        self._drag_moved = False
        self.bind("<ButtonPress-1>", self._drag_start)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", self._drag_end)

        if self.expanded:
            self._build_expanded_ui()
        else:
            self._build_compact_ui()

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
        """B: header widgets move up unchanged, then notebook + status bar."""
        self._build_compact_ui()  # B1: same widgets, same logic, now the top
        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=14, pady=(6, 4))
        self._player_box = tk.Frame(book, bg=BG)
        book.add(self._player_box, text="Player")
        self._build_player_tab(self._player_box)
        # C4: playlist_path: null hides the tab entirely.
        if self._playlist_path() is not None:
            playlists = tk.Frame(book, bg=BG)
            book.add(playlists, text="Playlists")
            self._build_playlists_tab(playlists)
        settings = tk.Frame(book, bg=BG)
        book.add(settings, text="Settings")
        self._build_settings_tab(settings)
        self._status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status_var, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 9)).pack(
                     fill="x", padx=14, pady=(0, 8))  # B6: 28px status bar

    def _build_player_tab(self, parent: tk.Widget) -> None:
        """B2-B5: now-playing + transport + volume + pool list.

        E2 adds a typed search box (the same ``play`` path a spoken query
        takes), E1 the recent-query strip under it, E5 the ``?`` cheat-sheet.
        """
        # E2: typed query -> the same handler a spoken `play` uses.  Voice is
        # still the primary input; this is only the second door to it.
        search = tk.Frame(parent, bg=BG)
        search.pack(fill="x", pady=(6, 2))
        self._search_var = tk.StringVar(value="")
        entry = tk.Entry(search, textvariable=self._search_var, bg="#1a2027",
                         fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=0, font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        entry.bind("<Return>", lambda _event: self._submit_search())
        tk.Button(search, text="Search", width=8,
                  command=self._submit_search).pack(side="left", padx=(6, 0))
        tk.Button(search, text="?", width=3,
                  command=self._show_cheatsheet).pack(side="left", padx=(6, 0))

        # E1: newest-first strip of the last spoken play queries (filled by
        # _refresh_recent on the poll tick, never rebuilt unless it changed).
        self._recent_box = tk.Frame(parent, bg=BG)
        self._recent_box.pack(fill="x", pady=(0, 2))

        self._now_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._now_var, bg=BG, fg=FG,
                 anchor="w", font=("Segoe UI", 12, "bold"),
                 wraplength=self._win_width - 60).pack(fill="x", pady=(6, 2))
        # B3: Play/Pause toggles the pause flag; the rest call player methods.
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 4))
        for text, action in (("⏯ Play/Pause", "play_pause"), ("⏹ Stop", "stop"),
                             ("⏮ Prev", "prev"), ("⏭ Next", "next"),
                             ("▶ Resume", "resume")):
            tk.Button(row, text=text, width=11,
                      command=lambda a=action: _player_transport(
                          self.ui_state.player, self.ui_state, a),
                      ).pack(side="left", padx=(0, 6))
        # B4: slider commits on release; +/- nudge by volume_step.
        vol = tk.Frame(parent, bg=BG)
        vol.pack(fill="x", pady=(0, 4))
        step = getattr(self.ui_state.player, "volume_step", 10)
        tk.Button(vol, text="-", width=3,
                  command=lambda: _player_step_volume(
                      self.ui_state.player, self.ui_state, -step),
                  ).pack(side="left")
        self._vol_var = tk.DoubleVar(value=50.0)
        scale = tk.Scale(vol, from_=0, to=130, orient="horizontal", bg=BG,
                         fg=FG, highlightthickness=0, showvalue=False,
                         length=220, variable=self._vol_var,
                         command=lambda _v: self._vol_var and self._vol_label
                         and self._vol_label.set(f"{float(_v):.0f}%"))
        scale.bind("<ButtonPress-1>", lambda _e: setattr(self, "_volume_dragging", True))
        scale.bind("<ButtonRelease-1>", self._on_volume_release)
        scale.pack(side="left", padx=8)
        tk.Button(vol, text="+", width=3,
                  command=lambda: _player_step_volume(
                      self.ui_state.player, self.ui_state, step),
                  ).pack(side="left")
        self._vol_label = tk.StringVar(value="50%")
        tk.Label(vol, textvariable=self._vol_label, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9), width=5).pack(side="left")
        tk.Button(vol, text="mute", width=5,
                  command=self._on_mute_toggle).pack(side="left", padx=(6, 0))
        # B5: listbox with scrollbar; click = jump via the same bounds check.
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(box, orient="vertical")
        self._player_list = tk.Listbox(box, bg="#1a2027", fg=FG,
                                       font=("Segoe UI", 10), height=5,
                                       yscrollcommand=scroll.set,
                                       activestyle="none")
        scroll.config(command=self._player_list.yview)
        scroll.pack(side="right", fill="y")
        self._player_list.pack(side="left", fill="both", expand=True)
        self._player_list.bind("<<ListboxSelect>>", self._on_list_select)

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
                                      values=[], width=26, state="readonly")
        self._pl_combo.pack(side="left")
        self._pl_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self._pl_reload_tracks())
        for text, command in (("New", self._pl_new), ("Rename", self._pl_rename),
                              ("Delete", self._pl_delete)):
            tk.Button(pick, text=text, command=command).pack(
                side="left", padx=(6, 0))

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self._pl_list = tk.Listbox(body, bg="#1a2027", fg=FG, bd=0,
                                   highlightthickness=0,
                                   selectbackground="#2d3f52",
                                   font=("Segoe UI", 9), activestyle="none")
        self._pl_list.pack(side="left", fill="both", expand=True)
        self._pl_list.bind("<Double-Button-1>", lambda _e: self._pl_play())

        side = tk.Frame(body, bg=BG)
        side.pack(side="left", fill="y", padx=(6, 0))
        for text, command in (("Play", self._pl_play),
                              ("Add current", self._pl_add_current),
                              ("Up", lambda: self._pl_move(-1)),
                              ("Down", lambda: self._pl_move(1)),
                              ("Remove", self._pl_remove)):
            tk.Button(side, text=text, width=11, command=command).pack(
                fill="x", pady=(0, 4))

        self._pl_url_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._pl_url_var, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(
                     fill="x", padx=6, pady=(4, 8))
        self._pl_msg = tk.StringVar(value="")
        tk.Label(parent, textvariable=self._pl_msg, bg=BG, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 8)).pack(fill="x", padx=6)
        self._pl_refresh()

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
        for track in self._pl_tracks:
            self._pl_list.insert("end",
                                 _truncate(track.get("title", ""), POOL_MAX_CHARS))

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
        """C2: play the highlighted entry; failures never kill the overlay."""
        index = self._pl_selected_index()
        player = self.ui_state.player
        if index is None or not self._pl_tracks or player is None:
            return
        track = self._pl_tracks[index]
        url = track.get("url") or ""
        title = track.get("title") or url
        try:
            player.command("playlist-clear", raise_on_error=False)
            player.load(url, "replace")
        except Exception as exc:
            self._pl_note(f"could not play {title!r}: {exc}", error=True)
            return
        self._pl_note(f"playing {title!r}")

    def _pl_add_current(self) -> None:
        store = self._store()
        name = self._pl_selected_name()
        player = self.ui_state.player
        if store is None or name is None or player is None:
            self._pl_note("nothing is playing", error=True)
            return
        title = player.current_title()
        try:
            url = player.command("get_property", "path", timeout=2.0,
                                 raise_on_error=False)
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

    def _pl_move(self, delta: int) -> None:
        store = self._store()
        name = self._pl_selected_name()
        index = self._pl_selected_index()
        if store is None or name is None or index is None:
            return
        target = index + delta
        try:
            store.reorder(name, index, target)
        except (ValueError, IndexError):
            return      # already at the edge: not an error worth reporting
        self._pl_reload_tracks()
        if self._pl_list is not None:
            self._pl_list.selection_set(target)

    # -- D: settings tab ---------------------------------------------------

    def _build_settings_tab(self, parent: tk.Widget) -> None:
        """D1/D2/E3/E4: a form over the live Config, saved back to YAML."""
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
        self._row(inner, "ui", "mode", ui.mode if ui else "compact", "str")
        self._row(inner, "ui", "overlay_width", ui.overlay_width if ui else 400, "int")
        self._row(inner, "ui", "overlay_height", ui.overlay_height if ui else 214, "int")
        self._row(inner, "ui", "always_on_top", ui.always_on_top if ui else True, "bool")
        self._row(inner, "ui", "show_playlist", ui.show_playlist if ui else True, "bool")
        self._row(inner, "ui", "hide_while_playing",
                  ui.hide_while_playing if ui else False, "bool")

        self._build_commands_editor(inner)   # D2
        self._build_settings_actions(inner)  # D3/D4/E4

    def _section(self, parent: tk.Widget, title: str) -> None:
        tk.Label(parent, text=title, bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(
                     fill="x", padx=6, pady=(10, 2), anchor="w")

    def _row(self, parent: tk.Widget, section: str, key: str, value,
             kind: str) -> None:
        """One labelled field; registers its variable for collection on save."""
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x", padx=6, pady=1)
        tk.Label(line, text=key, bg=BG, fg=FG_DIM, width=34, anchor="w",
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
        ttk.Combobox(line, textvariable=var, values=choices, width=28).pack(
            side="left")

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
            tk.Button(row, text=text, command=command).pack(
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
            specs.append({
                "action": action,
                "verbs": verbs,
                "takes_query": bool(query_var.get()),
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
                    path.write_text(backup, encoding="utf-8")
                except OSError:
                    LOGGER.exception("could not restore %s", path)
            self._settings_msg(f"not saved - {exc}")
            return
        self._settings_msg(
            f"saved to {path} - restart the daemon for voice-command, "
            "backend and microphone changes")

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

    def _on_volume_release(self, _event) -> None:
        """B4: slider drag finished -> set_volume() once, then resume polling."""
        self._volume_dragging = False
        if self._vol_var is not None:
            _player_set_volume(self.ui_state.player, self.ui_state,
                               self._vol_var.get())

    def _on_mute_toggle(self) -> None:
        """B4: mute -> 0, again -> the volume from before the mute.

        mpv has no mute flag the daemon tracks, so mute is just volume 0
        with the previous level remembered on the overlay.
        """
        player = self.ui_state.player
        if player is None:
            return
        try:
            current = float(player.volume())
        except Exception as exc:
            LOGGER.warning("player volume read failed: %s", exc)
            self.ui_state.set_error(f"player: {exc}")
            return
        if current > 0:
            self._mute_before = current
            _player_set_volume(player, self.ui_state, 0.0)
            self.ui_state.set_action("muted")
        else:
            _player_set_volume(player, self.ui_state,
                               self._mute_before if self._mute_before else 50.0)
            self.ui_state.set_action("unmuted")

    def _on_list_select(self, _event) -> None:
        """B5: listbox row -> same handler the compact pool rows use."""
        if self._player_list is None or self._drag_moved:
            return
        picked = self._player_list.curselection()
        if picked:
            self._on_pool_click(int(picked[0]))

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
        try:
            self.on_play_query(text)
        except Exception:  # the daemon owns failures; the overlay must not die
            LOGGER.exception("play-from-ui handler failed")
            return
        if self._search_var is not None:
            self._search_var.set("")

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
        self.attributes("-alpha", 1.0)
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
        frac = min(1.0, level / LEVEL_FULL)
        self._meter_frac += 0.35 * (frac - self._meter_frac)   # smooth
        width = int((self._win_width - 28) * self._meter_frac)
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

        # --- status line: last action, then what is playing ---
        self._action_var.set(self._status_line(snap, action_age))

        # --- result pool (compact labels) + B5 expanded listbox ---
        titles = self._pool_titles()
        for index, label in enumerate(self._pool_labels):
            if index < len(titles):
                text = _truncate(titles[index], POOL_MAX_CHARS)
                label.configure(text=f"{index + 1}. {text}", fg=FG)
            else:
                label.configure(text="", fg=FG_DIM)

        if self.expanded:
            # B2: now-playing line reuses _now_playing() verbatim (no new player
            # code) instead of rebuilding the title/position/volume string.
            if self._now_var is not None:
                self._now_var.set(self._now_playing() or "nothing playing")
            # B5: keep the scrolling list in step with the compact rows.
            if self._player_list is not None and titles != self._player_titles:
                self._player_titles = list(titles)
                selected = self._player_list.curselection()
                self._player_list.delete(0, "end")
                for pos, title in enumerate(titles, start=1):
                    self._player_list.insert(
                        "end", f"{pos}. {_truncate(title, POOL_MAX_CHARS)}")
                if selected and selected[0] < len(titles):
                    self._player_list.select_set(selected[0])
            # B4: volume polling is decimated (every PLAYER_POLL_EVERY ticks)
            # so mpv IPC stays off the 250 ms hot path; never fight a drag.
            self._tick += 1
            if self._tick % PLAYER_POLL_EVERY == 0:
                player = self.ui_state.player
                if player is not None and not self._volume_dragging:
                    try:
                        level = int(round(player.volume()))
                        if self._vol_var is not None:
                            self._vol_var.set(level)
                        if self._vol_label is not None:
                            self._vol_label.set(f"{level}%")
                    except Exception:
                        pass  # mpv restarting: keep the last slider position
            # B6: mic + backend + mpv + AEC, same tick, no new threads.
            if self._status_var is not None:
                self._status_var.set(
                    _player_status_line(self.ui_state.player, self.ui_state, snap))
            # E1: repaint the recent strip only when its list changed.
            self._refresh_recent(snap.get("recent") or [])

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

        # --- dim, then hide, once nothing happens for a while ---
        busy = max(snap["heard_ts"] if snap["heard"] else 0.0, snap["action_ts"], snap["error_ts"])
        if busy:
            quiet = now - busy
            if quiet > self._hide_after_s and not self._hidden:
                self.withdraw()
                self._hidden = True
            elif quiet <= self._hide_after_s:
                self.attributes("-alpha", FADE_ALPHA if quiet > self._fade_after_s else 1.0)
        elif now - self._born > self._hide_after_s and not self._hidden:
            self.withdraw()                  # idle since startup: hide too
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
        player = self.ui_state.player
        if player is None:
            return ""
        try:
            titles = player.pool_titles()
            title = player.current_title() or ""
            if not title:
                return f"{len(titles)} result(s) queued" if titles else ""
            position = titles.index(title) + 1 if title in titles else 0
            volume = int(round(player.volume()))
            return (
                f"{_truncate(title, TITLE_MAX_CHARS)} "
                f"[{position}/{len(titles)}]  vol {volume}%"
            )
        except Exception:  # mpv may be restarting; the overlay must not care
            return ""

    def _pool_titles(self) -> list[str]:
        player = self.ui_state.player
        if player is None:
            return []
        try:
            return player.pool_titles()
        except Exception:
            return []

    def _playing_now(self) -> bool:
        player = self.ui_state.player
        if player is None:
            return False
        try:
            return bool(player.playing)
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
        if self._thread is not None:
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
                      app_config=None, on_play_query=None) -> None:
    """Own the tkinter thread: build the window, run it, tear it down."""
    try:
        overlay = Overlay(state, config, on_pool_click, app_config, on_play_query)
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
        args=(state, config, on_pool_click, app_config, on_play_query),
        name="voiceyt-overlay",
        daemon=True,
    ).start()
    return tray

