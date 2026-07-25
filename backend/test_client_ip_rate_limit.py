"""Tests that rate limiting keys on the real client, not the proxy.

The app runs behind one load balancer in production. Without ProxyFix,
get_remote_address() returns the PROXY's address for every request, so every
client shares a single rate-limit bucket: one busy user (or the same person on a
phone as well as a laptop) exhausts the limit for everybody. That presented as
"works on my computer, fails on my phone".

Run from the backend directory:
    python -m pytest test_client_ip_rate_limit.py -v
"""
import importlib
import os
import unittest


def _reload_app_with(env):
    """Reload app.py with the given env overrides so module-level limiter and
    proxy config (read at import) take effect. Returns the reloaded module."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    import app as _app
    mod = importlib.reload(_app)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


# One shared proxy address, as Render's load balancer would present.
PROXY_ADDR = "10.0.0.1"


class TestClientIpBehindProxy(unittest.TestCase):
    def setUp(self):
        self.app = _reload_app_with({
            "RATE_LIMIT_ENABLED": "1",
            "RATE_LIMIT_TRANSLATE": "3 per minute",
            "TRUSTED_PROXY_COUNT": "1",
            "REDIS_URL": "redis://127.0.0.1:6399/0",  # unreachable -> memory fallback
        })
        self.client = self.app.app.test_client()

    def tearDown(self):
        _reload_app_with({
            "RATE_LIMIT_TRANSLATE": "30 per minute;300 per hour",
            "TRUSTED_PROXY_COUNT": "1",
        })

    def _post(self, client_ip, extra_hops=()):
        # The proxy appends the caller's address last. extra_hops simulates
        # entries the CALLER supplied before the proxy's own append.
        forwarded = list(extra_hops) + [client_ip]
        return self.client.post(
            "/api/translate",
            json={"paragraphs": ["hola"], "from_lang": "es", "to_lang": "en"},
            environ_base={"REMOTE_ADDR": PROXY_ADDR},
            headers={"X-Forwarded-For": ", ".join(forwarded)},
        )

    def test_distinct_clients_get_their_own_budget(self):
        """The bug: phone and laptop shared one bucket, so the second device 429'd."""
        for _ in range(3):
            self.assertEqual(self._post("203.0.113.10").status_code, 200)

        # A different device must still have its full allowance.
        self.assertEqual(self._post("203.0.113.99").status_code, 200)

    def test_one_client_is_still_limited(self):
        for _ in range(3):
            self._post("203.0.113.10")
        self.assertEqual(self._post("203.0.113.10").status_code, 429)

    def test_spoofed_forwarded_entries_cannot_mint_new_buckets(self):
        """Only the proxy's own (last) entry is trusted.

        A caller that prepends fake addresses must not get a fresh bucket each
        time, which is exactly what trusting more hops than exist would allow.
        """
        for i in range(3):
            self._post("203.0.113.10", extra_hops=[f"9.9.9.{i}"])

        resp = self._post("203.0.113.10", extra_hops=["9.9.9.250"])
        self.assertEqual(resp.status_code, 429)

    def test_client_ip_is_read_from_the_request(self):
        """Sanity check that the proxy address itself is no longer the key.

        Asserted through a real request (test_client), not test_request_context:
        the latter builds the WSGI environ directly and never runs the middleware
        that rewrites REMOTE_ADDR, so it would report the proxy address whether
        the fix were present or not.
        """
        seen = {}

        @self.app.app.route("/__test_client_ip")
        def _echo_ip():
            from flask import request as flask_request
            seen["addr"] = flask_request.remote_addr
            return "", 204

        self.client.get(
            "/__test_client_ip",
            environ_base={"REMOTE_ADDR": PROXY_ADDR},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        self.assertEqual(seen["addr"], "203.0.113.7")


class TestNoTrustedProxy(unittest.TestCase):
    """With TRUSTED_PROXY_COUNT=0 the header must be ignored entirely."""

    def setUp(self):
        self.app = _reload_app_with({
            "RATE_LIMIT_ENABLED": "1",
            "RATE_LIMIT_TRANSLATE": "3 per minute",
            "TRUSTED_PROXY_COUNT": "0",
            "REDIS_URL": "redis://127.0.0.1:6399/0",
        })
        self.client = self.app.app.test_client()

    def tearDown(self):
        _reload_app_with({
            "RATE_LIMIT_TRANSLATE": "30 per minute;300 per hour",
            "TRUSTED_PROXY_COUNT": "1",
        })

    def test_forwarded_header_is_not_trusted(self):
        body = {"paragraphs": ["hola"], "from_lang": "es", "to_lang": "en"}
        for i in range(3):
            self.client.post(
                "/api/translate", json=body,
                environ_base={"REMOTE_ADDR": "198.51.100.5"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            )

        # Same socket address, new spoofed header: must still be limited.
        resp = self.client.post(
            "/api/translate", json=body,
            environ_base={"REMOTE_ADDR": "198.51.100.5"},
            headers={"X-Forwarded-For": "203.0.113.240"},
        )
        self.assertEqual(resp.status_code, 429)


if __name__ == "__main__":
    unittest.main()
