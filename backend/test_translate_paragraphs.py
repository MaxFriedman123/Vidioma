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


class TestNoSpaceScriptSplitting(unittest.TestCase):
    """CJK/Thai fallback must not blank lines (the old .split() collapse bug)."""

    def test_detects_cjk(self):
        import app
        self.assertTrue(app._is_no_space_script("我想学中文因为它很有趣"))
        self.assertTrue(app._is_no_space_script("日本語のテキストです"))
        self.assertFalse(app._is_no_space_script("Hola mundo esto es"))
        self.assertFalse(app._is_no_space_script("한국어 텍스트"))  # Korean uses spaces

    def test_cjk_align_fills_all_lines(self):
        import app
        para = "我想学中文因为它很有趣而且我喜欢中国文化"
        chunks = app.align_lines_to_paragraph(para, ["我想学中文", "因为它很有趣", "而且我喜欢中国文化"])
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertTrue(c.strip(), f"CJK line was blank: {chunks!r}")
        # No characters lost or added.
        self.assertEqual("".join(chunks).replace(" ", ""), para)

    def test_cjk_proportional_is_even(self):
        import app
        para = "一二三四五六七八九"  # 9 chars
        chunks = app._proportional_word_split(para, ["", "", ""])
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [3, 3, 3])

    def test_spaced_language_unaffected(self):
        import app
        chunks = app.align_lines_to_paragraph(
            "Hola a todos bienvenidos al video de hoy",
            ["Hola a todos", "bienvenidos al video", "de hoy"],
        )
        self.assertEqual(len(chunks), 3)
        # Words should not be split mid-token; join with space reproduces content.
        self.assertEqual(" ".join(chunks).split(), "Hola a todos bienvenidos al video de hoy".split())


class TestDeeplStructuredLines(unittest.TestCase):
    """DeepL per-line structured translation (mocked network)."""

    def _with_deepl(self, mock_request):
        import app
        app.DEEPL_API_KEY = "test:fx"
        app._DEEPL_COOLDOWN_UNTIL = 0.0
        original = app._deepl_request
        app._deepl_request = mock_request
        return original

    def _restore(self, original):
        import app
        app._deepl_request = original
        app.DEEPL_API_KEY = ""

    def test_xml_happy_path_is_one_to_one(self):
        import re

        import app

        def mock(texts, target_lang, source_lang="auto", extra_params=None):
            if (extra_params or {}).get("tag_handling") == "xml":
                return [re.sub(r"<ln>(.*?)</ln>", lambda m: f"<ln>T:{m.group(1)}</ln>", texts[0])]
            return None

        original = self._with_deepl(mock)
        try:
            out = app._deepl_translate_lines(["one", "two", "three"], "es", "en")
        finally:
            self._restore(original)
        self.assertEqual(out, ["T:one", "T:two", "T:three"])

    def test_empty_lines_reinserted_at_correct_index(self):
        import re

        import app

        def mock(texts, target_lang, source_lang="auto", extra_params=None):
            if (extra_params or {}).get("tag_handling") == "xml":
                return [re.sub(r"<ln>(.*?)</ln>", lambda m: f"<ln>T:{m.group(1)}</ln>", texts[0])]
            return None

        original = self._with_deepl(mock)
        try:
            out = app._deepl_translate_lines(["hi", "  ", "bye"], "es", "en")
        finally:
            self._restore(original)
        self.assertEqual(out, ["T:hi", "", "T:bye"])

    def test_xml_bad_count_falls_back_to_array(self):
        import app

        def mock(texts, target_lang, source_lang="auto", extra_params=None):
            if (extra_params or {}).get("tag_handling") == "xml":
                return ["<ln>only-one</ln>"]  # wrong count -> triggers array mode
            # array mode: one output per input
            return [f"A:{t}" for t in texts]

        original = self._with_deepl(mock)
        try:
            out = app._deepl_translate_lines(["a", "b", "c"], "es", "en")
        finally:
            self._restore(original)
        self.assertEqual(out, ["A:a", "A:b", "A:c"])

    def test_returns_none_when_deepl_unavailable(self):
        import app

        def mock(texts, target_lang, source_lang="auto", extra_params=None):
            return None

        original = self._with_deepl(mock)
        try:
            out = app._deepl_translate_lines(["a", "b"], "es", "en")
        finally:
            self._restore(original)
        self.assertIsNone(out)

    def test_orchestrator_paragraph_equals_join_of_lines(self):
        import re

        import app

        def mock(texts, target_lang, source_lang="auto", extra_params=None):
            if (extra_params or {}).get("tag_handling") == "xml":
                return [re.sub(r"<ln>(.*?)</ln>", lambda m: f"[{m.group(1)}]", texts[0])]
            return None

        original = self._with_deepl(mock)
        try:
            tp, tl = app.translate_with_alignment(
                ["one two three"], [["one", "two", "three"]], "es", "en"
            )
        finally:
            self._restore(original)
        self.assertEqual(len(tl[0]), 3)
        self.assertEqual(tp[0], " ".join(tl[0]))


