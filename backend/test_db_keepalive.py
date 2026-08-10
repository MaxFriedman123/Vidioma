"""Tests for the /api/db-keepalive endpoint.

The endpoint exists so an external cron (cloudflare-worker-keepalive/) can keep a
Supabase free-plan project from pausing for low activity over a 7-day window. The
Supabase REST layer is stubbed so we exercise the real endpoint logic without a DB.

These tests pin the endpoint's CONTRACT, not the reason the pause kept happening.
The actual root cause was cron cadence: Supabase counts "user requests to the
database each day", so calling this every 2 days left 5 of every 7 days empty and
the project was flagged both as a read and as an upsert. That fix lives in
`cloudflare-worker-keepalive/wrangler.toml` and no unit test here can cover it.

What is still worth pinning: the ping writes exactly one row to the sentinel
table (`ping_count` is the diagnostic that separates cron runs from a manual
curl), it never touches user tables, and a successful write is never reported as
a failure.

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
        self._orig_post = app._sb_post
        self._orig_ready = app.supabase_ready
        self.reads = []
        self.writes = []

    def tearDown(self):
        app._sb_get = self._orig_get
        app._sb_post = self._orig_post
        app.supabase_ready = self._orig_ready

    def _stub_db(self, prev_count=7, error=None, read_error=None):
        """Stub the REST layer.

        `error` fails the WRITE (the ping itself). `read_error` fails only the
        incidental ping_count lookup, which must not stop the write.
        """
        def fake_get(table, params=None):
            self.reads.append((table, params))
            if read_error:
                raise read_error
            return [{"ping_count": prev_count}]

        def fake_post(table, data, extra_headers=None, params=None):
            self.writes.append((table, data, extra_headers, params))
            if error:
                raise error
            return []

        app._sb_get = fake_get
        app._sb_post = fake_post
        app.supabase_ready = True

    def _ping(self, method="post"):
        """Issue the request from a fresh IP.

        Locally the limiter keys on the real client address, so a fresh IP per
        request keeps the endpoint's own limit from being exhausted partway
        through the suite and turning later assertions into spurious 429s. Same
        approach as test_rate_limiting.py's per-IP cases.
        """
        type(self)._ip_seq += 1
        return getattr(self.client, method)(
            "/api/db-keepalive",
            environ_base={"REMOTE_ADDR": f"203.0.113.{type(self)._ip_seq}"},
        )

    def test_ping_writes_the_heartbeat_row(self):
        """One upsert to the sentinel row, so ping_count stays a usable counter."""
        self._stub_db(prev_count=7)

        resp = self._ping()

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("pinged_at", body)

        # Exactly one write, to the sentinel table, as an upsert on id=1.
        self.assertEqual(len(self.writes), 1)
        table, data, headers, params = self.writes[0]
        self.assertEqual(table, "keepalive")
        self.assertEqual(data["id"], 1)
        self.assertIn("pinged_at", data)
        self.assertEqual(params, {"on_conflict": "id"})
        self.assertIn("resolution=merge-duplicates", headers["Prefer"])

    def test_write_asks_for_a_representation_not_minimal(self):
        """`return=minimal` makes a SUCCESSFUL write look like a 502.

        _sb_post ends in resp.json(); with `Prefer: return=minimal` PostgREST
        replies 201 with an empty body, the parse raises, and the endpoint
        reports failure for a write that actually landed. That is the worst
        possible failure mode here — the cron's run history would show errors
        while the DB was being touched fine, or (if the row write were skipped)
        the reverse. Found only by running against a real PostgREST-shaped
        server; stubbing _sb_post hides it entirely.
        """
        self._stub_db()

        self._ping()

        prefer = self.writes[0][2]["Prefer"]
        self.assertIn("return=representation", prefer)
        self.assertNotIn("return=minimal", prefer)

    def test_ping_count_increments(self):
        self._stub_db(prev_count=7)

        body = self._ping().get_json()

        self.assertEqual(body["ping_count"], 8)
        self.assertEqual(self.writes[0][1]["ping_count"], 8)

    def test_ping_does_not_write_to_user_tables(self):
        """A heartbeat in a real table would show up in user-facing queries."""
        self._stub_db()

        self._ping()

        for table, _data, _h, _p in self.writes:
            self.assertEqual(table, "keepalive")

    def test_write_still_happens_when_count_lookup_fails(self):
        """The read is incidental; only the write counts as activity."""
        self._stub_db(read_error=Exception("Supabase GET keepalive failed (500): boom"))

        resp = self._ping()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.writes), 1)
        # Falls back to 0, so the row still gets a fresh timestamp.
        self.assertEqual(self.writes[0][1]["ping_count"], 1)

    def test_missing_row_still_pings(self):
        """A fresh project with no seeded row must still get a write."""
        def fake_get(table, params=None):
            self.reads.append((table, params))
            return []          # no row yet

        def fake_post(table, data, extra_headers=None, params=None):
            self.writes.append((table, data, extra_headers, params))
            return []

        app._sb_get, app._sb_post, app.supabase_ready = fake_get, fake_post, True

        resp = self._ping()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.writes[0][1]["ping_count"], 1)

    def test_get_also_allowed(self):
        """Browsers and some uptime pingers can only issue GET."""
        self._stub_db()

        resp = self._ping("get")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_db_error_returns_502(self):
        """A failed ping must not look like a successful run."""
        self._stub_db(error=Exception("Supabase POST keepalive failed (401): bad key"))

        resp = self._ping()

        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()["ok"])

    def test_db_error_does_not_leak_internals(self):
        self._stub_db(error=Exception("Supabase POST keepalive failed (401): service_role key xyz"))

        resp = self._ping()

        self.assertNotIn("xyz", resp.get_data(as_text=True))

    def test_unconfigured_db_returns_503(self):
        app.supabase_ready = False

        resp = self._ping()

        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()["ok"])

    def test_requires_no_auth(self):
        """The caller is a cron job with no user identity, so no token is sent."""
        self._stub_db()

        resp = self._ping()  # no Authorization header

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
