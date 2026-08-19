"""Pins the HTTP surface so refactoring cannot silently change it.

app.py became the app/ package so it can be split into modules over time. Moving
route definitions between modules is easy to get subtly wrong: a blueprint
registered under the wrong prefix, a decorator dropped in a move, a duplicated
rule. None of those fail any other test, because every other test drives the app
through a known-good URL.

This asserts the exact rule set. If a refactor changes it, this fails with a diff
of what appeared or vanished. Deliberate changes update EXPECTED_RULES in the same
commit, which makes the API surface change reviewable rather than incidental.

Run from the backend directory:
    python -m pytest test_route_table.py -v
"""
import unittest

import app

# (sorted methods, rule). Static endpoint excluded: Flask adds it automatically
# and it is not part of the app's own surface.
EXPECTED_RULES = {
    ("GET,HEAD,OPTIONS", "/"),
    ("GET,HEAD,OPTIONS", "/health"),
    ("GET,HEAD,OPTIONS,POST", "/api/db-keepalive"),
    ("OPTIONS,POST", "/api/transcript"),
    ("OPTIONS,POST", "/api/caption-tracks"),
    ("OPTIONS,POST", "/api/translate"),
    ("OPTIONS,POST", "/api/admin/clear-translation-cache"),
    ("GET,HEAD,OPTIONS", "/api/progress"),
    ("OPTIONS,POST", "/api/progress/upsert"),
    ("GET,HEAD,OPTIONS", "/api/progress/<youtube_id>"),
    ("GET,HEAD,OPTIONS", "/api/profile"),
    ("OPTIONS,POST", "/api/profile"),
    ("OPTIONS,PATCH", "/api/profile/name"),
    ("GET,HEAD,OPTIONS", "/api/classes"),
    ("OPTIONS,POST", "/api/classes"),
    ("GET,HEAD,OPTIONS", "/api/classes/<class_id>"),
    ("DELETE,OPTIONS", "/api/classes/<class_id>"),
    ("OPTIONS,POST", "/api/classes/join"),
    ("DELETE,OPTIONS", "/api/classes/<class_id>/students/<student_id>"),
    ("GET,HEAD,OPTIONS", "/api/assignments"),
    ("OPTIONS,POST", "/api/assignments"),
    ("GET,HEAD,OPTIONS", "/api/assignments/<assignment_id>"),
    ("DELETE,OPTIONS", "/api/assignments/<assignment_id>"),
    ("OPTIONS,POST", "/api/assignments/<assignment_id>/progress"),
}


def _actual_rules():
    return {
        (",".join(sorted(r.methods)), str(r.rule))
        for r in app.app.url_map.iter_rules()
        if r.endpoint != "static"
    }


class RouteTableTest(unittest.TestCase):
    def test_http_surface_is_unchanged(self):
        actual = _actual_rules()

        added = actual - EXPECTED_RULES
        removed = EXPECTED_RULES - actual
        self.assertEqual(
            (added, removed), (set(), set()),
            f"\n  routes ADDED (not in EXPECTED_RULES): {sorted(added)}"
            f"\n  routes MISSING (expected but absent): {sorted(removed)}"
            "\n  If this change is intended, update EXPECTED_RULES in this file.",
        )

    def test_no_duplicate_rules(self):
        """Two rules for one path is how a botched blueprint move presents."""
        paths = [str(r.rule) for r in app.app.url_map.iter_rules() if r.endpoint != "static"]
        methods_by_path = {}
        for r in app.app.url_map.iter_rules():
            if r.endpoint == "static":
                continue
            for m in r.methods - {"HEAD", "OPTIONS"}:
                key = (m, str(r.rule))
                self.assertNotIn(
                    key, methods_by_path,
                    f"{m} {r.rule} is registered twice "
                    f"({methods_by_path.get(key)} and {r.endpoint})",
                )
                methods_by_path[key] = r.endpoint
        self.assertTrue(paths)

    def test_wsgi_entrypoint_exposes_the_app(self):
        """gunicorn's target must keep working after the package conversion.

        The deployed start command lives in the host's dashboard, not in this
        repo, so a broken import path here would only surface as a failed deploy.
        """
        import wsgi
        self.assertIs(wsgi.app, app.app)


if __name__ == "__main__":
    unittest.main()
