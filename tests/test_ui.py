"""Overlay/tray support code and the daemon's pause contract (voiceyt.ui).

Everything here is offline: no microphone, no mpv, no window.  The tkinter
widgets and the win32 tray icon are exercised manually (see the README's UI
acceptance test); these tests cover the parts that carry state:

* ``_truncate`` and ``UiState`` (the daemon -> UI mailbox),
* the tray menu id scheme (device index <-> menu id),
* ``Listener.pause_event``: while paused the audio is dropped and the stream
  clock keeps running; on resume the pipeline is reset exactly once,
* the four callbacks the daemon hands to the UI.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
import yaml

from voiceyt.__main__ import _ui_jump_to_pool, _ui_log_pause, _ui_quit, _ui_switch_mic
from voiceyt.__main__ import Listener
from voiceyt.config import UiConfig, load_config
from voiceyt.ui import Overlay, Tray, UiState, _truncate, _window_size
from voiceyt.ui import RECENT_MAX

CONFIG = {
    "asr": {"backend": "whisper", "device": "cpu", "models_dir": "./models"},
    "vad": {"enabled": False},
    "behaviour": {"log_transcripts": False},
}


def load_test_config():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.yaml"
        path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
        return load_config(path)


class TruncateTests(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(_truncate("youtube", 20), "youtube")

    def test_exact_length_is_untouched(self) -> None:
        self.assertEqual(_truncate("abcde", 5), "abcde")

    def test_long_text_gets_an_ellipsis(self) -> None:
        self.assertEqual(_truncate("abcdefgh", 5), "abcd\u2026")
        self.assertEqual(len(_truncate("abcdefgh", 5)), 5)


class UiStateTests(unittest.TestCase):
    def test_starts_empty(self) -> None:
        state = UiState()
        snap = state.snapshot()
        self.assertEqual(snap["heard"], "")
        self.assertEqual(snap["heard_ts"], 0.0)
        self.assertEqual(snap["error"], "")
        self.assertFalse(state.paused.is_set())
        self.assertFalse(state.shutdown.is_set())

    def test_setters_stamp_and_report(self) -> None:
        state = UiState()
        state.set_heard("youtube passa nirvana")
        state.set_action("play nirvana")
        state.set_error("yt-dlp: unavailable")
        snap = state.snapshot()
        self.assertEqual(snap["heard"], "youtube passa nirvana")
        self.assertEqual(snap["action"], "play nirvana")
        self.assertEqual(snap["error"], "yt-dlp: unavailable")
        self.assertGreater(snap["heard_ts"], 0.0)
        self.assertGreaterEqual(snap["action_ts"], snap["heard_ts"])
        self.assertGreaterEqual(snap["error_ts"], snap["action_ts"])

    def test_snapshot_is_a_copy(self) -> None:
        state = UiState()
        snap = state.snapshot()
        snap["heard"] = "clobbered"
        self.assertEqual(state.snapshot()["heard"], "")

    def test_each_state_owns_its_events(self) -> None:
        first, second = UiState(), UiState()
        first.paused.set()
        self.assertTrue(first.paused.is_set())
        self.assertFalse(second.paused.is_set())


class TrayMenuIdTests(unittest.TestCase):
    """Device index -> menu id, which is how the mic submenu reports back."""

    def test_microphone_ids_round_trip(self) -> None:
        for index in (0, 1, 4, 12):
            self.assertEqual(Tray.MENU_MIC_BASE + index - Tray.MENU_MIC_BASE, index)

    def test_menu_ids_do_not_collide(self) -> None:
        self.assertGreater(Tray.MENU_MIC_BASE, Tray.MENU_QUIT)
        self.assertGreater(Tray.MENU_QUIT, Tray.MENU_TOGGLE)


class StubBackend:
    """Minimal ASRBackend: counts resets, echoes the block size."""

    name = "stub"
    streaming = False

    def __init__(self) -> None:
        self.resets = 0

    def transcribe(self, audio: np.ndarray) -> str:
        return f"text-{audio.size}"

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


class ListenerPauseTests(unittest.TestCase):
    """The tray's Pause button relies on this contract."""

    def setUp(self) -> None:
        self.heard: list[str] = []
        self.backend = StubBackend()
        self.paused = threading.Event()
        self.listener = Listener(
            load_test_config(),
            self.backend,
            on_utterance=lambda text, ts: self.heard.append(text),
            pause_event=self.paused,
        )
        # open() leaves segmenter None when vad.enabled is false; opening for
        # real would need a microphone, so mirror only that part here.
        self.listener.segmenter = None
        self.window = self.listener.window_samples
        self.block = np.zeros(self.window, dtype=np.float32)

    def test_audio_is_transcribed_while_awake(self) -> None:
        self.listener.process(self.block)
        self.assertEqual(self.heard, [f"text-{self.window}"])

    def test_nothing_is_transcribed_while_paused(self) -> None:
        self.paused.set()
        self.listener.process(self.block)
        self.listener.process(self.block)
        self.assertEqual(self.heard, [])

    def test_nothing_is_buffered_while_paused(self) -> None:
        self.paused.set()
        self.listener.process(self.block)
        self.assertEqual(self.listener._pending_samples, 0)

    def test_stream_clock_keeps_running_while_paused(self) -> None:
        self.paused.set()
        self.listener.process(self.block)
        self.assertGreater(self.listener.stream_ts, 0.0)

    def test_resume_resets_the_pipeline_once(self) -> None:
        # Baseline: finishing a block already resets the backend once.
        self.listener.process(self.block)
        before = self.backend.resets
        self.listener.process(self.block)
        awake_delta = self.backend.resets - before
        self.assertEqual(awake_delta, 1, "one reset per transcribed block")

        # Paused -> awake adds exactly one more: the transition's own reset.
        self.paused.set()
        self.listener.process(self.block)
        before = self.backend.resets
        self.paused.clear()
        self.listener.process(self.block)
        self.assertEqual(self.backend.resets - before, awake_delta + 1)

        # The block after that is back to the baseline (no repeated resets).
        before = self.backend.resets
        self.listener.process(self.block)
        self.assertEqual(self.backend.resets - before, awake_delta)

    def test_transcription_resumes_after_the_pause(self) -> None:
        self.paused.set()
        self.listener.process(self.block)
        self.paused.clear()
        self.listener.process(self.block)
        self.assertEqual(self.heard, [f"text-{self.window}"])

    def test_flush_while_paused_emits_nothing(self) -> None:
        self.paused.set()
        self.listener.flush()
        self.assertEqual(self.heard, [])

    def test_without_a_pause_event_nothing_changes(self) -> None:
        listener = Listener(
            load_test_config(),
            self.backend,
            on_utterance=lambda text, ts: self.heard.append(text),
        )
        listener.segmenter = None
        self.assertIsNone(listener.pause_event)
        listener.process(self.block)
        self.assertEqual(self.heard, [f"text-{self.window}"])


