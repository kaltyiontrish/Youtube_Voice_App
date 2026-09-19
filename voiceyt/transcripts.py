"""Transcript logging and offline replay (plan2.md §9, M2 -> M3).

Every transcript is appended to ``behaviour.log_path`` when
``log_transcripts: true``.  Tuning is impossible without that file: it is the
test set the matcher is validated against, and ``--replay-log`` is how the M3
acceptance test runs ("zero commands fire on ordinary conversation").

Format - one record per line, tab separated so it stays greppable:

    2026-09-19T18:04:12.345<TAB>parakeet<TAB>final<TAB>youtube passa nirvana
    2026-09-19T18:04:14.101<TAB>nemotron<TAB>partial<TAB>youtube passa nir
    2026-09-19T18:04:15.002<TAB>-<TAB>command<TAB>action=play query=nirvana
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import Config
from .matcher import Command, Matcher, State

LOGGER = logging.getLogger(__name__)

FINAL = "final"
PARTIAL = "partial"
COMMAND = "command"
SEPARATOR = "\t"


@dataclass(frozen=True)
class LogEntry:
    """One line of the transcript journal."""

    ts: float
    backend: str
    tag: str
    text: str

    @property
    def feeds_matcher(self) -> bool:
        """Only completed utterances may drive the state machine."""
        return self.tag == FINAL


class TranscriptLog:
    """Append-only transcript journal; a no-op when logging is disabled."""

    def __init__(self, path: Path | str, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._handle = None

    def open(self) -> "TranscriptLog":
        if not self.enabled or self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def _write(self, tag: str, backend: str, text: str, ts: float | None) -> None:
        if not self.enabled:
            return
        if self._handle is None:
            self.open()
        handle = self._handle
        if handle is None:  # pragma: no cover - open() failed
            return
        moment = datetime.fromtimestamp(ts) if ts else datetime.now()
        stamp = moment.isoformat(timespec="milliseconds")
        clean = " ".join(str(text).split())
        handle.write(f"{stamp}{SEPARATOR}{backend}{SEPARATOR}{tag}{SEPARATOR}{clean}\n")
        handle.flush()

    def write(self, text: str, backend: str, tag: str = FINAL, ts: float | None = None) -> None:
        """Record one transcript (an utterance, or a streaming partial)."""
        self._write(tag, backend, text, ts)

    def note(self, message: str, ts: float | None = None) -> None:
        """Record a non-transcript event, e.g. a command that fired."""
        self._write(COMMAND, "-", message, ts)

    def __enter__(self) -> "TranscriptLog":
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def parse_timestamp(value: str) -> float | None:
    """ISO timestamp -> epoch seconds, or ``None`` when unparsable."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def read_entries(path: Path | str) -> Iterator[LogEntry]:
    """Yield every well-formed record; malformed lines are counted and skipped."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    skipped = 0
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split(SEPARATOR)
            if len(parts) < 4:
                skipped += 1
                continue
            stamp, backend, tag, text = parts[0], parts[1], parts[2], SEPARATOR.join(parts[3:])
            ts = parse_timestamp(stamp)
            if ts is None:
                skipped += 1
                continue
            yield LogEntry(ts=ts, backend=backend, tag=tag, text=text)
    if skipped:
        LOGGER.warning("skipped %d malformed line(s) in %s", skipped, source)


def replay(
    config: Config, entries: list[LogEntry]
) -> tuple[list[tuple[LogEntry, Command]], list[str]]:
    """Run recorded transcripts through a fresh matcher (M3 acceptance test).

    Timestamps come from the journal: when the gap between two utterances exceeds
    ``trigger.silence_end_ms`` the matcher is told that much trailing silence
    elapsed, which is what the live VAD would have reported.  ``action=play`` and
    ``action=stop`` notes are used to reproduce the playback state, so the
    same-utterance guard is exercised exactly as it was live.
    """
    matcher = Matcher(
        trigger_words=config.trigger.words,
        trigger_window_ms=config.trigger.trigger_window_ms,
        silence_end_ms=config.trigger.silence_end_ms,
        query_max_ms=config.trigger.query_max_ms,
        commands=config.commands,
        require_same_utterance_while_playing=(
            config.behaviour.require_same_utterance_while_playing
        ),
    )
    firings: list[tuple[LogEntry, Command]] = []
    previous_ts: float | None = None
    playing = False

    for entry in entries:
        gap_ms = 0.0 if previous_ts is None else (entry.ts - previous_ts) * 1000.0
        previous_ts = entry.ts

        if entry.tag == COMMAND:
            if "action=play" in entry.text:
                playing = True
            elif "action=stop" in entry.text:
                playing = False
            matcher.set_playing(playing)
            continue

        silence_ms = gap_ms if gap_ms >= config.trigger.silence_end_ms else 0.0
        for command in matcher.tick(entry.ts, silence_ms):
            firings.append((entry, command))
        if not entry.feeds_matcher:
            continue
        for command in matcher.feed(entry.text, entry.ts):
            firings.append((entry, command))

    transitions: list[str] = []
    for event in matcher.trace:
        source = event.source.value if isinstance(event.source, State) else "-"
        suffix = f"  |{event.text[:60]}" if event.text else ""
        transitions.append(
            f"{datetime.fromtimestamp(event.ts).isoformat(timespec='milliseconds')} "
            f"{source:>10} -> {event.target.value:<10} {event.detail}{suffix}"
        )
    return firings, transitions