class TestLanguageCodeNormalization(unittest.TestCase):
    """Hebrew is 'iw' in the app/YouTube but the translators package wants 'he'
    and deep-translator's Google wants 'iw'. Mismatched codes made every
    translators engine reject a Hebrew target, which (with no timeout) read as
    'transcript loads forever'."""

    def test_translators_code_maps_iw_to_he(self):
        import app
        self.assertEqual(app._ts_lang("iw"), "he")
        self.assertEqual(app._ts_lang("IW"), "he")
        self.assertEqual(app._ts_lang("he"), "he")
        self.assertEqual(app._ts_lang("es"), "es")

    def test_google_code_maps_he_to_iw(self):
        import app
        self.assertEqual(app._google_lang("he"), "iw")
        self.assertEqual(app._google_lang("iw"), "iw")
        self.assertEqual(app._google_lang("es"), "es")
        self.assertEqual(app._google_lang("auto"), "auto")

    def test_ts_translate_uses_normalized_codes(self):
        import app
        captured = {}

        class FakeTs:
            @staticmethod
            def translate_text(text, translator=None, from_language=None, to_language=None, timeout=None):
                captured["to"] = to_language
                captured["from"] = from_language
                captured["timeout"] = timeout
                return "ok"

        import sys
        original = sys.modules.get("translators")
        sys.modules["translators"] = FakeTs
        app._TRANSLATORS_IMPORT_FAILED = False
        try:
            app._ts_translate("bing", "hello", "iw", "iw", attempts=1)
        finally:
            if original is not None:
                sys.modules["translators"] = original
            else:
                del sys.modules["translators"]
        # iw normalized to he for the translators package, and a timeout is passed.
        self.assertEqual(captured["to"], "he")
        self.assertEqual(captured["from"], "he")
        self.assertIsNotNone(captured["timeout"])


class TestTranslateWithAlignmentDeadline(unittest.TestCase):
    """translate_with_alignment must always return within its wall-clock cap even
    if a paragraph task hangs — the backstop against the infinite spinner."""

    def test_returns_within_cap_when_a_paragraph_hangs(self):
        import time

        import app

        orig_timeout = app._TRANSLATE_CALL_TIMEOUT
        orig_deepl = app._deepl_available
        orig_tp = app.translate_paragraphs
        orig_align = app._legacy_align_paragraph
        app._TRANSLATE_CALL_TIMEOUT = 1.0  # deadline = 1*3 + 5 = 8s
        app._deepl_available = lambda: False

        def slow_tp(paras, target, source_lang="auto"):
            if paras and "HANG" in paras[0]:
                time.sleep(60)
            return [f"T::{p}" for p in paras]

        app.translate_paragraphs = slow_tp
        app._legacy_align_paragraph = lambda pt, sl, tl, srcl: [f"L::{x}" for x in sl]
        try:
            start = time.time()
            tp, tl = app.translate_with_alignment(
                ["normal one", "HANG this", "normal three"],
                [["normal one"], ["HANG this"], ["normal three"]],
                "he", "auto",
            )
            elapsed = time.time() - start
        finally:
            app._TRANSLATE_CALL_TIMEOUT = orig_timeout
            app._deepl_available = orig_deepl
            app.translate_paragraphs = orig_tp
            app._legacy_align_paragraph = orig_align

        self.assertLess(elapsed, 15, f"did not honor wall-clock cap ({elapsed:.1f}s)")
        self.assertTrue(tp[0])
        self.assertTrue(tp[2])
        self.assertEqual(tp[1], "")  # hung paragraph left empty


class _FakeRedisPipe:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def setex(self, k, ttl, v):
        self.ops.append((k, v))
        return self

    def execute(self):
        for k, v in self.ops:
            self.store[k] = v
        self.ops = []


class _FakeRedis:
    """Minimal in-memory Redis supporting the ops /api/translate + the L2
    transcript cache use (get/mget/setex/pipeline)."""
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def setex(self, k, ttl, v):
        self.store[k] = v

    def pipeline(self):
        return _FakeRedisPipe(self.store)


