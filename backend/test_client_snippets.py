"""Tests for the client-side caption path: browser-fetched snippets flowing
through the same grouping/translation pipeline as the server fetch, plus the
input validation that guards the /api/transcript endpoint.

Run from the backend directory:
    python -m pytest test_client_snippets.py -v
"""

import unittest

import app


class TestValidateClientSnippets(unittest.TestCase):
    def test_valid_snippets_are_normalized(self):
        raw = [
            {"text": "Hello", "start": 1.5, "duration": 2.0},
            {"text": "world", "start": "3.5", "duration": "1"},  # numeric strings
        ]
        cleaned = app._validate_client_snippets(raw)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0], {"text": "Hello", "start": 1.5, "duration": 2.0})
        # Numeric strings coerced to floats.
        self.assertEqual(cleaned[1], {"text": "world", "start": 3.5, "duration": 1.0})

    def test_missing_timings_default_to_zero(self):
        cleaned = app._validate_client_snippets([{"text": "x"}])
        self.assertEqual(cleaned[0], {"text": "x", "start": 0.0, "duration": 0.0})

    def test_negative_and_nonfinite_timings_coerced_to_zero(self):
        raw = [
            {"text": "a", "start": -5, "duration": 1},
            {"text": "b", "start": float("inf"), "duration": float("nan")},
        ]
        cleaned = app._validate_client_snippets(raw)
        self.assertEqual(cleaned[0]["start"], 0.0)
        self.assertEqual(cleaned[1]["start"], 0.0)
        self.assertEqual(cleaned[1]["duration"], 0.0)

    def test_rejects_non_list(self):
        self.assertIsNone(app._validate_client_snippets("nope"))
        self.assertIsNone(app._validate_client_snippets({"text": "x"}))
        self.assertIsNone(app._validate_client_snippets(None))

    def test_rejects_empty_list(self):
        self.assertIsNone(app._validate_client_snippets([]))

    def test_rejects_non_dict_element(self):
        self.assertIsNone(app._validate_client_snippets([{"text": "ok"}, "bad"]))

    def test_rejects_non_string_text(self):
        self.assertIsNone(app._validate_client_snippets([{"text": 123}]))
        self.assertIsNone(app._validate_client_snippets([{"start": 1.0}]))  # no text

    def test_rejects_too_many_snippets(self):
        raw = [{"text": "x"}] * (app._MAX_CLIENT_SNIPPETS + 1)
        self.assertIsNone(app._validate_client_snippets(raw))

    def test_overlong_text_is_truncated_not_rejected(self):
        long_text = "z" * (app._MAX_CLIENT_SNIPPET_CHARS + 500)
        cleaned = app._validate_client_snippets([{"text": long_text}])
        self.assertEqual(len(cleaned[0]["text"]), app._MAX_CLIENT_SNIPPET_CHARS)


class TestProcessSnippetsFromClient(unittest.TestCase):
    """The client path must produce the same output as the server path given the
    same raw snippets — it reuses _process_transcript_snippets, so we mainly
    assert the wiring (correct-lang passthrough, manual-translate branch)."""

    def setUp(self):
        # No Redis in tests; make L2 a no-op so we exercise the processing path.
        self._orig_redis = app.redis_client
        app.redis_client = None
        self._orig_twa = app.translate_with_alignment

    def tearDown(self):
        app.redis_client = self._orig_redis
        app.translate_with_alignment = self._orig_twa

    def test_correct_lang_snippets_pass_through_untranslated(self):
        # Browser already fetched native/target-language captions.
        snippets = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
        ]
        called = {"twa": False}

        def spy(*a, **k):
            called["twa"] = True
            return [], []

        app.translate_with_alignment = spy

        assigned, paragraphs = app.get_processed_snippets_from_client(
            "vid", "en", snippets, True
        )
        # No manual translation ran; source lines preserved.
        self.assertFalse(called["twa"])
        self.assertEqual(assigned[0]["source"], "Hello everyone")
        self.assertTrue(len(paragraphs) >= 1)

    def test_wrong_lang_snippets_are_manually_translated(self):
        # Browser could only get English; German requested -> translate here.
        snippets = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
        ]
        app.translate_with_alignment = lambda paras, lines, target, source_lang="auto": (
            [f"[{target}]{p}" for p in paras],
            [[f"[{target}]{ln}" for ln in group] for group in lines],
        )

        assigned, paragraphs = app.get_processed_snippets_from_client(
            "vid", "de", snippets, False
        )
        self.assertTrue(all(s["source"].startswith("[de]") for s in assigned))
        self.assertFalse(any(s["source"] == "Hello everyone" for s in assigned))

    def test_client_and_server_paths_produce_identical_output(self):
        # The two entry points must be interchangeable for the same raw input.
        snippets = [
            {"text": "One two three.", "start": 0.0, "duration": 1.0},
            {"text": "Four five six.", "start": 1.0, "duration": 1.0},
        ]
        self._orig_fetch = app.get_cached_transcript
        try:
            app.get_cached_transcript = lambda vid, lang: (snippets, True)
            app.get_cached_processed_snippets.cache_clear()
            server = app.get_cached_processed_snippets("vidX", "en")
            client = app.get_processed_snippets_from_client("vidX", "en", snippets, True)
            self.assertEqual(server, client)
        finally:
            app.get_cached_transcript = self._orig_fetch
            app.get_cached_processed_snippets.cache_clear()


