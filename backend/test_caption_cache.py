"""Tests for transcript caching and the corroboration that gates it.

Why this exists: the L2 (Redis) transcript cache had only ONE writer, the
server-side YouTube fetch, which YouTube IP-blocks on our host. So in production
the cache could never populate: every request re-fetched, and a YouTube-side
outage took the app down with nothing to fall back on.

Browser-supplied captions can now populate it, but only after being corroborated
against YouTube independently, because that endpoint is unauthenticated and its
payload is caller-controlled.

Run from the backend directory:
    python -m pytest test_caption_cache.py -v
"""
import json
import unittest
from unittest import mock

import app

SNIPPETS = [
    {"text": "hello there", "start": 0.0, "duration": 2.0},
    {"text": "general kenobi", "start": 2.0, "duration": 2.0},
    {"text": "you are a bold one", "start": 4.0, "duration": 2.0},
]


def _json3(texts):
    """A timedtext json3 body, the shape YouTube returns."""
    return {"events": [{"segs": [{"utf8": t}], "tStartMs": i * 1000, "dDurationMs": 1000}
                       for i, t in enumerate(texts)]}


class FakeRedis:
    """Just enough Redis for the L2 transcript cache."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def ping(self):
        return True


class CorroborationTest(unittest.TestCase):
    def setUp(self):
        self.relay_patch = mock.patch.object(app, "CAPTION_RELAY_URL", "https://relay.test")
        self.relay_patch.start()

    def tearDown(self):
        self.relay_patch.stop()

    def _stub_youtube(self, relay_ok=True, is_correct_lang=True, truth_texts=None, tt_ok=True):
        """Stub the relay list call and the timedtext download it points at."""
        truth = truth_texts if truth_texts is not None else [s["text"] for s in SNIPPETS]

        def fake_post(url, **kw):
            resp = mock.Mock()
            resp.ok = relay_ok
            resp.json.return_value = {
                "url": "https://youtube.test/timedtext?v=vid&fmt=srv3",
                "is_correct_lang": is_correct_lang,
            }
            return resp

        def fake_get(url, **kw):
            resp = mock.Mock()
            resp.ok = tt_ok
            resp.json.return_value = _json3(truth)
            return resp

        return mock.patch.multiple(app._HTTP_SESSION, post=fake_post, get=fake_get)

    def test_matching_snippets_are_corroborated(self):
        with self._stub_youtube():
            self.assertTrue(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_substituted_content_is_rejected(self):
        """The whole point: a caller cannot get their own text into the cache."""
        forged = [{"text": "buy my product", "start": 0.0, "duration": 2.0},
                  {"text": "click this link", "start": 2.0, "duration": 2.0},
                  {"text": "totally not the video", "start": 4.0, "duration": 2.0}]
        with self._stub_youtube():
            self.assertFalse(app._corroborate_client_snippets("vid", "en", forged))

    def test_partially_substituted_content_is_rejected(self):
        """A batch that starts genuine and then diverges must still fail."""
        half = [SNIPPETS[0]] + [{"text": f"injected line {i}", "start": i, "duration": 1.0}
                                for i in range(2, 12)]
        with self._stub_youtube(truth_texts=[s["text"] for s in SNIPPETS]):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", half))

    def test_relay_unavailable_means_no_corroboration(self):
        with self._stub_youtube(relay_ok=False):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_timedtext_unavailable_means_no_corroboration(self):
        with self._stub_youtube(tt_ok=False):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_relay_disagrees_on_language(self):
        """If YouTube's track isn't in from_lang, the client's claim is unverified."""
        with self._stub_youtube(is_correct_lang=False):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_no_relay_configured_means_no_corroboration(self):
        with mock.patch.object(app, "CAPTION_RELAY_URL", ""):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_network_error_is_contained(self):
        """A corroboration failure must degrade to 'do not cache', never raise."""
        def boom(*a, **kw):
            raise RuntimeError("connection reset")

        with mock.patch.object(app._HTTP_SESSION, "post", boom):
            self.assertFalse(app._corroborate_client_snippets("vid", "en", SNIPPETS))

    def test_cosmetic_differences_still_corroborate(self):
        """Line wrapping and casing must not fail an otherwise-genuine batch."""
        cosmetic = [{"text": "Hello   There", "start": 0.0, "duration": 2.0},
                    {"text": "General\nKenobi", "start": 2.0, "duration": 2.0},
                    {"text": "You Are A Bold One", "start": 4.0, "duration": 2.0}]
        with self._stub_youtube():
            self.assertTrue(app._corroborate_client_snippets("vid", "en", cosmetic))


class CachePromotionTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedis()
        self.redis_patch = mock.patch.object(app, "redis_client", self.fake)
        self.redis_patch.start()

    def tearDown(self):
        self.redis_patch.stop()

    def test_corroborated_snippets_are_cached(self):
        with mock.patch.object(app, "_corroborate_client_snippets", return_value=True):
            app._promote_client_snippets_to_l2(
                "vid", "en", SNIPPETS, [{"source": "hello there", "start": 0, "duration": 2, "paragraph": 0}],
                ["hello there"],
            )
        self.assertTrue(self.fake.store, "expected a cache entry to be written")

    def test_uncorroborated_snippets_are_not_cached(self):
        with mock.patch.object(app, "_corroborate_client_snippets", return_value=False):
            app._promote_client_snippets_to_l2(
                "vid", "en", SNIPPETS, [{"source": "x", "start": 0, "duration": 2, "paragraph": 0}], ["x"],
            )
        self.assertEqual(self.fake.store, {}, "uncorroborated data must never be cached")

    def test_promotion_never_raises(self):
        with mock.patch.object(app, "_corroborate_client_snippets", side_effect=RuntimeError("boom")):
            app._promote_client_snippets_to_l2("vid", "en", SNIPPETS, [], [])  # must not raise


class StaleCacheFallbackTest(unittest.TestCase):
    """A live-fetch failure should serve a cached transcript instead of erroring."""

    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()
        self.fake = FakeRedis()
        self.redis_patch = mock.patch.object(app, "redis_client", self.fake)
        self.redis_patch.start()
        self._ip = 0

    def tearDown(self):
        self.redis_patch.stop()

    def _post(self, body):
        self._ip += 1
        return self.client.post("/api/transcript", json=body,
                                environ_base={"REMOTE_ADDR": f"198.51.100.{self._ip}"})

    def _seed_cache(self, video_id, from_lang, snippets, paragraphs):
        key = app._processed_snippets_cache_key(video_id, from_lang)
        self.fake.store[key] = json.dumps((snippets, paragraphs))

    def test_cached_transcript_served_when_youtube_blocks_us(self):
        cached_snippets = [{"source": "hola mundo", "start": 0, "duration": 2, "paragraph": 0}]
        self._seed_cache("dQw4w9WgXcQ", "es", cached_snippets, ["hola mundo"])

        # The live server fetch fails exactly as it does in production.
        with mock.patch.object(app, "get_cached_processed_snippets",
                               side_effect=Exception("RequestBlocked: YouTube is blocking requests")):
            resp = self._post({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                               "from_lang": "es", "to_lang": "en"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["snippets"], cached_snippets)

    def test_still_errors_when_nothing_is_cached(self):
        with mock.patch.object(app, "get_cached_processed_snippets",
                               side_effect=Exception("RequestBlocked: YouTube is blocking requests")):
            resp = self._post({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                               "from_lang": "es", "to_lang": "en"})

        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