class TestPerParagraphTranslateCache(unittest.TestCase):
    """The alignment path caches EACH paragraph under its own key so the same
    paragraph reused in a different lookahead/scrub window is a hit, with
    byte-identical output. Verifies cross-window reuse + misses-only translation
    + a per-paragraph write guard that never caches empty/untranslated work."""

    def setUp(self):
        import app
        self._orig_redis = app.redis_client
        self._orig_twa = app.translate_with_alignment
        self.fake = _FakeRedis()
        app.redis_client = self.fake
        self.calls = []

        def fake_twa(paras, lines, target, source_lang="auto"):
            self.calls.append(list(paras))
            tp, tl = [], []
            for p, grp in zip(paras, lines):
                if p == "EMPTY":
                    tp.append("")
                    tl.append([])
                elif p == "SAME":
                    tp.append(p)          # comes back identical to source
                    tl.append(list(grp))
                else:
                    tp.append(f"[{target}]{p}")
                    tl.append([f"[{target}]{x}" for x in grp])
            return tp, tl

        app.translate_with_alignment = fake_twa
        self.client = app.app.test_client()

    def tearDown(self):
        import app
        app.redis_client = self._orig_redis
        app.translate_with_alignment = self._orig_twa

    def _req(self, paras, lines):
        return self.client.post("/api/translate", json={
            "paragraphs": paras, "lines": lines, "from_lang": "es", "to_lang": "en",
        }).get_json()

    def test_cross_window_reuse_and_misses_only(self):
        a = self._req(["P0", "P1", "P2"], [["a0"], ["b0", "b1"], ["c0"]])
        self.assertFalse(a["cache_hit"])
        self.assertEqual(a["translated_paragraphs"], ["[en]P0", "[en]P1", "[en]P2"])
        self.assertEqual(a["translated_lines"], [["[en]a0"], ["[en]b0", "[en]b1"], ["[en]c0"]])

        # Overlapping window: P1,P2 are hits; only P3 is translated.
        self.calls.clear()
        b = self._req(["P1", "P2", "P3"], [["b0", "b1"], ["c0"], ["d0"]])
        self.assertEqual(b["translated_paragraphs"], ["[en]P1", "[en]P2", "[en]P3"])
        self.assertEqual(self.calls, [["P3"]])
        # Same paragraph, identical output regardless of batch window.
        self.assertEqual(a["translated_paragraphs"][1], b["translated_paragraphs"][0])
        self.assertEqual(a["translated_lines"][1], b["translated_lines"][0])

        # Exact repeat = full hit, nothing translated.
        self.calls.clear()
        c = self._req(["P0", "P1", "P2"], [["a0"], ["b0", "b1"], ["c0"]])
        self.assertTrue(c["cache_hit"])
        self.assertEqual(self.calls, [])

    def test_write_guard_skips_empty_and_untranslated(self):
        import app
        r = self._req(["PA", "EMPTY", "SAME"], [["a"], ["b"], ["c"]])
        self.assertEqual(r["translated_paragraphs"], ["[en]PA", "", "SAME"])
        self.assertIn(app.generate_paragraph_cache_key("es", "en", "PA", ["a"]), self.fake.store)
        self.assertNotIn(app.generate_paragraph_cache_key("es", "en", "EMPTY", ["b"]), self.fake.store)
        self.assertNotIn(app.generate_paragraph_cache_key("es", "en", "SAME", ["c"]), self.fake.store)


class TestProcessedTranscriptL2Cache(unittest.TestCase):
    """The cross-worker Redis transcript cache is written ONLY on the
    already-in-language path (is_correct_lang=True) — never for manual
    translation, which is provider-state-dependent."""

    def setUp(self):
        import app
        self._orig_redis = app.redis_client
        self._orig_fetch = app.get_cached_transcript
        self._orig_twa = app.translate_with_alignment
        self.fake = _FakeRedis()
        app.redis_client = self.fake
        app.get_cached_processed_snippets.cache_clear()

    def tearDown(self):
        import app
        app.redis_client = self._orig_redis
        app.get_cached_transcript = self._orig_fetch
        app.translate_with_alignment = self._orig_twa
        app.get_cached_processed_snippets.cache_clear()

    def test_writes_on_correct_lang_and_hit_skips_fetch(self):
        import app
        app.get_cached_transcript = lambda vid, lang: ([{"text": "hola", "start": 0.0, "duration": 1.0}], True)
        a1, p1 = app.get_cached_processed_snippets("vidT", "es")
        key = app._processed_snippets_cache_key("vidT", "es")
        self.assertIn(key, self.fake.store)

        # A hit must skip the fetch entirely and be byte-identical.
        app.get_cached_processed_snippets.cache_clear()

        def boom(vid, lang):
            raise AssertionError("must not fetch on L2 hit")

        app.get_cached_transcript = boom
        a2, p2 = app.get_cached_processed_snippets("vidT", "es")
        self.assertEqual((a1, p1), (a2, p2))

    def test_no_write_on_manual_translation(self):
        import app
        app.get_cached_transcript = lambda vid, lang: (
            [{"text": "hello", "start": 0.0, "duration": 1.0},
             {"text": "there friend", "start": 1.0, "duration": 1.0}], False)
        app.translate_with_alignment = lambda paras, lines, target, source_lang="auto": (
            [f"[{target}]{p}" for p in paras],
            [[f"[{target}]{x}" for x in g] for g in lines])
        app.get_cached_processed_snippets("vidF", "es")
        self.assertNotIn(app._processed_snippets_cache_key("vidF", "es"), self.fake.store)


if __name__ == "__main__":
    unittest.main()
