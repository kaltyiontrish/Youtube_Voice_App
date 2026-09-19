"""Trigger/verb state machine (plan2.md §6.2).

    IDLE       --trigger word seen-------------------> ARMED
    ARMED      --verb seen within trigger_window_ms--> COLLECTING (verb takes a query)
    ARMED      --verb seen within trigger_window_ms--> FIRE       (verb takes no query)
    ARMED      --any non-verb word-------------------> IDLE
    ARMED      --trigger_window_ms elapsed-----------> IDLE
    COLLECTING --silence for silence_end_ms----------> FIRE
    COLLECTING --query_max_ms elapsed----------------> FIRE
    COLLECTING --new trigger word--------------------> ARMED (restart)

Batch backends hand over one complete utterance, so the common case
("youtube passa nirvana") fires with no timer at all: the query is already
non-empty when the utterance ends.  The timers only matter when the speaker
pauses mid-command.

The trigger word alone never does anything, and while playback is running the
trigger and the verb must come from the *same* utterance - the cheap
anti-lyrics guard from plan2.md §8 step 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .config import CommandSpec
from .text import find_phrase, normalize, phrase_tokens, sort_longest_first, tokenize


class State(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    COLLECTING = "COLLECTING"


@dataclass(frozen=True)
class Command:
    """A recognised command, ready for ``commands.CommandRunner``."""

    action: str
    query: str
    ts: float
    text: str
    reason: str


@dataclass(frozen=True)
class TraceEvent:
    ts: float
    source: State | None
    target: State
    detail: str
    text: str


@dataclass(frozen=True)
class VerbRule:
    action: str
    takes_query: bool
    tokens: tuple[str, ...]


class Matcher:
    """Feed it transcripts and timestamps, get :class:`Command` objects."""

    def __init__(
        self,
        *,
        trigger_words: Sequence[str],
        trigger_window_ms: int,
        silence_end_ms: int,
        query_max_ms: int,
        commands: Sequence[CommandSpec],
        require_same_utterance_while_playing: bool = True,
    ) -> None:
        self.trigger_words = tuple(trigger_words)
        self.trigger_window_ms = int(trigger_window_ms)
        self.silence_end_ms = int(silence_end_ms)
        self.query_max_ms = int(query_max_ms)
        self.require_same_utterance_while_playing = bool(require_same_utterance_while_playing)

        self._triggers = sort_longest_first([phrase_tokens(word) for word in self.trigger_words])
        self._rules = sorted(
            (
                VerbRule(
                    action=spec.action,
                    takes_query=spec.takes_query,
                    tokens=phrase_tokens(verb),
                )
                for spec in commands
                for verb in spec.verbs
            ),
            key=lambda rule: -len(rule.tokens),
        )

        self.state: State = State.IDLE
        self._trigger_ts = 0.0
        self._query_start = 0.0
        self._query: list[str] = []
        self._rule: VerbRule | None = None
        self._trigger_utterance = 0
        self._utterance = 0
        self._playing = False
        self._trace: list[TraceEvent] = []

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    @property
    def trace(self) -> list[TraceEvent]:
        """Transition history, used by ``--replay-log``."""
        return list(self._trace)

    @property
    def pending_query(self) -> str:
        return " ".join(self._query).strip()

    def reset(self) -> None:
        self.state = State.IDLE
        self._query = []
        self._rule = None

    def set_playing(self, playing: bool) -> None:
        """Tell the matcher whether mpv is playing right now (plan2.md §8 step 3)."""
        self._playing = bool(playing)

    def feed(self, text: str, ts: float) -> list[Command]:
        """Process one transcript whose utterance ended at *ts* (seconds)."""
        self._utterance += 1
        commands = self._expire(ts, 0.0, text)
        tokens = tokenize(normalize(text))
        if tokens:
            commands += self._consume(tokens, ts, text)
        return commands

    def tick(self, ts: float, silence_ms: float = 0.0) -> list[Command]:
        """Advance the timers; *silence_ms* is the trailing silence length."""
        return self._expire(ts, silence_ms, "")

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _transition(
        self, source: State | None, target: State, detail: str, text: str, ts: float
    ) -> None:
        self.state = target
        self._trace.append(
            TraceEvent(ts=ts, source=source, target=target, detail=detail, text=text)
        )

    def _expire(self, ts: float, silence_ms: float, text: str) -> list[Command]:
        """Apply the three timers before new text is processed."""
        source = self.state
        if source is State.ARMED:
            if (ts - self._trigger_ts) * 1000.0 > self.trigger_window_ms:
                self._rule = None
                self._transition(
                    source,
                    State.IDLE,
                    f"trigger window elapsed ({self.trigger_window_ms} ms)",
                    text,
                    ts,
                )
        elif source is State.COLLECTING:
            if silence_ms >= self.silence_end_ms:
                return self._fire(
                    f"silence {int(silence_ms)} ms >= {self.silence_end_ms} ms", ts, text
                )
            if (ts - self._query_start) * 1000.0 > self.query_max_ms:
                return self._fire(f"query window elapsed ({self.query_max_ms} ms)", ts, text)
        return []

    def _consume(self, tokens: list[str], ts: float, text: str) -> list[Command]:
        out: list[Command] = []
        index = 0
        while index < len(tokens):
            if self.state is State.IDLE:
                hit = find_phrase(tokens, self._triggers, index)
                if hit is None or hit[0] != index:
                    index = index + 1 if hit is None else hit[0]
                    continue
                self._trigger_ts = ts
                self._trigger_utterance = self._utterance
                self._rule = None
                self._transition(State.IDLE, State.ARMED, "trigger word", text, ts)
                index += hit[1]
                continue

            if self.state is State.ARMED:
                hit = find_phrase(tokens, self._triggers, index)
                if hit is not None and hit[0] == index:
                    self._trigger_ts = ts
                    self._trigger_utterance = self._utterance
                    self._rule = None
                    self._transition(State.ARMED, State.ARMED, "trigger word restarts", text, ts)
                    index += hit[1]
                    continue
                verb = self._match_verb(tokens, index)
                if verb is not None:
                    rule, length = verb
                    if self._guard_blocks(ts, text):
                        index += length
                        continue
                    self._rule = rule
                    if rule.takes_query:
                        self._query = []
                        self._query_start = ts
                        self._transition(
                            State.ARMED, State.COLLECTING, f"verb {rule.action!r}", text, ts
                        )
                    else:
                        self._transition(
                            State.ARMED, State.IDLE, f"verb {rule.action!r}", text, ts
                        )
                        out.append(
                            Command(
                                action=rule.action,
                                query="",
                                ts=ts,
                                text=text,
                                reason="verb without query",
                            )
                        )
                        self._rule = None
                    index += length
                    continue
                # "vi um video no youtube ontem": 'ontem' is not a verb -> IDLE.
                self._rule = None
                self._transition(
                    State.ARMED, State.IDLE, f"non-verb word {tokens[index]!r}", text, ts
                )
                index += 1
                continue

            # COLLECTING: a fresh trigger restarts, everything else is query text.
            hit = find_phrase(tokens, self._triggers, index)
            if hit is not None and hit[0] == index:
                self._trigger_ts = ts
                self._trigger_utterance = self._utterance
                self._rule = None
                self._query = []
                self._transition(State.COLLECTING, State.ARMED, "new trigger word", text, ts)
                index += hit[1]
                continue
            self._query.extend(tokens[index:])
            index = len(tokens)
            if self._query:
                # Fast path: the whole command arrived in one utterance.
                out += self._fire("query completed in one utterance", ts, text)
        return out

    def _match_verb(self, tokens: list[str], index: int) -> tuple[VerbRule, int] | None:
        """Longest-first verb lookup; multi-word verbs win over their prefixes."""
        for rule in self._rules:
            if tuple(tokens[index : index + len(rule.tokens)]) == rule.tokens:
                return rule, len(rule.tokens)
        return None

    def _guard_blocks(self, ts: float, text: str) -> bool:
        """plan2.md §8 step 3: while playing, trigger and verb must share an utterance."""
        if not (self._playing and self.require_same_utterance_while_playing):
            return False
        if self._trigger_utterance == self._utterance:
            return False
        self._rule = None
        self._transition(
            State.ARMED,
            State.IDLE,
            "playing: verb arrived in a later utterance than the trigger",
            text,
            ts,
        )
        return True

    def _fire(self, reason: str, ts: float, text: str) -> list[Command]:
        source = self.state
        rule = self._rule
        query = self.pending_query
        self._query = []
        self._rule = None
        self._transition(source, State.IDLE, f"fire: {reason}", text, ts)
        if rule is None:
            return []
        if rule.takes_query and not query:
            self._trace.append(
                TraceEvent(
                    ts=ts,
                    source=source,
                    target=State.IDLE,
                    detail="fire skipped: empty query",
                    text=text,
                )
            )
            return []
        return [Command(action=rule.action, query=query, ts=ts, text=text, reason=reason)]