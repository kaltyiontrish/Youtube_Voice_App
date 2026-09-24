"""The trigger state machine (plan2.md §6.2).

Feed times are seconds on a monotonic-ish clock; the matcher only ever looks at
differences, so plain floats are fine here.
"""

from __future__ import annotations

import unittest

from voiceyt.config import CommandSpec
from voiceyt.matcher import Matcher, State

COMMANDS = (
    CommandSpec(action="play", verbs=("passa", "toca", "poe", "mete"), takes_query=True),
    CommandSpec(
        action="next", verbs=("proximo", "seguinte", "outra", "salta"), takes_query=False
    ),
    CommandSpec(action="prev", verbs=("anterior", "volta"), takes_query=False),
    CommandSpec(action="stop", verbs=("para", "pausa", "chega"), takes_query=False),
    CommandSpec(action="jump", verbs=("jump", "salta para"), takes_query=True),
    CommandSpec(
        action="volume_up", verbs=("mais alto", "aumenta", "sobe o som"), takes_query=False
    ),
    CommandSpec(
        action="volume_down", verbs=("mais baixo", "baixa", "baixa o som"), takes_query=False
    ),
)


def make_matcher(**overrides: object) -> Matcher:
    settings: dict[str, object] = {
        "trigger_words": ("youtube", "you tube", "iutube"),
        "trigger_window_ms": 1500,
        "silence_end_ms": 800,
        "query_max_ms": 6000,
        "commands": COMMANDS,
        "require_same_utterance_while_playing": True,
    }
    settings.update(overrides)
    return Matcher(**settings)  # type: ignore[arg-type]


class TriggerTests(unittest.TestCase):
    def test_trigger_alone_does_nothing(self) -> None:
        matcher = make_matcher()
        self.assertEqual(matcher.feed("youtube", 0.0), [])
        self.assertIs(matcher.state, State.ARMED)

    def test_similar_word_does_not_arm(self) -> None:
        matcher = make_matcher()
        self.assertEqual(matcher.feed("os youtubers falam muito", 0.0), [])
        self.assertIs(matcher.state, State.IDLE)

    def test_trigger_alias_and_multiword(self) -> None:
        matcher = make_matcher()
        self.assertEqual(matcher.feed("you tube passa nirvana", 0.0)[0].action, "play")
        self.assertEqual(matcher.feed("iutube para", 1.0)[0].action, "stop")

    def test_armed_expires_after_trigger_window(self) -> None:
        matcher = make_matcher()
        matcher.feed("youtube", 0.0)
        self.assertEqual(matcher.feed("passa nirvana", 2.0), [])
        self.assertIs(matcher.state, State.IDLE)

    def test_non_verb_word_drops_back_to_idle(self) -> None:
        matcher = make_matcher()
        self.assertEqual(matcher.feed("vi um video no youtube ontem", 0.0), [])
        self.assertIs(matcher.state, State.IDLE)

    def test_trigger_restart_while_collecting(self) -> None:
        matcher = make_matcher()
        commands = matcher.feed("youtube youtube passa nirvana", 0.0)
        self.assertEqual([command.query for command in commands], ["nirvana"])


class SingleUtteranceTests(unittest.TestCase):
    def test_play_splits_after_the_verb(self) -> None:
        commands = make_matcher().feed("youtube passa nirvana smells like teen spirit", 0.0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].action, "play")
        self.assertEqual(commands[0].query, "nirvana smells like teen spirit")

    def test_jump_to_playlist_position(self) -> None:
        commands = make_matcher().feed("youtube jump 3", 0.0)
        self.assertEqual(commands[0].action, "jump")
        self.assertEqual(commands[0].query, "3")

        commands = make_matcher().feed("youtube toca Análise Fúria", 0.0)
        self.assertEqual(commands[0].query, "analise furia")

    def test_verb_without_query_fires_immediately(self) -> None:
        commands = make_matcher().feed("youtube próximo", 0.0)
        self.assertEqual(commands[0].action, "next")

    def test_prev_fires_on_anterior(self) -> None:
        commands = make_matcher().feed("youtube anterior", 0.0)
        self.assertEqual(commands[0].action, "prev")

    def test_multiword_verb_beats_its_prefix(self) -> None:
        commands = make_matcher().feed("youtube mais alto", 0.0)
        self.assertEqual([command.action for command in commands], ["volume_up"])

    def test_multiword_verb_with_surrounding_words(self) -> None:
        commands = make_matcher().feed("youtube baixa o som agora", 0.0)
        self.assertEqual(commands[0].action, "volume_down")

    def test_verb_prefix_is_not_enough(self) -> None:
        # "poeira" must not be read as the verb "poe".
        self.assertEqual(make_matcher().feed("youtube poeira no ar", 0.0), [])


