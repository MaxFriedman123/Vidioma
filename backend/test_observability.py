"""Tests for the health endpoint, security event logging, and request ids.

These exist so an incident is diagnosable. Before them the app used bare print()
with no levels, had no way to tell an authorization denial from a cache miss in a
log stream, and had no endpoint that answered "which dependency is broken?".

Run from the backend directory:
    python -m pytest test_observability.py -v
"""
import importlib
import os
import unittest
from unittest import mock

import jwt

import app


def _token(sub, secret="test-secret"):
    return jwt.encode({"sub": sub, "aud": "authenticated"}, secret, algorithm="HS256")


class HealthEndpointTest(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()
        self._ip = 0

    def _get(self):
        self._ip += 1
        return self.client.get("/health", environ_base={"REMOTE_ADDR": f"203.0.113.{self._ip}"})

    def test_reports_ok_when_dependencies_are_absent(self):
        """Redis and Supabase are optional, so absent is not degraded."""
        with mock.patch.object(app, "redis_client", None), \
             mock.patch.object(app, "supabase_ready", False):
            body = self._get().get_json()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["checks"]["redis"], "not_configured")
        self.assertEqual(body["checks"]["supabase"], "not_configured")

    def test_reports_degraded_when_a_configured_dependency_is_unreachable(self):
        broken = mock.Mock()
        broken.ping.side_effect = RuntimeError("connection refused")

        with mock.patch.object(app, "redis_client", broken):
            resp = self._get()
            body = resp.get_json()

        # Still 200: Redis is a cache, so a load balancer must not pull the
        # instance out of service over it.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertIn("redis", body["degraded"])

    def test_surfaces_deepl_cooldown(self):
        """A silent fallback to lower-quality engines must be visible."""
        with mock.patch.object(app, "DEEPL_API_KEY", "key:fx"), \
             mock.patch.object(app, "_DEEPL_COOLDOWN_UNTIL", float("inf")):
            body = self._get().get_json()

        self.assertEqual(body["checks"]["translator"], "deepl_cooling_down")

    def test_does_not_leak_configuration_detail(self):
        """The endpoint is unauthenticated, so no URLs, keys or versions."""
        with mock.patch.object(app, "CAPTION_RELAY_URL", "https://secret-relay.example.workers.dev"), \
             mock.patch.object(app, "DEEPL_API_KEY", "super-secret-key:fx"):
            text = self._get().get_data(as_text=True)

        self.assertNotIn("secret-relay", text)
        self.assertNotIn("super-secret-key", text)


