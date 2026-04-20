"""Tests for full-transcript translation chunk recovery.

The real translator is never called — we test the recovery helpers directly
and stub the translator for the top-level orchestrator.

Run from the backend directory:
    python test_translate_paragraphs.py
or with pytest:
    python -m pytest test_translate_paragraphs.py -v
"""

import unittest

from app import (
    _proportional_sentence_split,
    _recover_chunks,
    align_lines_to_paragraph,
    translate_paragraphs,
)


class TestRecoverChunks(unittest.TestCase):
    def test_single_paragraph_returns_whole_translation(self):
        chunks = _recover_chunks("Hola mundo.", ["Hello world."])
        self.assertEqual(chunks, ["Hola mundo."])

    def test_double_newline_split(self):
        translated = "Hola.\n\nAdios."
        chunks = _recover_chunks(translated, ["Hello.", "Goodbye."])
        self.assertEqual(chunks, ["Hola.", "Adios."])

    def test_single_newline_split_when_double_missing(self):
        translated = "Hola.\nAdios."
        chunks = _recover_chunks(translated, ["Hello.", "Goodbye."])
        self.assertEqual(chunks, ["Hola.", "Adios."])

    def test_falls_back_to_sentence_alignment(self):
        # Translator stripped all newlines — two sentences, two paragraphs.
        translated = "Hola mundo. Adios mundo."
        chunks = _recover_chunks(translated, ["Hello world.", "Goodbye world."])
        self.assertEqual(len(chunks), 2)
        self.assertIn("Hola mundo", chunks[0])
        self.assertIn("Adios mundo", chunks[1])

    def test_empty_translation_returns_none(self):
        self.assertIsNone(_recover_chunks("", ["a", "b"]))


class TestProportionalSentenceSplit(unittest.TestCase):
    def test_equal_source_paragraphs_split_sentences_evenly(self):
        translated = "Uno. Dos. Tres. Cuatro."
        # Four source paragraphs of equal length → each paragraph gets one sentence.
        source = ["aaaa", "aaaa", "aaaa", "aaaa"]
        chunks = _proportional_sentence_split(translated, source)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks, ["Uno.", "Dos.", "Tres.", "Cuatro."])

    def test_unequal_proportions_weight_boundaries(self):
        translated = "First. Second. Third. Fourth. Fifth. Sixth."
        # First paragraph is 5x longer — should capture most sentences.
        source = ["a" * 50, "a" * 10]
        chunks = _proportional_sentence_split(translated, source)
        self.assertEqual(len(chunks), 2)
        # First chunk gets most of the sentences; second gets the tail.
        self.assertGreater(len(chunks[0]), len(chunks[1]))

    def test_returns_none_when_fewer_sentences_than_paragraphs(self):
        # Only one sentence but three source paragraphs — can't split safely.
        chunks = _proportional_sentence_split("Solo una oracion.", ["a", "b", "c"])
        self.assertIsNone(chunks)

    def test_each_paragraph_gets_at_least_one_sentence(self):
        translated = "One. Two. Three."
        # Lopsided source proportions shouldn't leave a paragraph empty.
        chunks = _proportional_sentence_split(translated, ["a" * 100, "a", "a"])
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertTrue(c.strip())


class FakeTranslator:
    """Stub translator that joins a fixed suffix; lets us verify orchestration."""

    def __init__(self, *, behavior="uppercase", fail_batch=False):
        self.calls = []
        self.behavior = behavior
        self.fail_batch = fail_batch

    def __call__(self, text, target_lang, source_lang="auto"):
        self.calls.append(text)
        if self.fail_batch and "\n\n" in text:
            raise RuntimeError("simulated batch failure")
        if self.behavior == "uppercase":
            return text.upper()
        if self.behavior == "strip_newlines":
            return text.replace("\n\n", " ").replace("\n", " ").upper()
        raise ValueError(self.behavior)


def _install_fake(fake):
    """Patch the single translator seam used by translate_paragraphs."""
    import app
    original = app._translate_text
    app._translate_text = fake
    return original


def _restore(original):
    import app
    app._translate_text = original


