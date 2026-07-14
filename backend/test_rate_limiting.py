"""Tests for the abuse-protection layer added to the public endpoints:
per-IP rate limiting, the /api/translate size caps, and MAX_CONTENT_LENGTH.

Run from the backend directory:
    python -m pytest test_rate_limiting.py -v
"""
import importlib
import os
import unittest


def _reload_app_with(env):
    """Reload app.py with the given env overrides so module-level limiter config
    (read at import) takes effect. Returns the reloaded module."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    import app as _app
    mod = importlib.reload(_app)
    # restore env for the next reload
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


class TestRateLimiting(unittest.TestCase):
    def setUp(self):
        # Tight, deterministic limit; memory storage so no Redis needed.
        self.app = _reload_app_with({
            "RATE_LIMIT_ENABLED": "1",
            "RATE_LIMIT_CAPTION_TRACKS": "3 per minute",
            "RATE_LIMIT_TRANSLATE": "3 per minute",
            "REDIS_URL": "redis://127.0.0.1:6399/0",  # unreachable -> memory fallback
        })
        self.client = self.app.app.test_client()
        # Stub the network-touching selector so the endpoint is pure.
        self.app.select_caption_track_url = lambda vid, lang: {
            "url": "https://yt/timedtext?lang=es&fmt=srv3",
            "is_correct_lang": True, "tlang": None, "language_code": "es",
        }

    def tearDown(self):
        # Reset to defaults so other test modules see a normal app.
        _reload_app_with({"RATE_LIMIT_CAPTION_TRACKS": "60 per minute;600 per hour"})

    def _post(self, ip="7.7.7.7"):
        return self.client.post(
            "/api/caption-tracks",
            json={"url": "dQw4w9WgXcQ", "from_lang": "es"},
            environ_base={"REMOTE_ADDR": ip},
        )

    def test_requests_under_limit_pass(self):
        for _ in range(3):
            self.assertEqual(self._post().status_code, 200)

    def test_request_over_limit_gets_429(self):
        for _ in range(3):
            self._post()
        resp = self._post()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("too quickly", resp.get_json()["error"])

    def test_limit_is_per_ip(self):
        # Exhaust IP A, a different IP B is unaffected.
        for _ in range(4):
            self._post(ip="1.1.1.1")
        self.assertEqual(self._post(ip="2.2.2.2").status_code, 200)


class TestRateLimitDisabled(unittest.TestCase):
    def test_disabled_flag_makes_endpoints_unthrottled(self):
        app = _reload_app_with({
            "RATE_LIMIT_ENABLED": "0",
            "RATE_LIMIT_CAPTION_TRACKS": "1 per minute",
        })
        try:
            self.assertIsNone(app.limiter)
            app.select_caption_track_url = lambda vid, lang: {
                "url": "u", "is_correct_lang": True, "tlang": None, "language_code": "es",
            }
            client = app.app.test_client()
            for _ in range(5):  # well over the "1 per minute" that would apply if enabled
                r = client.post("/api/caption-tracks",
                                 json={"url": "dQw4w9WgXcQ", "from_lang": "es"},
                                 environ_base={"REMOTE_ADDR": "3.3.3.3"})
                self.assertEqual(r.status_code, 200)
        finally:
            _reload_app_with({"RATE_LIMIT_ENABLED": "1"})


class TestTranslateSizeCaps(unittest.TestCase):
    def setUp(self):
        self.app = _reload_app_with({"RATE_LIMIT_ENABLED": "0"})  # isolate the size caps
        self.client = self.app.app.test_client()

    def tearDown(self):
        _reload_app_with({"RATE_LIMIT_ENABLED": "1"})

    def test_too_many_paragraphs_rejected_413(self):
        n = self.app._MAX_TRANSLATE_PARAGRAPHS + 1
        resp = self.client.post("/api/translate", json={
            "paragraphs": ["x"] * n, "from_lang": "es", "to_lang": "en",
        })
        self.assertEqual(resp.status_code, 413)
        self.assertIn("Too many paragraphs", resp.get_json()["error"])

    def test_total_chars_over_cap_rejected_413(self):
        # A handful of paragraphs whose combined length exceeds the char cap.
        big = "a" * (self.app._MAX_TRANSLATE_TOTAL_CHARS // 4 + 1)
        resp = self.client.post("/api/translate", json={
            "paragraphs": [big, big, big, big], "from_lang": "es", "to_lang": "en",
        })
        self.assertEqual(resp.status_code, 413)
        self.assertIn("too large", resp.get_json()["error"].lower())

    def test_within_caps_not_rejected_for_size(self):
        # Stub the translation so we exercise ONLY the size gate, not DeepL.
        self.app.translate_with_alignment = lambda paras, lines, target, source_lang="auto": (
            list(paras), [[] for _ in paras])
        resp = self.client.post("/api/translate", json={
            "paragraphs": ["hola", "mundo"], "from_lang": "es", "to_lang": "en",
        })
        self.assertNotEqual(resp.status_code, 413)


class TestMaxContentLength(unittest.TestCase):
    def test_oversized_body_rejected(self):
        app = _reload_app_with({
            "RATE_LIMIT_ENABLED": "0",
            "MAX_CONTENT_LENGTH_BYTES": "1024",  # 1 KB cap for the test
        })
        try:
            client = app.app.test_client()
            resp = client.post(
                "/api/translate",
                data=b'{"paragraphs":["' + b"a" * 4000 + b'"]}',
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 413)
        finally:
            _reload_app_with({"MAX_CONTENT_LENGTH_BYTES": str(8 * 1024 * 1024)})


if __name__ == "__main__":
    unittest.main()
