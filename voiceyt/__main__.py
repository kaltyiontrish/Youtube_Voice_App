"""Executable compatibility shim for the voiceyt CLI."""

from .cli_app import build_parser, configure_logging, main
from .daemon import (
    _ui_browse_query,
    _ui_jump_to_pool,
    _ui_log_pause,
    _ui_play_playlist,
    _ui_play_query,
    _ui_quit,
    _ui_switch_mic,
)
from .listener import Listener

if __name__ == "__main__":
    raise SystemExit(main())