class PausedCommandTests(unittest.TestCase):
    def test_verb_then_query_in_a_later_utterance(self) -> None:
        matcher = make_matcher()
        self.assertEqual(matcher.feed("youtube passa", 0.0), [])
        self.assertIs(matcher.state, State.COLLECTING)
        commands = matcher.feed("nirvana", 0.4)
        self.assertEqual(commands[0].query, "nirvana")

    def test_empty_query_is_refused_at_fire_time(self) -> None:
        matcher = make_matcher()
        matcher.feed("youtube passa", 0.0)
        # 800 ms of trailing silence ends the query, but there is nothing to search.
        self.assertEqual(matcher.tick(1.0, silence_ms=800), [])
        self.assertIs(matcher.state, State.IDLE)

    def test_query_window_caps_collecting(self) -> None:
        matcher = make_matcher()
        matcher.feed("youtube passa", 0.0)
        self.assertEqual(matcher.tick(7.0, silence_ms=0), [])
        self.assertIs(matcher.state, State.IDLE)


class PlaybackGuardTests(unittest.TestCase):
    def test_same_utterance_still_fires_while_playing(self) -> None:
        matcher = make_matcher()
        matcher.set_playing(True)
        commands = matcher.feed("youtube passa nirvana", 0.0)
        self.assertEqual(commands[0].action, "play")

    def test_split_utterance_is_blocked_while_playing(self) -> None:
        matcher = make_matcher()
        matcher.set_playing(True)
        self.assertEqual(matcher.feed("youtube", 0.0), [])
        self.assertEqual(matcher.feed("passa nirvana", 0.4), [])
        self.assertIs(matcher.state, State.IDLE)

    def test_split_utterance_allowed_when_playback_stopped(self) -> None:
        matcher = make_matcher()
        matcher.set_playing(True)
        matcher.feed("youtube", 0.0)
        matcher.set_playing(False)
        commands = matcher.feed("passa nirvana", 0.4)
        self.assertEqual(commands[0].action, "play")

    def test_guard_can_be_disabled_by_config(self) -> None:
        matcher = make_matcher(require_same_utterance_while_playing=False)
        matcher.set_playing(True)
        matcher.feed("youtube", 0.0)
        commands = matcher.feed("passa nirvana", 0.4)
        self.assertEqual(commands[0].action, "play")


class OrdinaryConversationTests(unittest.TestCase):
    """The M3 acceptance criterion in miniature: none of this may fire."""

    def test_normal_speech_never_commands(self) -> None:
        matcher = make_matcher()
        phrases = [
            "vi um video no youtube ontem sobre isso",
            "o youtube tem coisas boas e mas",
            "vou ao youtube ver",
            "isso esta mais alto do que devia",
            "para mim esta bem",
            "proximo domingo vou a lisboa",
            "o anterior dono do carro",
            "volta para casa depois das seis",
            "pausa para pensar",
            "aumenta a luz da sala",
            "baixa o preco do produto",
        ]
        for index, phrase in enumerate(phrases):
            self.assertEqual(matcher.feed(phrase, index * 3.0), [], phrase)


if __name__ == "__main__":
    unittest.main()