class RequestIdTest(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def test_response_carries_a_request_id(self):
        resp = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.60"})
        self.assertTrue(resp.headers.get("X-Request-Id"))

    def test_caller_supplied_id_is_echoed(self):
        resp = self.client.get("/", headers={"X-Request-Id": "abc-123"},
                               environ_base={"REMOTE_ADDR": "203.0.113.61"})
        self.assertEqual(resp.headers["X-Request-Id"], "abc-123")

    def test_malicious_id_is_replaced_not_reflected(self):
        """A caller must not be able to inject text into the log stream.

        Set on the WSGI environ rather than via headers=: werkzeug's test client
        refuses to build a request with a newline in a header value, so passing it
        that way would only test werkzeug. A real client can still put arbitrary
        bytes on the wire, which is what this simulates.
        """
        for hostile in ("bad\nevent=auth.ok outcome=success",
                        "id with spaces",
                        "a" * 200,
                        "../../etc/passwd"):
            resp = self.client.get("/", environ_base={
                "REMOTE_ADDR": "203.0.113.62",
                "HTTP_X_REQUEST_ID": hostile,
            })
            rid = resp.headers["X-Request-Id"]
            self.assertNotEqual(rid, hostile, f"hostile id was reflected: {hostile!r}")
            self.assertRegex(rid, r"^[A-Za-z0-9._-]{1,64}$")

    def test_wellformed_caller_id_is_still_honoured(self):
        resp = self.client.get("/", environ_base={
            "REMOTE_ADDR": "203.0.113.63",
            "HTTP_X_REQUEST_ID": "trace-abc_123.4",
        })
        self.assertEqual(resp.headers["X-Request-Id"], "trace-abc_123.4")


class SecurityLoggingTest(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def test_missing_auth_header_is_logged(self):
        with self.assertLogs("vidioma.security", level="WARNING") as cm:
            self.client.get("/api/progress", environ_base={"REMOTE_ADDR": "203.0.113.70"})
        joined = "\n".join(cm.output)
        self.assertIn("event=auth.header_missing", joined)
        self.assertIn("outcome=failure", joined)

    def test_invalid_token_is_logged(self):
        """A forged/tampered token is the event worth alerting on."""
        with mock.patch.object(app, "SUPABASE_JWT_SECRET", "test-secret"), \
             mock.patch.object(app, "_jwks_client", None):
            with self.assertLogs("vidioma.security", level="WARNING") as cm:
                self.client.get(
                    "/api/progress",
                    headers={"Authorization": f"Bearer {_token('u1', secret='WRONG-secret')}"},
                    environ_base={"REMOTE_ADDR": "203.0.113.71"},
                )
        self.assertIn("event=auth.token_invalid", "\n".join(cm.output))

    def test_security_log_records_ip_and_path_but_not_the_token(self):
        token = _token("u1", secret="WRONG-secret")
        with mock.patch.object(app, "SUPABASE_JWT_SECRET", "test-secret"), \
             mock.patch.object(app, "_jwks_client", None):
            with self.assertLogs("vidioma.security", level="WARNING") as cm:
                self.client.get("/api/progress",
                                headers={"Authorization": f"Bearer {token}"},
                                environ_base={"REMOTE_ADDR": "203.0.113.72"})
        joined = "\n".join(cm.output)
        self.assertIn("ip=203.0.113.72", joined)
        self.assertIn("path=GET /api/progress", joined)
        self.assertNotIn(token, joined)


class PerUserRateLimitTest(unittest.TestCase):
    """A classroom shares one public IP, so signed-in users need their own bucket."""

    @classmethod
    def setUpClass(cls):
        cls.saved = {k: os.environ.get(k) for k in
                     ("RATE_LIMIT_ENABLED", "RATE_LIMIT_TRANSLATE", "REDIS_URL", "SUPABASE_JWT_SECRET")}
        os.environ.update({
            "RATE_LIMIT_ENABLED": "1",
            "RATE_LIMIT_TRANSLATE": "2 per minute",
            "REDIS_URL": "redis://127.0.0.1:6399/0",  # unreachable -> memory fallback
            "SUPABASE_JWT_SECRET": "test-secret",
        })
        cls.mod = importlib.reload(app)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(app)

    def setUp(self):
        self.client = self.mod.app.test_client()
        self.body = {"paragraphs": ["hola"], "from_lang": "es", "to_lang": "en"}

    def _post(self, sub=None, ip="203.0.113.90"):
        headers = {"Authorization": f"Bearer {_token(sub)}"} if sub else {}
        return self.client.post("/api/translate", json=self.body, headers=headers,
                                environ_base={"REMOTE_ADDR": ip})

    def test_two_users_on_one_ip_do_not_throttle_each_other(self):
        # Exhaust user A's 2/minute budget.
        self.assertEqual(self._post(sub="user-a").status_code, 200)
        self.assertEqual(self._post(sub="user-a").status_code, 200)
        self.assertEqual(self._post(sub="user-a").status_code, 429)

        # A different student on the SAME network still has their own budget.
        self.assertEqual(self._post(sub="user-b").status_code, 200)

    def test_one_user_is_still_limited_across_devices(self):
        """Per-user keying must also mean a user can't get more by switching IP."""
        self.assertEqual(self._post(sub="user-c", ip="203.0.113.91").status_code, 200)
        self.assertEqual(self._post(sub="user-c", ip="203.0.113.92").status_code, 200)
        self.assertEqual(self._post(sub="user-c", ip="203.0.113.93").status_code, 429)

    def test_anonymous_traffic_is_still_limited_by_ip(self):
        self.assertEqual(self._post(ip="203.0.113.95").status_code, 200)
        self.assertEqual(self._post(ip="203.0.113.95").status_code, 200)
        self.assertEqual(self._post(ip="203.0.113.95").status_code, 429)
        # A different address is unaffected.
        self.assertEqual(self._post(ip="203.0.113.96").status_code, 200)

    def test_forged_token_falls_back_to_ip_not_a_fresh_bucket(self):
        """An unverifiable token must not mint an unlimited number of buckets."""
        forged = jwt.encode({"sub": "anything"}, "WRONG-secret", algorithm="HS256")
        codes = []
        for _ in range(3):
            codes.append(self.client.post(
                "/api/translate", json=self.body,
                headers={"Authorization": f"Bearer {forged}"},
                environ_base={"REMOTE_ADDR": "203.0.113.99"},
            ).status_code)
        self.assertIn(429, codes, "forged tokens must fall back to the IP bucket")


if __name__ == "__main__":
    unittest.main()
