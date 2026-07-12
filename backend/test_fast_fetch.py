"""Tests for the cold-load fast transcript fetch (skip the watch-page round-trip).

The fast fetcher POSTs the innertube player API directly instead of first
downloading the ~1MB watch HTML just to scrape the API key. It is a
FAST-PATH-WITH-FALLBACK: any anomaly (block, empty captions, parse error,
library-shape change) must transparently fall back to the stock library path so
a would-succeed fetch is never turned into a failure.

No network calls are made — the innertube/stock methods are stubbed.

Run from the backend directory:
    python -m pytest test_fast_fetch.py -v
"""

import unittest

import app


@unittest.skipIf(app._FastTranscriptListFetcher is None, "fast fetcher not available")
class TestFastFetcherFallback(unittest.TestCase):
    def setUp(self):
        from youtube_transcript_api._transcripts import TranscriptListFetcher
        self.Stock = TranscriptListFetcher
        self._orig_stock = TranscriptListFetcher._fetch_captions_json
        # A fetcher with no real http client; we stub every network method.
        self.f = app._FastTranscriptListFetcher.__new__(app._FastTranscriptListFetcher)

    def tearDown(self):
        self.Stock._fetch_captions_json = self._orig_stock

    def _stub_stock(self, marker):
        calls = {"n": 0}

        def fake(inner_self, video_id, try_number=0):
            calls["n"] += 1
            return {"captionTracks": [marker], "translationLanguages": []}

        self.Stock._fetch_captions_json = fake
        return calls

    def test_fast_path_used_when_captions_present(self):
        # innertube returns real caption tracks -> stock path must NOT run.
        self.f._fetch_innertube_data = lambda vid, key: {"x": 1}
        self.f._extract_captions_json = lambda data, vid: {"captionTracks": [{"languageCode": "en"}]}
        stock_calls = self._stub_stock({"languageCode": "STOCK"})
        result = self.f._fetch_captions_json("vid")
        self.assertEqual(stock_calls["n"], 0, "stock path should not run when fast path yields captions")
        self.assertEqual(result["captionTracks"][0]["languageCode"], "en")

    def test_empty_captions_falls_back_to_stock(self):
        # A blocked-but-200 IP can return no captionTracks; must fall back so we
        # never mislabel it as TranscriptsDisabled.
        self.f._fetch_innertube_data = lambda vid, key: {"x": 1}
        self.f._extract_captions_json = lambda data, vid: {}  # no captionTracks
        stock_calls = self._stub_stock({"languageCode": "STOCK"})
        result = self.f._fetch_captions_json("vid")
        self.assertEqual(stock_calls["n"], 1, "must fall back to stock when fast path has no captions")
        self.assertEqual(result["captionTracks"][0]["languageCode"], "STOCK")

    def test_parse_error_falls_back_to_stock(self):
        def boom(vid, key):
            raise ValueError("innertube shape changed")

        self.f._fetch_innertube_data = boom
        stock_calls = self._stub_stock({"languageCode": "STOCK"})
        result = self.f._fetch_captions_json("vid")
        self.assertEqual(stock_calls["n"], 1, "must fall back to stock on any fast-path error")
        self.assertEqual(result["captionTracks"][0]["languageCode"], "STOCK")

    def test_block_propagates_and_skips_stock(self):
        # A block must re-raise so the library's IP-rotation retry loop fires —
        # it must NOT be swallowed into the (same-IP) stock fallback.
        from youtube_transcript_api._errors import RequestBlocked

        def blocked(vid, key):
            raise RequestBlocked(vid)

        self.f._fetch_innertube_data = blocked
        stock_calls = self._stub_stock({"languageCode": "STOCK"})
        with self.assertRaises(RequestBlocked):
            self.f._fetch_captions_json("vid")
        self.assertEqual(stock_calls["n"], 0, "block must propagate, not fall back")


class TestForceProxyGating(unittest.TestCase):
    """FORCE_PROXY/RENDER skips the always-failing direct attempt in prod; unset
    keeps today's direct-first behavior."""

    def test_force_proxy_skips_direct_and_requires_creds(self):
        import os

        app.get_cached_transcript.cache_clear()
        had = os.environ.get("FORCE_PROXY")
        os.environ["FORCE_PROXY"] = "1"
        # Ensure no proxy creds so the proxy path raises immediately (proving we
        # went straight to it without attempting/awaiting a direct fetch).
        saved = {k: os.environ.pop(k, None) for k in ("WEBSHARE_USERNAME", "WEBSHARE_PASSWORD")}
        try:
            with self.assertRaises(ValueError):
                app.get_cached_transcript("dQw4w9WgXcQ", "en")
        finally:
            if had is None:
                os.environ.pop("FORCE_PROXY", None)
            else:
                os.environ["FORCE_PROXY"] = had
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            app.get_cached_transcript.cache_clear()


if __name__ == "__main__":
    unittest.main()