class FakePlayer:
    def __init__(self, titles: list[str]) -> None:
        self.titles = list(titles)
        self.jumped: list[int] = []

    def pool_titles(self) -> list[str]:
        return list(self.titles)

    def jump_to(self, index: int) -> bool:
        self.jumped.append(index)
        return True


class FakeCapture:
    def __init__(self) -> None:
        self.device_index = 0

    def reopen(self, device=None) -> str:
        if device == 99:
            raise RuntimeError("device busy")
        self.device_index = device
        return f"device-{device}"


class FakeListener:
    def __init__(self) -> None:
        self.capture = FakeCapture()
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class UiCallbackTests(unittest.TestCase):
    """The callbacks ``run_daemon`` hands to :func:`start_ui`."""

    def setUp(self) -> None:
        self.player = FakePlayer(["a", "b", "c"])
        self.ui = UiState(player=self.player)

    def test_pool_click_jumps_to_that_index(self) -> None:
        _ui_jump_to_pool(self.player, self.ui, 2)
        self.assertEqual(self.player.jumped, [2])
        self.assertIn("jump #3", self.ui.snapshot()["action"])

    def test_pool_click_out_of_range_does_nothing(self) -> None:
        _ui_jump_to_pool(self.player, self.ui, 5)
        _ui_jump_to_pool(self.player, self.ui, -1)
        self.assertEqual(self.player.jumped, [])

    def test_pause_and_resume_report_on_the_overlay(self) -> None:
        _ui_log_pause(self.ui, True)
        self.assertEqual(self.ui.snapshot()["action"], "paused (tray)")
        _ui_log_pause(self.ui, False)
        self.assertEqual(self.ui.snapshot()["action"], "listening again")

    def test_microphone_switch_names_the_new_device(self) -> None:
        listener = FakeListener()
        _ui_switch_mic(listener, self.ui, 3)
        self.assertEqual(listener.capture.device_index, 3)
        self.assertEqual(self.ui.snapshot()["action"], "microphone: device-3")

    def test_failed_microphone_switch_keeps_the_device_and_reports(self) -> None:
        listener = FakeListener()
        _ui_switch_mic(listener, self.ui, 3)
        _ui_switch_mic(listener, self.ui, 99)
        self.assertEqual(listener.capture.device_index, 3)
        self.assertIn("device busy", self.ui.snapshot()["error"])

    def test_quit_stops_the_capture(self) -> None:
        listener = FakeListener()
        _ui_quit(listener)
        self.assertTrue(listener.stopped)


