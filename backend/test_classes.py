"""Tests for the class, profile and personal-progress endpoints.

These were the least-covered endpoints in the app despite carrying the most
authorization risk: they are the multi-tenant surface, where a mistake means one
teacher reads another teacher's roster or a student reads someone else's
progress. RLS (backend/db/rls_policies.sql) is a second line of defense, but the
backend talks to Supabase with the service-role key which bypasses RLS, so these
application-level checks are the ones that actually run in production.

Reuses the in-memory Supabase fake from test_assignments.py so the real endpoint
logic runs without a database.

Run from the backend directory:
    python -m pytest test_classes.py -v
"""
import unittest

import app
from test_assignments import AssignmentTestBase


class TestCreateClass(AssignmentTestBase):
    def test_teacher_creates_a_class(self):
        resp = self.client.post("/api/classes", json={"class_name": "French 201"},
                                headers=self.hdr())

        self.assertEqual(resp.status_code, 201)
        created = resp.get_json()["class"]
        self.assertEqual(created["class_name"], "French 201")
        # A join code must come back, or students have no way in.
        self.assertTrue(created.get("class_code"))

    def test_student_cannot_create_a_class(self):
        self.as_user("stud1")
        resp = self.client.post("/api/classes", json={"class_name": "Sneaky"},
                                headers=self.hdr())
        self.assertEqual(resp.status_code, 403)

    def test_class_name_is_required(self):
        resp = self.client.post("/api/classes", json={}, headers=self.hdr())
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_cannot_create(self):
        resp = self.client.post("/api/classes", json={"class_name": "X"})
        self.assertEqual(resp.status_code, 401)

    def test_join_codes_are_unique_across_classes(self):
        codes = set()
        for i in range(5):
            resp = self.client.post("/api/classes", json={"class_name": f"Class {i}"},
                                    headers=self.hdr())
            codes.add(resp.get_json()["class"]["class_code"])
        self.assertEqual(len(codes), 5, "duplicate join codes would send a student to the wrong class")


class TestListClasses(AssignmentTestBase):
    def test_teacher_sees_only_their_own_classes(self):
        # A second teacher with their own class.
        self.db.tables["user_profiles"].append(
            {"user_id": "teach2", "user_name": "Teacher Two", "user_role": "teacher"})
        self.db.tables["classes"].append(
            {"class_id": "c2", "class_name": "Other Teacher Class",
             "teacher_id": "teach2", "is_active": True})

        resp = self.client.get("/api/classes", headers=self.hdr())

        self.assertEqual(resp.status_code, 200)
        names = [c["class_name"] for c in resp.get_json()["classes"]]
        self.assertIn("Spanish 101", names)
        self.assertNotIn("Other Teacher Class", names,
                         "a teacher must not see another teacher's class")

    def test_student_listing_is_scoped_to_their_enrolments(self):
        """The list is built from the caller's own student_classes rows only.

        Asserted on the query rather than the payload: the endpoint flattens a
        PostgREST embedded join (classes(*)) that the in-memory fake does not
        synthesise, so the response body here is empty by construction. The
        security-relevant part is the filter, which is observable.
        """
        seen = []
        real_get = app._sb_get

        def spy(table, params=None):
            seen.append((table, dict(params or {})))
            return real_get(table, params)

        app._sb_get = spy
        try:
            self.as_user("stud1")
            self.client.get("/api/classes", headers=self.hdr())
        finally:
            app._sb_get = real_get

        enrolment_queries = [p for t, p in seen if t == "student_classes"]
        self.assertTrue(enrolment_queries, "expected the student's enrolments to be queried")
        self.assertEqual(enrolment_queries[0].get("student_id"), "eq.stud1",
                         "the enrolment query must be filtered to the caller")


