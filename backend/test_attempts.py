"""Tests for POST /api/attempts, the per-answer log.

The app graded every submission and threw the result away: which line a learner
failed, and what they typed, were unrecoverable. This endpoint records them, and
because it accepts a caller-supplied batch it is also a write surface that has to
reject attribution to another user, clamp out-of-range values, and never fail a
whole batch over one bad entry.

Run from the backend directory:
    python -m pytest test_attempts.py -v
"""
import unittest

import app
from test_assignments import AssignmentTestBase


def _attempt(**over):
    base = {
        "youtube_id": "vid00000001",
        "transcript_language": "en",
        "translation_language": "es",
        "line_index": 3,
        "source_text": "We are no strangers to love",
        "expected_text": "No somos ajenos al amor",
        "user_text": "no somos ajenos al amor",
        "practice_mode": "translate",
        "score": 0.91,
        "passed": True,
    }
    base.update(over)
    return base


class AttemptLogTestBase(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.db.tables.setdefault("line_attempts", [])
        # The limiter uses in-memory storage that persists for the whole pytest
        # session, so a test that sends many requests as one user would otherwise
        # drain the shared bucket and 429 whatever runs next.
        if app.limiter is not None:
            try:
                app.limiter.storage.reset()
                self.addCleanup(app.limiter.storage.reset)
            except Exception:
                pass

    def post(self, attempts):
        return self.client.post("/api/attempts", json={"attempts": attempts},
                                headers=self.hdr())

    @property
    def rows(self):
        return self.db.tables["line_attempts"]


class TestRecording(AttemptLogTestBase):
    def test_records_an_attempt(self):
        self.as_user("stud1")
        resp = self.post([_attempt()])

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["recorded"], 1)
        self.assertEqual(len(self.rows), 1)
        row = self.rows[0]
        self.assertEqual(row["user_text"], "no somos ajenos al amor")
        self.assertEqual(row["line_index"], 3)
        self.assertTrue(row["passed"])

    def test_records_a_batch_in_one_request(self):
        self.as_user("stud1")
        resp = self.post([_attempt(line_index=i) for i in range(5)])

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(self.rows), 5)

    def test_a_failed_attempt_is_recorded_too(self):
        """The failures are the useful half: a pass-only log has no error history."""
        self.as_user("stud1")
        self.post([_attempt(passed=False, score=0.2, user_text="wrong")])

        self.assertEqual(len(self.rows), 1)
        self.assertFalse(self.rows[0]["passed"])

    def test_source_hash_is_stable_across_cosmetic_changes(self):
        """Keyed on normalized text, so a caption re-upload does not orphan history."""
        self.as_user("stud1")
        self.post([_attempt(source_text="We are no strangers to love")])
        self.post([_attempt(source_text="  we   ARE no strangers to love  ")])

        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[0]["source_hash"], self.rows[1]["source_hash"])

    def test_different_lines_hash_differently(self):
        self.as_user("stud1")
        self.post([_attempt(source_text="line one"), _attempt(source_text="line two")])

        self.assertNotEqual(self.rows[0]["source_hash"], self.rows[1]["source_hash"])

    def test_practice_mode_is_recorded(self):
        """The same line graded as translation and as dictation is not comparable."""
        self.as_user("stud1")
        self.post([_attempt(practice_mode="dictate")])

        self.assertEqual(self.rows[0]["practice_mode"], "dictate")

    def test_an_unknown_mode_falls_back_rather_than_failing(self):
        self.as_user("stud1")
        resp = self.post([_attempt(practice_mode="hovercraft")])

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.rows[0]["practice_mode"], "translate")

    def test_expected_text_is_stored(self):
        """Snapshotted: the translation cache expires and MT is not stable, so
        re-deriving later would change what 'correct' meant at the time."""
        self.as_user("stud1")
        self.post([_attempt(expected_text="No somos ajenos al amor")])

        self.assertEqual(self.rows[0]["expected_text"], "No somos ajenos al amor")


