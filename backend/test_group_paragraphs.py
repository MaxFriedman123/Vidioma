"""Tests for transcript paragraph grouping + paragraph translation shape.

Run from the backend directory:
    python test_group_paragraphs.py
or with pytest (if installed):
    python -m pytest test_group_paragraphs.py -v
"""

import unittest

from app import group_into_paragraphs


def make_frag(text, start, duration=1.0):
    return {"source": text, "start": start, "duration": duration}


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
