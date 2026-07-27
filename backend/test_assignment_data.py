"""Tests for assignment data integrity: the progress denominator, class deletion
cleanup, and the batched teacher listing.

Three distinct data problems live here, all invisible from the happy path:

  total_lines   assignments.total_lines existed in the schema but no endpoint
                ever wrote it, so every teacher percentage divided by 0 and a
                class that had not started read as 0% forever.
  orphaning     deleting a class left its assignments and the students' progress
                rows behind with no reachable target: nothing lists them and
                nothing can delete them, which in a school is undeleted student
                data.
  query fan-out the teacher listing issued 1 + 2 queries per assignment, each
                with its own connect+read timeout.

Reuses the in-memory Supabase fake from test_assignments.py so the real endpoint
logic runs without a database.

Run from the backend directory:
    python -m pytest test_assignment_data.py -v
"""
import unittest

import app
from test_assignments import AssignmentTestBase


class AssignmentDataTestBase(AssignmentTestBase):
    """Adds a rate-limiter reset around each test.

    The limiter's default budget (RATE_LIMIT_DEFAULT, keyed per user) lives in
    process memory, so it accumulates across a whole pytest session rather than
    resetting per test. The batching tests below create enough assignments as one
    user to exhaust the per-minute bucket, which would otherwise surface as 429s
    in whichever test file happened to run next. Clearing it on both sides keeps
    these tests independent of file ordering without weakening the real limit.
    """

    def setUp(self):
        super().setUp()
        self._reset_rate_limiter()
        self.addCleanup(self._reset_rate_limiter)

    @staticmethod
    def _reset_rate_limiter():
        storage = getattr(app.limiter, "storage", None) if app.limiter else None
        if storage is not None:
            storage.reset()


class TestAssignmentTotalLines(AssignmentDataTestBase):
    """assignments.total_lines is the denominator of every % a teacher reads."""

    def _create(self, **body):
        payload = {"url": "https://youtu.be/jNQXAC9I4Ss", "title": "Zoo", "class_ids": ["c1"]}
        payload.update(body)
        return self.client.post("/api/assignments", json=payload, headers=self.hdr())

    def _assignment_row(self):
        return self.db.tables["assignments"][0]

    def test_create_stores_total_lines(self):
        resp = self._create(total_lines=42)

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._assignment_row()["total_lines"], 42)
        self.assertEqual(resp.get_json()["assignment"]["total_lines"], 42)

    def test_create_without_total_lines_stores_zero(self):
        self._create()
        self.assertEqual(self._assignment_row()["total_lines"], 0)

    def test_create_coerces_bad_total_lines(self):
        for bad in (-5, "abc", None, [3], 7.9):
            self.db.tables["assignments"] = []
            self._create(total_lines=bad)
            stored = self._assignment_row()["total_lines"]
            self.assertIsInstance(stored, int, f"total_lines={bad!r} stored a non-integer")
            self.assertGreaterEqual(stored, 0, f"total_lines={bad!r} stored a negative")

    def test_create_caps_total_lines(self):
        self._create(total_lines=10_000_000)
        self.assertEqual(self._assignment_row()["total_lines"], app.MAX_ASSIGNMENT_TOTAL_LINES)

    def test_first_student_save_backfills_total_lines(self):
        """The teacher assigns before anything has read the captions, so the real
        line count is only known once a student starts."""
        self._create()
        aid = self._assignment_row()["assignment_id"]
        self.as_user("stud1")

        self.client.post(f"/api/assignments/{aid}/progress",
                         json={"current_line_index": 1, "total_lines": 12},
                         headers=self.hdr())

        self.assertEqual(self._assignment_row()["total_lines"], 12,
                         "the teacher's view still divides by 0 after a student started")

    def test_backfill_also_fires_when_total_lines_is_null(self):
        self._create()
        self.db.tables["assignments"][0]["total_lines"] = None
        aid = self._assignment_row()["assignment_id"]
        self.as_user("stud1")

        self.client.post(f"/api/assignments/{aid}/progress",
                         json={"current_line_index": 1, "total_lines": 12},
                         headers=self.hdr())

        self.assertEqual(self._assignment_row()["total_lines"], 12)

    def test_backfill_never_overwrites_an_existing_value(self):
        """A crafted save must not rewrite the denominator the rest of the class
        is already being measured against."""
        self._create(total_lines=10)
        aid = self._assignment_row()["assignment_id"]
        self.as_user("stud1")

        self.client.post(f"/api/assignments/{aid}/progress",
                         json={"current_line_index": 1, "total_lines": 99999},
                         headers=self.hdr())

        self.assertEqual(self._assignment_row()["total_lines"], 10)

    def test_backfill_is_capped(self):
        self._create()
        aid = self._assignment_row()["assignment_id"]
        self.as_user("stud1")

        self.client.post(f"/api/assignments/{aid}/progress",
                         json={"current_line_index": 1, "total_lines": 10_000_000},
                         headers=self.hdr())

        self.assertEqual(self._assignment_row()["total_lines"], app.MAX_ASSIGNMENT_TOTAL_LINES)

    def test_a_save_reporting_no_total_lines_does_not_backfill(self):
        self._create()
        aid = self._assignment_row()["assignment_id"]
        self.as_user("stud1")

        self.client.post(f"/api/assignments/{aid}/progress",
                         json={"current_line_index": 1},
                         headers=self.hdr())

        self.assertEqual(self._assignment_row()["total_lines"], 0)

    def test_a_non_target_cannot_backfill(self):
        self._create()
        aid = self._assignment_row()["assignment_id"]
        self.db.tables["user_profiles"].append(
            {"user_id": "outsider", "user_name": "X", "user_role": "student"})
        self.as_user("outsider")

        resp = self.client.post(f"/api/assignments/{aid}/progress",
                                json={"current_line_index": 1, "total_lines": 77},
                                headers=self.hdr())

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._assignment_row()["total_lines"], 0)