class TestAttribution(AttemptLogTestBase):
    def test_attempts_are_attributed_to_the_authenticated_user(self):
        self.as_user("stud1")
        self.post([_attempt()])

        self.assertEqual(self.rows[0]["user_id"], "stud1")

    def test_a_caller_supplied_user_id_is_ignored(self):
        """The write surface must not let one learner log practice as another."""
        self.as_user("stud1")
        self.post([_attempt(user_id="stud2")])

        self.assertEqual({r["user_id"] for r in self.rows}, {"stud1"})

    def test_unauthenticated_cannot_log(self):
        resp = self.client.post("/api/attempts", json={"attempts": [_attempt()]})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.rows, [])


class TestValidation(AttemptLogTestBase):
    def setUp(self):
        super().setUp()
        self.as_user("stud1")

    def test_rejects_a_non_object_body(self):
        self.assertEqual(self.client.post("/api/attempts", json=[], headers=self.hdr()).status_code, 400)

    def test_rejects_an_empty_batch(self):
        self.assertEqual(self.post([]).status_code, 400)

    def test_rejects_an_oversized_batch(self):
        resp = self.post([_attempt() for _ in range(app.MAX_ATTEMPTS_PER_BATCH + 1)])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.rows, [])

    def test_skips_a_malformed_entry_but_keeps_the_good_ones(self):
        """One bad row must not cost a learner the rest of their session."""
        resp = self.post([_attempt(), {"garbage": True}, _attempt(line_index=9)])

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["recorded"], 2)
        self.assertEqual(len(self.rows), 2)

    def test_rejects_a_batch_with_nothing_usable(self):
        resp = self.post([{"nope": 1}, "not even an object"])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.rows, [])

    def test_missing_required_text_is_skipped(self):
        resp = self.post([_attempt(user_text=None)])
        self.assertEqual(resp.status_code, 400)

    def test_score_is_clamped_into_range(self):
        """Out-of-range would violate the CHECK constraint and fail the batch."""
        self.post([_attempt(score=5), _attempt(score=-2)])

        scores = [r["score"] for r in self.rows]
        self.assertEqual(scores, [1.0, 0.0])

    def test_a_non_numeric_score_becomes_zero(self):
        self.post([_attempt(score="banana")])
        self.assertEqual(self.rows[0]["score"], 0.0)

    def test_nan_score_becomes_zero(self):
        self.post([_attempt(score=float("nan"))])
        self.assertEqual(self.rows[0]["score"], 0.0)

    def test_a_negative_line_index_is_coerced(self):
        self.post([_attempt(line_index=-5)])
        self.assertEqual(self.rows[0]["line_index"], 0)

    def test_long_text_is_truncated(self):
        self.post([_attempt(user_text="x" * 9000, source_text="y" * 9000)])

        self.assertLessEqual(len(self.rows[0]["user_text"]), app.MAX_ATTEMPT_TEXT_CHARS)
        self.assertLessEqual(len(self.rows[0]["source_text"]), app.MAX_ATTEMPT_TEXT_CHARS)

    def test_missing_languages_are_rejected(self):
        self.assertEqual(self.post([_attempt(transcript_language=None)]).status_code, 400)
        self.assertEqual(self.post([_attempt(translation_language="  ")]).status_code, 400)


class TestAssignmentLink(AttemptLogTestBase):
    def test_an_assignment_attempt_records_the_assignment(self):
        self.as_user("stud1")
        self.post([_attempt(assignment_id="asg-1")])

        self.assertEqual(self.rows[0]["assignment_id"], "asg-1")

    def test_personal_practice_records_no_assignment(self):
        self.as_user("stud1")
        self.post([_attempt()])

        self.assertIsNone(self.rows[0]["assignment_id"])


class TestDegradation(AttemptLogTestBase):
    def test_returns_503_when_the_database_is_unconfigured(self):
        self.as_user("stud1")
        app.supabase_ready = False
        self.assertEqual(self.post([_attempt()]).status_code, 503)

    def test_a_database_error_does_not_leak_internals(self):
        self.as_user("stud1")

        def boom(*a, **kw):
            raise RuntimeError("connection string postgres://user:secret@host")

        app._sb_post = boom
        resp = self.post([_attempt()])

        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("secret", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
