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
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

LOGGER = logging.getLogger(__name__)

# ---- look & feel -----------------------------------------------------------

FADE_AFTER_S = 30.0      # quiet time before the overlay dims
HIDE_AFTER_S = 120.0     # quiet time before the overlay goes away
FADE_ALPHA = 0.35        # opacity while dimmed
POLL_MS = 250            # overlay refresh period
HEARD_LIFETIME_S = 8.0   # how long the big "heard" line stays on screen
ERROR_LIFETIME_S = 8.0   # how long an error keeps the dot red
ACTION_LIFETIME_S = 9.0  # how long the last action label stays
HEARD_MAX_CHARS = 110    # about two wrapped lines
TITLE_MAX_CHARS = 46
POOL_MAX_CHARS = 44
POOL_ROWS = 5
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



# ---- overlay ---------------------------------------------------------------


class Overlay(tk.Tk):
    """Frameless always-on-top window that renders a :class:`UiState`.

    Built and ticked on one thread (see :func:`start_ui`); it only ever reads
    the state snapshot, so daemon threads never touch a widget.
    """

    def __init__(self, state: UiState, on_pool_click=None):
        super().__init__()
        self.ui_state = state
        self.on_pool_click = on_pool_click
        self._hidden = True
        self._build_ui()
        self.after(POLL_MS, self._poll_ui)

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.overrideredirect(True)          # no title bar, no taskbar entry
        self.attributes("-topmost", True)
        self.configure(bg=BG)
        self._born = time.monotonic()        # for the startup quiet timer
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}{WINDOW_POS}")
        self.show()                          # visible at startup ("listening")

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
            wraplength=WINDOW_WIDTH - 60,
        )
        self._heard_label.pack(side="left", fill="x", expand=True)

        self._action_var = tk.StringVar()
        self._action_label = tk.Label(
            self,
            textvariable=self._action_var,
            fg=FG_DIM,
            bg=BG,
            justify="left",
            anchor="w",
            font=tkfont.Font(family="Segoe UI", size=9),
            wraplength=WINDOW_WIDTH - 28,
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
            label.bind("<Button-1>", lambda _event, index=row: self._on_pool_click(index))
            self._pool_labels.append(label)

    # -- events ----------------------------------------------------------

    def _on_pool_click(self, index: int) -> None:
        if self.on_pool_click is None:
            return
        try:
            self.on_pool_click(index)
        except Exception:
            LOGGER.exception("pool click handler failed")

    def show(self) -> None:
        """Reveal the overlay (a new utterance, an error, startup)."""
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

        # --- result pool ---
        titles = self._pool_titles()
        for index, label in enumerate(self._pool_labels):
            if index < len(titles):
                text = _truncate(titles[index], POOL_MAX_CHARS)
                label.configure(text=f"{index + 1}. {text}", fg=FG)
            else:
                label.configure(text="", fg=FG_DIM)

        # --- dim, then hide, once nothing happens for a while ---
        busy = max(snap["heard_ts"] if snap["heard"] else 0.0, snap["action_ts"], snap["error_ts"])
        if busy:
            quiet = now - busy
            if quiet > HIDE_AFTER_S and not self._hidden:
                self.withdraw()
                self._hidden = True
            elif quiet <= HIDE_AFTER_S:
                self.attributes("-alpha", FADE_ALPHA if quiet > FADE_AFTER_S else 1.0)
        elif now - self._born > HIDE_AFTER_S and not self._hidden:
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
        return label or playing or "listening"

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


def _overlay_mainloop(state: UiState, on_pool_click) -> None:
    """Own the tkinter thread: build the window, run it, tear it down."""
    try:
        overlay = Overlay(state, on_pool_click)
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
    on_pool_click=None,
    on_pause=None,
    on_resume=None,
    on_mic_pick=None,
    on_quit=None,
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
        args=(state, on_pool_click),
        name="voiceyt-overlay",
        daemon=True,
    ).start()
    return tray