class TestDeleteClassCleansUpAssignments(AssignmentDataTestBase):
    """Deleting a class must not leave unreachable assignment/progress rows."""

    def setUp(self):
        super().setUp()
        # A second class owned by the same teacher, for the multi-target cases.
        self.db.tables["classes"].append(
            {"class_id": "c2", "class_name": "French 101", "teacher_id": "teach1", "is_active": True})
        self.db.tables["student_classes"].append(
            {"student_class_id": "sc3", "class_id": "c2", "student_id": "stud2"})

    def _create(self, **body):
        payload = {"url": "https://youtu.be/jNQXAC9I4Ss", "title": "Zoo"}
        payload.update(body)
        resp = self.client.post("/api/assignments", json=payload, headers=self.hdr())
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()["assignment"]["assignment_id"]

    def _add_progress(self, assignment_id, student_id):
        self.db.tables["assignment_progress"].append({
            "assignment_id": assignment_id, "student_id": student_id,
            "current_line_index": 2, "max_line_reached": 2, "total_lines": 10,
            "completed": False,
        })

    def _ids(self, table, key):
        return {r[key] for r in self.db.tables[table]}

    def test_class_only_assignment_and_its_progress_are_deleted(self):
        aid = self._create(class_ids=["c1"])
        self._add_progress(aid, "stud1")

        resp = self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertIn(resp.status_code, (200, 204))
        self.assertNotIn(aid, self._ids("assignments", "assignment_id"),
                         "the assignment outlived its only class and is now unreachable")
        self.assertNotIn(aid, self._ids("assignment_progress", "assignment_id"),
                         "student progress rows outlived their assignment")
        self.assertNotIn(aid, self._ids("assignment_targets", "assignment_id"))

    def test_individually_targeted_assignment_is_deleted_with_its_class(self):
        aid = self._create(student_targets=[{"class_id": "c1", "student_id": "stud1"}])
        self._add_progress(aid, "stud1")

        self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertNotIn(aid, self._ids("assignments", "assignment_id"))
        self.assertNotIn(aid, self._ids("assignment_progress", "assignment_id"))

    def test_assignment_targeting_another_class_survives(self):
        """assignment_targets has one row per class, so a multi-class assignment
        must lose only the deleted class's target."""
        aid = self._create(class_ids=["c1", "c2"])
        self._add_progress(aid, "stud2")

        self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertIn(aid, self._ids("assignments", "assignment_id"),
                      "an assignment still targeted at c2 was deleted with c1")
        self.assertIn(aid, self._ids("assignment_progress", "assignment_id"),
                      "progress was deleted for an assignment that still exists")
        remaining = {(t["assignment_id"], t["class_id"])
                     for t in self.db.tables["assignment_targets"]}
        self.assertIn((aid, "c2"), remaining)
        self.assertNotIn((aid, "c1"), remaining, "the deleted class's target survived")

    def test_survives_when_the_other_target_is_an_individual_student(self):
        aid = self._create(class_ids=["c1"],
                           student_targets=[{"class_id": "c2", "student_id": "stud2"}])

        self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertIn(aid, self._ids("assignments", "assignment_id"))
        self.assertEqual({(t["assignment_id"], t["class_id"])
                          for t in self.db.tables["assignment_targets"]},
                         {(aid, "c2")})

    def test_mixed_set_deletes_only_the_unreachable_assignments(self):
        doomed = self._create(class_ids=["c1"])
        doomed_individual = self._create(
            student_targets=[{"class_id": "c1", "student_id": "stud1"}])
        shared = self._create(class_ids=["c1", "c2"])
        untouched = self._create(class_ids=["c2"])
        for aid in (doomed, doomed_individual, shared, untouched):
            self._add_progress(aid, "stud2")

        self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertEqual(self._ids("assignments", "assignment_id"), {shared, untouched})
        self.assertEqual(self._ids("assignment_progress", "assignment_id"), {shared, untouched})
        self.assertEqual({t["class_id"] for t in self.db.tables["assignment_targets"]}, {"c2"})

    def test_class_with_no_assignments_still_deletes(self):
        resp = self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertIn(resp.status_code, (200, 204))
        self.assertEqual(self.db.tables["classes"], [c for c in self.db.tables["classes"]
                                                     if c["class_id"] == "c2"])

    def test_a_rejected_delete_touches_nothing(self):
        aid = self._create(class_ids=["c1"])
        self._add_progress(aid, "stud1")
        self.db.tables["user_profiles"].append(
            {"user_id": "teach2", "user_name": "Teacher Two", "user_role": "teacher"})
        self.as_user("teach2")

        resp = self.client.delete("/api/classes/c1", headers=self.hdr())

        self.assertEqual(resp.status_code, 403)
        self.assertIn(aid, self._ids("assignments", "assignment_id"))
        self.assertIn(aid, self._ids("assignment_progress", "assignment_id"))