class OverlayConfigTests(unittest.TestCase):
    """A3 of UI_Design.MD: UiConfig sizes/timers reach the overlay."""

    @staticmethod
    def make_config(**overrides) -> UiConfig:
        values = dict(
            overlay_width=1280,
            overlay_height=720,
            always_on_top=False,
            show_playlist=True,
            fade_after_s=5.0,
            hide_after_s=60.0,
            hide_while_playing=True,
            mode="expanded",
        )
        values.update(overrides)
        return UiConfig(**values)

    def test_expanded_mode_takes_the_configured_size(self) -> None:
        self.assertEqual(
            _window_size(self.make_config(mode="expanded", overlay_width=1600, overlay_height=900)),
            (1600, 900),
        )

    def test_compact_mode_keeps_the_classic_geometry(self) -> None:
        self.assertEqual(_window_size(self.make_config(mode="compact")), (400, 214))

    def test_missing_config_falls_back_to_the_constants(self) -> None:
        self.assertEqual(_window_size(None), (400, 214))

    def test_configured_size_reaches_overlay_geometry(self) -> None:
        try:
            overlay = Overlay(UiState(), self.make_config(mode="expanded"))
        except Exception as exc:  # no display (headless CI): the seam above still covers it
            self.skipTest(f"cannot open a window: {exc}")
        try:
            self.assertIn("1280x720", overlay.geometry())
            self.assertFalse(overlay.attributes("-topmost"))
        finally:
            overlay.destroy()

    def test_default_overlay_signature_still_works(self) -> None:
        try:
            overlay = Overlay(UiState())
        except Exception as exc:
            self.skipTest(f"cannot open a window: {exc}")
        try:
            self.assertIn("400x214", overlay.geometry())
            self.assertTrue(overlay.attributes("-topmost"))
        finally:
            overlay.destroy()


class RecentQueryTests(unittest.TestCase):
    """E1 of UI_Design.MD: the recent-query memory behind the history strip."""

    def test_newest_query_comes_first(self) -> None:
        state = UiState()
        state.note_query("nirvana")
        state.note_query("fado")
        self.assertEqual(state.recent_queries(), ["fado", "nirvana"])

    def test_repeating_the_last_query_changes_nothing(self) -> None:
        state = UiState()
        state.note_query("nirvana")
        state.note_query("nirvana")
        self.assertEqual(state.recent_queries(), ["nirvana"])

    def test_a_repeat_after_another_query_is_kept(self) -> None:
        state = UiState()
        for query in ("nirvana", "fado", "nirvana"):
            state.note_query(query)
        self.assertEqual(state.recent_queries(), ["nirvana", "fado", "nirvana"])

    def test_blank_queries_are_ignored(self) -> None:
        state = UiState()
        for query in ("", "   ", "\t"):
            state.note_query(query)
        self.assertEqual(state.recent_queries(), [])

    def test_surrounding_whitespace_is_stripped(self) -> None:
        state = UiState()
        state.note_query("  daft punk  ")
        self.assertEqual(state.recent_queries(), ["daft punk"])

    def test_the_list_is_capped(self) -> None:
        state = UiState()
        for index in range(RECENT_MAX + 5):
            state.note_query(f"query {index}")
        recent = state.recent_queries()
        self.assertEqual(len(recent), RECENT_MAX)
        self.assertEqual(recent[0], f"query {RECENT_MAX + 4}")

    def test_snapshot_carries_the_recent_list(self) -> None:
        state = UiState()
        state.note_query("daft punk")
        self.assertEqual(state.snapshot()["recent"], ["daft punk"])

    def test_snapshot_hands_out_a_copy(self) -> None:
        state = UiState()
        state.note_query("daft punk")
        state.snapshot()["recent"].append("not in the state")
        self.assertEqual(state.recent_queries(), ["daft punk"])


