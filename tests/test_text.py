"""Normalization and phrase matching (plan2.md §6.1)."""

from __future__ import annotations

import unittest

from voiceyt.text import (
    find_phrase,
    match_at,
    normalize,
    phrase_tokens,
    sort_longest_first,
    strip_accents,
)


class NormalizeTests(unittest.TestCase):
    def test_lowercase_accents_punctuation_whitespace(self) -> None:
        self.assertEqual(normalize("  YouTube,  passa  "), "youtube passa")
        self.assertEqual(normalize("PÁRA!"), "para")
        self.assertEqual(normalize("próximo"), "proximo")
        self.assertEqual(normalize("MAIS  ALTO?"), "mais alto")

    def test_empty_and_none(self) -> None:
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("..."), "")
        self.assertEqual(normalize(None or ""), "")

    def test_strip_accents_keeps_letters(self) -> None:
        self.assertEqual(strip_accents("ação"), "acao")
        self.assertEqual(strip_accents("über"), "uber")

    def test_digits_survive(self) -> None:
        self.assertEqual(normalize("video 2 - ao vivo"), "video 2 ao vivo")


class PhraseTests(unittest.TestCase):
    def test_phrase_tokens_are_normalized(self) -> None:
        self.assertEqual(phrase_tokens("mais alto"), ("mais", "alto"))
        self.assertEqual(phrase_tokens("PÁRA"), ("para",))

    def test_match_at_is_whole_word(self) -> None:
        tokens = ["os", "youtubers", "falam"]
        self.assertFalse(match_at(tokens, ("youtube",), 1))
        self.assertIsNone(find_phrase(tokens, [("youtube",)]))
        self.assertTrue(match_at(["youtube", "passa"], ("youtube",), 0))

    def test_multi_word_phrase_matches_anywhere(self) -> None:
        tokens = ["agora", "sobe", "o", "som", "por", "favor"]
        self.assertEqual(find_phrase(tokens, [("sobe", "o", "som")]), (1, 3))

    def test_longest_phrase_wins_at_the_same_position(self) -> None:
        tokens = ["youtube", "mais", "alto"]
        phrases = [("mais",), ("mais", "alto")]
        self.assertEqual(find_phrase(tokens, phrases), (1, 2))

    def test_sort_longest_first(self) -> None:
        phrases = [("mais",), ("mais", "alto"), ("youtube",)]
        self.assertEqual(
            sort_longest_first(phrases),
            [("mais", "alto"), ("mais",), ("youtube",)],
        )

    def test_multi_word_trigger_spans_tokens(self) -> None:
        self.assertEqual(find_phrase(["you", "tube", "passa"], [("you", "tube")]), (0, 2))


if __name__ == "__main__":
    unittest.main()