class TestClassDetail(AssignmentTestBase):
    def test_teacher_sees_the_roster(self):
        resp = self.client.get("/api/classes/c1", headers=self.hdr())

        self.assertEqual(resp.status_code, 200)
        students = resp.get_json().get("students", [])
        # Names arrive via a PostgREST embedded join the fake doesn't synthesise,
        # so identify the roster by student_id, which the endpoint selects directly.
        self.assertEqual({s["student_id"] for s in students}, {"stud1", "stud2"})

    def test_other_teacher_cannot_read_the_roster(self):
        """The cross-tenant case: student names are personal data."""
        self.db.tables["user_profiles"].append(
            {"user_id": "teach2", "user_name": "Teacher Two", "user_role": "teacher"})
        self.as_user("teach2")

        resp = self.client.get("/api/classes/c1", headers=self.hdr())

        self.assertEqual(resp.status_code, 403)

    def test_enrolled_student_can_read_the_class(self):
        self.as_user("stud1")
        resp = self.client.get("/api/classes/c1", headers=self.hdr())
        self.assertEqual(resp.status_code, 200)

    def test_unenrolled_student_cannot_read_the_class(self):
        self.db.tables["user_profiles"].append(
            {"user_id": "stud9", "user_name": "Outsider", "user_role": "student"})
        self.as_user("stud9")

        resp = self.client.get("/api/classes/c1", headers=self.hdr())

        self.assertEqual(resp.status_code, 403)


class TestJoinClass(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.db.tables["classes"][0]["class_code"] = "ABC123"

    def test_student_joins_with_a_valid_code(self):
        self.db.tables["user_profiles"].append(
            {"user_id": "stud3", "user_name": "Student Three", "user_role": "student"})
        self.as_user("stud3")

        resp = self.client.post("/api/classes/join", json={"class_code": "ABC123"},
                                headers=self.hdr())

        self.assertIn(resp.status_code, (200, 201))
        enrolled = [r["student_id"] for r in self.db.tables["student_classes"]
                    if r["class_id"] == "c1"]
        self.assertIn("stud3", enrolled)

    def test_bad_code_is_rejected(self):
        self.as_user("stud1")
        resp = self.client.post("/api/classes/join", json={"class_code": "NOPE99"},
                                headers=self.hdr())
        self.assertEqual(resp.status_code, 404)

    def test_teacher_cannot_join_as_a_student(self):
        resp = self.client.post("/api/classes/join", json={"class_code": "ABC123"},
                                headers=self.hdr())
        self.assertEqual(resp.status_code, 403)

    def test_joining_twice_does_not_duplicate_enrolment(self):
        self.as_user("stud1")  # already enrolled in setUp
        self.client.post("/api/classes/join", json={"class_code": "ABC123"},
                         headers=self.hdr())

        rows = [r for r in self.db.tables["student_classes"]
                if r["class_id"] == "c1" and r["student_id"] == "stud1"]
        self.assertEqual(len(rows), 1, "a duplicate enrolment would double the roster")


class TestRemoveStudent(AssignmentTestBase):
    def test_owning_teacher_removes_a_student(self):
        resp = self.client.delete("/api/classes/c1/students/stud1", headers=self.hdr())

        self.assertIn(resp.status_code, (200, 204))
        remaining = [r["student_id"] for r in self.db.tables["student_classes"]
                     if r["class_id"] == "c1"]
        self.assertNotIn("stud1", remaining)
        self.assertIn("stud2", remaining)

    def test_other_teacher_cannot_remove(self):
        self.db.tables["user_profiles"].append(
            {"user_id": "teach2", "user_name": "Teacher Two", "user_role": "teacher"})
        self.as_user("teach2")

        resp = self.client.delete("/api/classes/c1/students/stud1", headers=self.hdr())

        self.assertEqual(resp.status_code, 403)
        self.assertIn("stud1", [r["student_id"] for r in self.db.tables["student_classes"]])

    def test_student_cannot_remove_a_classmate(self):
        self.as_user("stud1")
        resp = self.client.delete("/api/classes/c1/students/stud2", headers=self.hdr())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("stud2", [r["student_id"] for r in self.db.tables["student_classes"]])


class TestDeleteClass(AssignmentTestBase):
    def test_owning_teacher_deletes(self):
        resp = self.client.delete("/api/classes/c1", headers=self.hdr())
        self.assertIn(resp.status_code, (200, 204))

    def test_other_teacher_cannot_delete(self):
        self.db.tables["user_profiles"].append(
            {"user_id": "teach2", "user_name": "Teacher Two", "user_role": "teacher"})
        self.as_user("teach2")

        resp = self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertEqual(resp.status_code, 403)
        self.assertTrue([c for c in self.db.tables["classes"] if c["class_id"] == "c1"],
                        "the class must still exist")


class TestProfile(AssignmentTestBase):
    def test_user_reads_their_own_profile(self):
        resp = self.client.get("/api/profile", headers=self.hdr())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["profile"]["user_id"], "teach1")

    def test_profile_is_scoped_to_the_caller(self):
        """Reading a profile must never return another user's row."""
        self.as_user("stud1")
        body = self.client.get("/api/profile", headers=self.hdr()).get_json()
        self.assertEqual(body["profile"]["user_id"], "stud1")

    def test_rename_updates_only_the_caller(self):
        self.as_user("stud1")
        resp = self.client.patch("/api/profile/name", json={"user_name": "Renamed"},
                                 headers=self.hdr())

        self.assertEqual(resp.status_code, 200)
        by_id = {r["user_id"]: r["user_name"] for r in self.db.tables["user_profiles"]}
        self.assertEqual(by_id["stud1"], "Renamed")
        self.assertEqual(by_id["stud2"], "Student Two", "another user's name changed")

    def test_blank_name_rejected(self):
        resp = self.client.patch("/api/profile/name", json={"user_name": "   "},
                                 headers=self.hdr())
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_cannot_read_a_profile(self):
        resp = self.client.get("/api/profile")
        self.assertEqual(resp.status_code, 401)