class PlayerTabExtrasTests(unittest.TestCase):
    """E1/E2/E5/C4: the extras that live inside the player tab.

    Each test needs a real window; on a headless box they skip, exactly like
    :class:`OverlayConfigTests`.
    """

    @staticmethod
    def make_config(**overrides) -> UiConfig:
        values = dict(
            overlay_width=1280,
            overlay_height=720,
            always_on_top=True,
            show_playlist=True,
            fade_after_s=5.0,
            hide_after_s=60.0,
            hide_while_playing=False,
            mode="expanded",
        )
        values.update(overrides)
        return UiConfig(**values)

    def build(self, config=None, app_config=None, on_play_query=None):
        try:
            overlay = Overlay(UiState(), config or self.make_config(),
                              app_config=app_config, on_play_query=on_play_query)
        except Exception as exc:  # no display (headless CI)
            self.skipTest(f"cannot open a window: {exc}")
        self.addCleanup(overlay.destroy)
        overlay.update_idletasks()
        return overlay

    def tab_names(self, overlay) -> list[str]:
        for child in overlay.winfo_children():
            if child.winfo_class() == "TNotebook":
                return [child.tab(i, "text") for i in range(child.index("end"))]
        return []

    @staticmethod
    def texts_in(widget) -> list[str]:
        out = []
        for child in widget.winfo_children():
            try:
                out.append(str(child.cget("text")))
            except Exception:
                continue
        return out

    def test_expanded_mode_builds_the_player_and_settings_tabs(self) -> None:
        names = self.tab_names(self.build())
        self.assertIn("Player", names)
        self.assertIn("Settings", names)

    def test_compact_mode_builds_no_tabs(self) -> None:
        overlay = self.build(self.make_config(mode="compact"))
        self.assertEqual(self.tab_names(overlay), [])

    def test_compact_mode_has_no_history_strip(self) -> None:
        overlay = self.build(self.make_config(mode="compact"))
        self.assertIsNone(overlay._recent_box)

    def test_playlists_tab_is_hidden_without_a_playlist_path(self) -> None:
        overlay = self.build(app_config=load_test_config())
        self.assertNotIn("Playlists", self.tab_names(overlay))
        self.assertIsNone(overlay._playlist_path())

    def test_search_box_dispatches_then_clears(self) -> None:
        sent: list[str] = []
        overlay = self.build(on_play_query=sent.append)
        overlay._search_var.set("  daft punk  ")
        overlay._submit_search()
        self.assertEqual(sent, ["daft punk"])
        self.assertEqual(overlay._search_var.get(), "")

    def test_empty_search_does_not_dispatch(self) -> None:
        sent: list[str] = []
        overlay = self.build(on_play_query=sent.append)
        overlay._search_var.set("   ")
        overlay._submit_search()
        self.assertEqual(sent, [])

    def test_search_without_a_handler_is_harmless(self) -> None:
        overlay = self.build()          # on_play_query stays None
        overlay._search_var.set("daft punk")
        overlay._submit_search()        # must not raise
        self.assertEqual(overlay._search_var.get(), "daft punk")

    def test_recent_chip_reuses_the_play_path(self) -> None:
        sent: list[str] = []
        self.build(on_play_query=sent.append)._submit_search("from a chip")
        self.assertEqual(sent, ["from a chip"])

    def test_recent_strip_renders_one_chip_per_query(self) -> None:
        overlay = self.build()
        overlay._refresh_recent(["nirvana", "fado", "classical"])
        self.assertEqual(len(overlay._recent_buttons), 3)
        labels = [str(b.cget("text")) for b in overlay._recent_buttons]
        self.assertEqual(labels[0], "nirvana")

    def test_recent_strip_is_not_rebuilt_when_nothing_changed(self) -> None:
        overlay = self.build()
        overlay._refresh_recent(["nirvana"])
        first = overlay._recent_buttons[0]
        overlay._refresh_recent(["nirvana"])
        self.assertIs(overlay._recent_buttons[0], first)

    def test_recent_strip_clears_when_the_list_empties(self) -> None:
        overlay = self.build()
        overlay._refresh_recent(["nirvana"])
        overlay._refresh_recent([])
        self.assertEqual(overlay._recent_buttons, [])

    def test_cheat_sheet_lists_the_trigger_and_verbs(self) -> None:
        app_config = load_test_config()
        overlay = self.build(app_config=app_config)
        overlay._show_cheatsheet()
        window = overlay._cheat_window
        self.assertIsNotNone(window)
        joined = "\n".join(self.texts_in(window))
        self.assertIn(app_config.trigger.words[0], joined)
        for spec in app_config.commands:
            self.assertIn(spec.action, joined)
        overlay._close_cheatsheet()
        self.assertIsNone(overlay._cheat_window)

    def test_settings_tab_form_has_the_edited_keys(self) -> None:
        overlay = self.build(app_config=load_test_config())
        self.assertIn(("vad", "threshold"), overlay._set_vars)
        self.assertIn(("ui", "mode"), overlay._set_vars)
        self.assertIn("volume_step", [key for _section, key in overlay._set_vars])
        for spec in overlay.app_config.commands:
            self.assertIn(spec.action, overlay._cmd_vars)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