class _FakeRedis:
    """Minimal Redis stand-in that records writes, so we can assert whether a
    code path populates the shared L2 cache."""

    def __init__(self):
        self.store = {}
        self.writes = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.writes.append(key)
        self.store[key] = value


class TestClientPathCacheSafety(unittest.TestCase):
    """Security regression: the unauthenticated client path must NEVER write the
    shared L2 cache (its content + is_correct_lang flag are attacker-controlled,
    so writing would poison what other users receive). The server path, whose
    content comes from YouTube directly, still writes."""

    def setUp(self):
        self._orig_redis = app.redis_client
        self._orig_fetch = app.get_cached_transcript
        self.fake = _FakeRedis()
        app.redis_client = self.fake
        app.get_cached_processed_snippets.cache_clear()

    def tearDown(self):
        app.redis_client = self._orig_redis
        app.get_cached_transcript = self._orig_fetch
        app.get_cached_processed_snippets.cache_clear()

    def test_client_path_does_not_write_l2(self):
        snippets = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
        ]
        # correct-lang so no translation runs; the server path WOULD write here.
        app.get_processed_snippets_from_client("vidP", "en", snippets, True)
        self.assertEqual(self.fake.writes, [], "client path must not populate L2")

    def test_server_path_still_writes_l2(self):
        snippets = [
            {"text": "Hello everyone", "start": 0.0, "duration": 1.5},
            {"text": "welcome to the show", "start": 1.5, "duration": 1.5},
        ]
        app.get_cached_transcript = lambda vid, lang: (snippets, True)
        app.get_cached_processed_snippets("vidS", "en")
        self.assertEqual(len(self.fake.writes), 1, "server path should populate L2")

    def test_client_path_still_reads_l2(self):
        # A server-populated entry must be a free hit for a later client request.
        app.get_cached_transcript = lambda vid, lang: (
            [{"text": "cached line", "start": 0.0, "duration": 1.0}], True
        )
        app.get_cached_processed_snippets("vidR", "en")  # server populates L2
        writes_before = len(self.fake.writes)
        # Client request for the same (video, lang) should read the cached value
        # and NOT add another write.
        assigned, _ = app.get_processed_snippets_from_client(
            "vidR", "en", [{"text": "IGNORED forged", "start": 0.0, "duration": 1.0}], True
        )
        self.assertEqual(assigned[0]["source"], "cached line")
        self.assertEqual(len(self.fake.writes), writes_before)


class TestManualRepairDeadline(unittest.TestCase):
    """Security regression: the synchronous per-line translation-repair loop must
    stop issuing translate calls once the wall-clock deadline passes, so a large
    wrong-language transcript can't pin a worker with unbounded sequential calls."""

    def setUp(self):
        self._orig_redis = app.redis_client
        self._orig_twa = app.translate_with_alignment
        self._orig_tt = app._translate_text
        self._orig_deadline = app._MANUAL_REPAIR_DEADLINE_SECONDS
        app.redis_client = None

    def tearDown(self):
        app.redis_client = self._orig_redis
        app.translate_with_alignment = self._orig_twa
        app._translate_text = self._orig_tt
        app._MANUAL_REPAIR_DEADLINE_SECONDS = self._orig_deadline

    def test_repair_loop_stops_after_deadline(self):
        # Many lines, all needing repair (alignment returns empty per-line).
        snippets = [
            {"text": f"line {i}", "start": float(i), "duration": 1.0}
            for i in range(50)
        ]
        app.translate_with_alignment = lambda paras, lines, target, source_lang="auto": (
            ["" for _ in paras],
            [["" for _ in group] for group in lines],
        )
        # Deadline of 0 => the repair loop should make ZERO direct translate calls.
        app._MANUAL_REPAIR_DEADLINE_SECONDS = 0.0
        calls = {"n": 0}

        def counting_tt(text, target, source_lang="auto"):
            calls["n"] += 1
            return f"[{target}]{text}"

        app._translate_text = counting_tt

        # With no repair and empty alignment, the result is un-translated -> the
        # no-op guard raises (retryable), which is the intended safe degradation.
        with self.assertRaises(app.TranscriptTranslationError):
            app.get_processed_snippets_from_client("vidD", "de", snippets, False)
        self.assertEqual(calls["n"], 0, "no direct translate calls after deadline")


if __name__ == "__main__":
    unittest.main()