class TestTeacherListingIsBatched(AssignmentDataTestBase):
    """The teacher listing must aggregate counts in a fixed number of queries.

    Each Supabase call carries a (5, 15) connect/read timeout, so a per-assignment
    loop turns a page render into ~100 serial round trips. This is a pure
    performance change, so the counts are asserted alongside the query budget.
    """

    def setUp(self):
        super().setUp()
        self.db.tables["classes"].append(
            {"class_id": "c2", "class_name": "French 101", "teacher_id": "teach1", "is_active": True})
        self.db.tables["user_profiles"].append(
            {"user_id": "stud3", "user_name": "Student Three", "user_role": "student"})
        self.db.tables["student_classes"].append(
            {"student_class_id": "sc3", "class_id": "c2", "student_id": "stud3"})

    def _create(self, **body):
        payload = {"url": "https://youtu.be/jNQXAC9I4Ss", "title": "Zoo"}
        payload.update(body)
        resp = self.client.post("/api/assignments", json=payload, headers=self.hdr())
        return resp.get_json()["assignment"]["assignment_id"]

    def _list_counting_queries(self, path="/api/assignments"):
        """Return (response json, number of _sb_get calls the listing made)."""
        seen = []
        real_get = app._sb_get

        def spy(table, params=None):
            seen.append(table)
            return real_get(table, params)

        app._sb_get = spy
        try:
            resp = self.client.get(path, headers=self.hdr())
        finally:
            app._sb_get = real_get
        return resp.get_json(), len(seen)

    def test_query_count_does_not_grow_with_the_number_of_assignments(self):
        for _ in range(3):
            self._create(class_ids=["c1"])
        _, few = self._list_counting_queries()

        for _ in range(12):
            self._create(class_ids=["c1"])
        body, many = self._list_counting_queries()

        self.assertEqual(len(body["assignments"]), 15)
        self.assertEqual(few, many,
                         f"query count scales with assignment count ({few} -> {many}); "
                         "the listing is still doing per-assignment reads")

    def test_counts_are_correct_for_mixed_targets(self):
        whole_class = self._create(class_ids=["c1"])                    # stud1, stud2
        both_classes = self._create(class_ids=["c1", "c2"])             # stud1, stud2, stud3
        individual = self._create(
            student_targets=[{"class_id": "c1", "student_id": "stud1"}])  # stud1
        # stud1 started both class-wide assignments and finished the first.
        self.db.tables["assignment_progress"].extend([
            {"assignment_id": whole_class, "student_id": "stud1", "completed": True,
             "current_line_index": 10, "max_line_reached": 10, "total_lines": 10},
            {"assignment_id": both_classes, "student_id": "stud1", "completed": False,
             "current_line_index": 3, "max_line_reached": 3, "total_lines": 10},
        ])

        body, _ = self._list_counting_queries()

        by_id = {a["assignment_id"]: a for a in body["assignments"]}
        self.assertEqual(
            (by_id[whole_class]["assigned_count"], by_id[whole_class]["started_count"],
             by_id[whole_class]["completed_count"]), (2, 1, 1))
        self.assertEqual(
            (by_id[both_classes]["assigned_count"], by_id[both_classes]["started_count"],
             by_id[both_classes]["completed_count"]), (3, 1, 0))
        self.assertEqual(
            (by_id[individual]["assigned_count"], by_id[individual]["started_count"],
             by_id[individual]["completed_count"]), (1, 0, 0))

    def test_counts_never_leak_across_assignments(self):
        a1 = self._create(class_ids=["c1"])
        a2 = self._create(class_ids=["c2"])
        self.db.tables["assignment_progress"].append(
            {"assignment_id": a1, "student_id": "stud1", "completed": True,
             "current_line_index": 5, "max_line_reached": 5, "total_lines": 5})

        body, _ = self._list_counting_queries()

        by_id = {a["assignment_id"]: a for a in body["assignments"]}
        self.assertEqual(by_id[a2]["started_count"], 0,
                         "another assignment's progress row was counted here")
        self.assertEqual(by_id[a2]["completed_count"], 0)
        self.assertEqual(by_id[a1]["started_count"], 1)

    def test_class_filtered_listing_is_also_batched_and_scoped(self):
        self._create(class_ids=["c1"])
        self._create(class_ids=["c1"])
        self._create(class_ids=["c2"])

        body, queries = self._list_counting_queries("/api/assignments?class_id=c1")

        self.assertEqual(len(body["assignments"]), 2)
        self.assertLessEqual(queries, 8, f"class-scoped listing made {queries} queries")

    def test_empty_listing_makes_no_count_queries_per_assignment(self):
        body, _ = self._list_counting_queries()
        self.assertEqual(body["assignments"], [])
        self.assertEqual(body["role"], "teacher")


if __name__ == "__main__":
    unittest.main()