class TestTranslateParagraphsOrchestration(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(translate_paragraphs([], "es"), [])

    def test_skips_empty_paragraphs_preserving_positions(self):
        fake = FakeTranslator()
        original = _install_fake(fake)
        try:
            result = translate_paragraphs(["hello.", "", "world."], "es")
        finally:
            _restore(original)

        self.assertEqual(result[1], "")
        self.assertTrue(result[0])
        self.assertTrue(result[2])

    def test_full_text_failure_triggers_per_paragraph_fallback(self):
        fake = FakeTranslator(fail_batch=True)
        original = _install_fake(fake)
        try:
            result = translate_paragraphs(["one.", "two."], "es")
        finally:
            _restore(original)

        # Each paragraph was translated individually after full-text failed.
        self.assertEqual(result, ["ONE.", "TWO."])

    def test_strip_newlines_still_recovers_via_sentence_alignment(self):
        fake = FakeTranslator(behavior="strip_newlines")
        original = _install_fake(fake)
        try:
            result = translate_paragraphs(["Hello world.", "Goodbye world."], "es")
        finally:
            _restore(original)

        self.assertEqual(len(result), 2)
        self.assertTrue(result[0])
        self.assertTrue(result[1])
        # Each chunk should be one of the two translated sentences.
        self.assertIn("HELLO WORLD", result[0])
        self.assertIn("GOODBYE WORLD", result[1])


class TestAlignLinesToParagraph(unittest.TestCase):
    def test_single_line_returns_whole_paragraph(self):
        self.assertEqual(
            align_lines_to_paragraph("Hola mundo.", ["Hello world."]),
            ["Hola mundo."],
        )

    def test_empty_paragraph_gives_empty_chunks(self):
        self.assertEqual(
            align_lines_to_paragraph("", ["Hello", "World"]),
            ["", ""],
        )

    def test_no_lines_returns_empty(self):
        self.assertEqual(align_lines_to_paragraph("Hola mundo.", []), [])

    def test_handles_word_order_flip(self):
        # English: "you want to speak" splits across two lines; Spanish reorders.
        # Paragraph: "quieres hablar" — verb is at the END in Spanish.
        # Anchor for line 1 ("you want to") should capture "quieres".
        # Anchor for line 2 ("speak") should capture "hablar".
        # Proportional split would put both words in line 1 (since line 1 has
        # more source words); alignment should correctly split them.
        paragraph = "Probablemente estés aquí porque quieres hablar"
        anchors = [
            "Probablemente estás aquí porque quieres",  # line 1 anchor
            "hablar",                                    # line 2 anchor
        ]
        chunks = align_lines_to_paragraph(paragraph, anchors)
        self.assertEqual(len(chunks), 2)
        self.assertIn("quieres", chunks[0])
        self.assertEqual(chunks[1], "hablar")
        self.assertNotIn("hablar", chunks[0])

    def test_anchors_with_more_content_win_longer_spans(self):
        paragraph = "uno dos tres cuatro cinco"
        anchors = ["uno dos tres", "cinco"]
        chunks = align_lines_to_paragraph(paragraph, anchors)
        self.assertEqual(len(chunks), 2)
        self.assertIn("uno", chunks[0])
        self.assertIn("dos", chunks[0])
        self.assertIn("tres", chunks[0])
        self.assertIn("cinco", chunks[1])

    def test_each_line_gets_at_least_one_word(self):
        paragraph = "a b c"
        anchors = ["", "", ""]  # no anchor signal
        chunks = align_lines_to_paragraph(paragraph, anchors)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertTrue(c.strip(), f"chunk was empty: {chunks!r}")

    def test_falls_back_when_fewer_words_than_lines(self):
        # 2 words, 3 lines — DP can't give each line a word; fallback should
        # still return 3 chunks (some may be empty by count).
        chunks = align_lines_to_paragraph("uno dos", ["a", "b", "c"])
        self.assertEqual(len(chunks), 3)

    def test_punctuation_in_paragraph_is_tolerated(self):
        paragraph = "Hola, mundo. ¡Hasta luego!"
        anchors = ["hello world", "bye"]
        chunks = align_lines_to_paragraph(paragraph, anchors)
        # "bye" → anchor token {bye} doesn't match Spanish, so fallback to
        # proportional-ish — but each line still gets at least one word.
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0])
        self.assertTrue(chunks[1])


if __name__ == "__main__":
    unittest.main()
