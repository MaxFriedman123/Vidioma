"""Tests for transcript paragraph grouping + paragraph translation shape.

Run from the backend directory:
    python test_group_paragraphs.py
or with pytest (if installed):
    python -m pytest test_group_paragraphs.py -v
"""

import unittest

import app
from app import group_into_paragraphs


def make_frag(text, start, duration=1.0):
    return {"source": text, "start": start, "duration": duration}


class TestNonNativeTranscriptLanguage(unittest.TestCase):
    """When the requested from_lang transcript isn't natively available, BOTH
    the paragraphs AND the displayed snippet lines must be translated into
    from_lang (regression: picking German used to still show English lines)."""

    def setUp(self):
        self._orig_fetch = app.get_cached_transcript
        self._orig_twa = app.translate_with_alignment
        app.get_cached_processed_snippets.cache_clear()

    def tearDown(self):
        app.get_cached_transcript = self._orig_fetch
        app.translate_with_alignment = self._orig_twa
        app.get_cached_processed_snippets.cache_clear()

    def test_snippets_translated_when_not_native_language(self):
        english = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
            {"text": "let us begin now", "start": 3.2, "duration": 1.5},
        ]
        # Video only has English; German (de) requested -> is_correct_lang False.
        app.get_cached_transcript = lambda vid, lang: (english, False)
        # Deterministic stub: tag every string with the target language.
        app.translate_with_alignment = lambda paras, lines, target, source_lang="auto": (
            [f"[{target}]{p}" for p in paras],
            [[f"[{target}]{ln}" for ln in group] for group in lines],
        )

        snippets, paragraphs = app.get_cached_processed_snippets("vid", "de")

        # Every displayed snippet line must now be in the requested language.
        self.assertTrue(all(s["source"].startswith("[de]") for s in snippets))
        self.assertTrue(all(p.startswith("[de]") for p in paragraphs))
        # No English leaked into the shown transcript.
        self.assertFalse(any(s["source"] == "Hello everyone" for s in snippets))

    def test_native_language_left_untouched(self):
        english = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
        ]
        # Video has English and English (en) requested -> is_correct_lang True.
        app.get_cached_transcript = lambda vid, lang: (english, True)
        called = {"twa": False}

        def spy(*a, **k):
            called["twa"] = True
            return [], []

        app.translate_with_alignment = spy

        snippets, paragraphs = app.get_cached_processed_snippets("vid", "en")

        # Native language: no manual translation, source lines preserved verbatim.
        self.assertFalse(called["twa"])
        self.assertEqual(snippets[0]["source"], "Hello everyone")


class TestGroupIntoParagraphs(unittest.TestCase):
    def test_single_fragment_produces_one_paragraph(self):
        frags = [make_frag("hello world", 0)]
        assigned, paragraphs = group_into_paragraphs(frags)
        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0]["paragraph"], 0)
        self.assertEqual(paragraphs, ["hello world"])

    def test_fragments_share_paragraph_when_close_together(self):
        frags = [
            make_frag("I went", 0, 1.0),
            make_frag("to the store", 1.0, 1.0),
            make_frag("yesterday.", 2.0, 1.0),
        ]
        assigned, paragraphs = group_into_paragraphs(frags)
        # All three should land in the same paragraph (closed by sentence end)
        self.assertEqual({a["paragraph"] for a in assigned}, {0})
        self.assertEqual(len(paragraphs), 1)
        self.assertIn("I went", paragraphs[0])
        self.assertIn("yesterday.", paragraphs[0])

    def test_time_gap_forces_new_paragraph(self):
        frags = [
            make_frag("First bit", 0, 1.0),
            make_frag("Second bit after long pause", 10.0, 1.0),
        ]
        assigned, paragraphs = group_into_paragraphs(frags)
        self.assertEqual(assigned[0]["paragraph"], 0)
        self.assertEqual(assigned[1]["paragraph"], 1)
        self.assertEqual(len(paragraphs), 2)

    def test_sentence_boundary_after_enough_fragments_starts_new_paragraph(self):
        # Paragraphs need at least a couple of fragments before a sentence
        # boundary closes them — otherwise every short utterance becomes its
        # own paragraph and we lose the translation context benefit.
        frags = [
            make_frag("Chapter one", 0, 1.0),
            make_frag("begins here.", 1.0, 1.0),
            make_frag("A new topic", 2.0, 1.0),
            make_frag("opens up.", 3.0, 1.0),
        ]
        assigned, paragraphs = group_into_paragraphs(frags)
        self.assertEqual(assigned[0]["paragraph"], 0)
        self.assertEqual(assigned[1]["paragraph"], 0)
        self.assertEqual(assigned[2]["paragraph"], 1)
        self.assertEqual(assigned[3]["paragraph"], 1)
        self.assertEqual(len(paragraphs), 2)

    def test_single_fragment_sentence_does_not_close_paragraph(self):
        # A short utterance on its own shouldn't become a solo paragraph —
        # keep merging until we have enough context.
        frags = [
            make_frag("Hello.", 0, 1.0),
            make_frag("Something else follows.", 1.0, 1.0),
        ]
        assigned, paragraphs = group_into_paragraphs(frags)
        self.assertEqual(assigned[0]["paragraph"], 0)
        self.assertEqual(assigned[1]["paragraph"], 0)
        self.assertEqual(len(paragraphs), 1)

    def test_hard_cap_on_fragment_count(self):
        # Auto-caption streams without punctuation should still be chunked.
        frags = [make_frag(f"word{i}", i, 1.0) for i in range(20)]
        _, paragraphs = group_into_paragraphs(frags)
        self.assertGreaterEqual(len(paragraphs), 2)

    def test_empty_input_returns_empty(self):
        assigned, paragraphs = group_into_paragraphs([])
        self.assertEqual(assigned, [])
        self.assertEqual(paragraphs, [])

    def test_skips_empty_fragments(self):
        frags = [
            make_frag("Hello.", 0, 1.0),
            make_frag("   ", 1.0, 1.0),
            make_frag("World.", 2.0, 1.0),
        ]
        assigned, paragraphs = group_into_paragraphs(frags)
        self.assertEqual(len(assigned), 2)

    def test_fragment_carries_original_timing(self):
        frags = [
            make_frag("first", 5.0, 2.0),
            make_frag("second.", 7.5, 1.5),
        ]
        assigned, _ = group_into_paragraphs(frags)
        self.assertEqual(assigned[0]["start"], 5.0)
        self.assertEqual(assigned[0]["duration"], 2.0)
        self.assertEqual(assigned[1]["start"], 7.5)
        self.assertEqual(assigned[1]["duration"], 1.5)


if __name__ == "__main__":
    unittest.main()
