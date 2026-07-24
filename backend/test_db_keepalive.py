"""Tests for the /api/db-keepalive endpoint.

The endpoint exists so an external cron (cloudflare-worker-keepalive/) can keep a
Supabase free-plan project from pausing after 7 idle days. The Supabase REST
layer is stubbed so we exercise the real endpoint logic without a DB.

Run from the backend directory:
    python test_db_keepalive.py
or with pytest:
    python -m pytest test_db_keepalive.py -v
"""

import unittest

import app


class DbKeepaliveTest(unittest.TestCase):
    # Class-level, not per-test: the limiter counts per IP across the whole
    # process, so a counter reset in setUp would hand every test the same first
    # address and exhaust the limit partway through the suite.
    _ip_seq = 0

    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()
        self._orig_get = app._sb_get
        self._orig_ready = app.supabase_ready
        self.calls = []

    def tearDown(self):
        app._sb_get = self._orig_get
        app.supabase_ready = self._orig_ready

    def _stub_db(self, result=None, error=None):
        def fake_get(table, params=None):
            self.calls.append((table, params))
            if error:
                raise error
            return result if result is not None else [{"id": "vid-1"}]

        app._sb_get = fake_get
        app.supabase_ready = True

    def _ping(self, method="post"):
        """Issue the request from a fresh IP.

        The endpoint's real "6 per hour" limit is deliberately tight, and the
        limiter is per-IP, so reusing one address would exhaust it partway
        through the suite and turn later assertions into spurious 429s. Same
        approach as test_rate_limiting.py's per-IP cases.
        """
        type(self)._ip_seq += 1
        return getattr(self.client, method)(
            "/api/db-keepalive",
            environ_base={"REMOTE_ADDR": f"203.0.113.{type(self)._ip_seq}"},
        )

    def test_ping_reads_one_row_and_reports_ok(self):
        self._stub_db()

        resp = self._ping()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertIn("pinged_at", resp.get_json())
        # Must actually hit the DB -- a ping that touches nothing would let the
        # project pause while still reporting success.
        self.assertEqual(len(self.calls), 1)
        table, params = self.calls[0]
        self.assertEqual(table, "videos")
        # Cheapest possible read: one column, one row.
        self.assertEqual(params, {"select": "id", "limit": "1"})

    def test_get_also_allowed(self):
        """Browsers and some uptime pingers can only issue GET."""
        self._stub_db()

        resp = self._ping("get")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_db_error_returns_502(self):
        """A failed ping must not look like a successful run."""
        self._stub_db(error=Exception("Supabase GET videos failed (401): bad key"))

        resp = self._ping()

        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()["ok"])

    def test_db_error_does_not_leak_internals(self):
        self._stub_db(error=Exception("Supabase GET videos failed (401): service_role key xyz"))

        resp = self._ping()

        self.assertNotIn("xyz", resp.get_data(as_text=True))

    def test_unconfigured_db_returns_503(self):
        app.supabase_ready = False

        resp = self._ping()

        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()["ok"])

    def test_empty_table_still_counts_as_a_ping(self):
        """A brand-new project with no videos rows still reached the DB."""
        self._stub_db(result=[])

        resp = self._ping()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_requires_no_auth(self):
        """The caller is a cron job with no user identity, so no token is sent."""
        self._stub_db()

        resp = self._ping()  # no Authorization header

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