class TestPersonalProgress(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.db.tables.setdefault("user_progress", [])

    def _upsert(self, **kw):
        body = {"youtube_id": "vid00000001", "transcript_language": "en",
                "translation_language": "es", "current_line_index": 3,
                "total_lines": 10}
        body.update(kw)
        return self.client.post("/api/progress/upsert", json=body, headers=self.hdr())

    def test_saves_progress_for_the_caller(self):
        self.as_user("stud1")
        resp = self._upsert()

        self.assertIn(resp.status_code, (200, 201))
        rows = self.db.tables["user_progress"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "stud1")

    def test_progress_is_never_written_under_another_user(self):
        """A caller-supplied user_id must not override the authenticated one."""
        self.as_user("stud1")
        self._upsert(user_id="stud2")

        owners = {r["user_id"] for r in self.db.tables["user_progress"]}
        self.assertEqual(owners, {"stud1"},
                         "progress was attributed to a different user")

    def test_reading_progress_returns_only_the_callers_rows(self):
        self.db.tables["user_progress"] = [
            {"id": "p1", "user_id": "stud1", "current_line_index": 1, "total_lines": 5,
             "transcript_language": "en", "translation_language": "es",
             "last_accessed_at": "2026-01-01T00:00:00Z"},
            {"id": "p2", "user_id": "stud2", "current_line_index": 2, "total_lines": 5,
             "transcript_language": "en", "translation_language": "es",
             "last_accessed_at": "2026-01-01T00:00:00Z"},
        ]
        self.as_user("stud1")

        body = self.client.get("/api/progress", headers=self.hdr()).get_json()

        self.assertTrue(all(r["user_id"] == "stud1" for r in body["progress"]))
        self.assertEqual(len(body["progress"]), 1)

    def test_unauthenticated_cannot_save_progress(self):
        resp = self.client.post("/api/progress/upsert",
                                json={"youtube_id": "vid00000001"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
