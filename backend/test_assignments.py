"""Tests for the teacher->student video assignment endpoints.

The Supabase REST layer is stubbed with an in-memory fake so we exercise the
real endpoint logic (auth, roster expansion, no-skip progress) without a DB.

Run from the backend directory:
    python test_assignments.py
or with pytest:
    python -m pytest test_assignments.py -v
"""

import unittest

import app


class FakeDB:
    """Minimal in-memory stand-in for the Supabase REST helpers."""

    def __init__(self):
        self.tables = {
            "user_profiles": [],
            "classes": [],
            "student_classes": [],
            "assignments": [],
            "assignment_targets": [],
            "assignment_progress": [],
            "videos": [],
        }
        self._id = 0

    def _next_id(self, prefix):
        self._id += 1
        return f"{prefix}{self._id}"

    @staticmethod
    def _match(row, params):
        for k, v in (params or {}).items():
            if k in ("select", "order", "limit", "on_conflict"):
                continue
            if not isinstance(v, str):
                continue
            if v.startswith("eq."):
                target = v[3:]
                actual = row.get(k)
                # Normalize booleans so eq.true matches Python True.
                if isinstance(actual, bool):
                    if str(actual).lower() != target.lower():
                        return False
                elif str(actual) != target:
                    return False
            elif v.startswith("in."):
                inside = v[v.index("(") + 1:v.rindex(")")]
                allowed = set(inside.split(",")) if inside else set()
                if str(row.get(k)) not in allowed:
                    return False
        return True

    def get(self, table, params=None):
        return [dict(r) for r in self.tables[table] if self._match(r, params)]

    def post(self, table, data, extra_headers=None, params=None):
        rows = data if isinstance(data, list) else [data]
        out = []
        for r in rows:
            r = dict(r)
            # Assign ids for primary keys the schema would default.
            if table == "assignments" and "assignment_id" not in r:
                r["assignment_id"] = self._next_id("asg")
            if table == "assignment_targets" and "target_id" not in r:
                r["target_id"] = self._next_id("tgt")
            if table == "videos" and "id" not in r:
                r["id"] = self._next_id("vid")
            # merge-duplicates for assignment_progress on (assignment,student)
            if table == "assignment_progress":
                existing = next((x for x in self.tables[table]
                                 if x["assignment_id"] == r["assignment_id"] and x["student_id"] == r["student_id"]), None)
                if existing:
                    existing.update(r)
                    out.append(dict(existing))
                    continue
            self.tables[table].append(r)
            out.append(dict(r))
        return out

    def patch(self, table, data, params=None):
        updated = []
        for row in self.tables[table]:
            if self._match(row, params):
                row.update(data)
                updated.append(dict(row))
        return updated

    def delete(self, table, params=None):
        keep = [r for r in self.tables[table] if not self._match(r, params)]
        removed = [r for r in self.tables[table] if self._match(r, params)]
        self.tables[table] = keep
        return removed


class AssignmentTestBase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        app.supabase_ready = True
        # Patch the sb helpers + video resolver.
        self._orig = (app._sb_get, app._sb_post, app._sb_patch, app._sb_delete, app._ensure_video, app._verify_token)
        app._sb_get = self.db.get
        app._sb_post = self.db.post
        app._sb_patch = self.db.patch
        app._sb_delete = self.db.delete
        app._ensure_video = lambda youtube_id, title=None, thumbnail_url=None: self.db.post(
            "videos", {"youtube_id": youtube_id, "title": title})[0]["id"]
        self.current_user = "teach1"
        app._verify_token = lambda tok: {"sub": self.current_user}

        # Seed a teacher, two students, a class, enrollments.
        self.db.tables["user_profiles"] = [
            {"user_id": "teach1", "user_name": "Teacher One", "user_role": "teacher"},
            {"user_id": "stud1", "user_name": "Student One", "user_role": "student"},
            {"user_id": "stud2", "user_name": "Student Two", "user_role": "student"},
        ]
        self.db.tables["classes"] = [
            {"class_id": "c1", "class_name": "Spanish 101", "teacher_id": "teach1", "is_active": True},
        ]
        self.db.tables["student_classes"] = [
            {"student_class_id": "sc1", "class_id": "c1", "student_id": "stud1"},
            {"student_class_id": "sc2", "class_id": "c1", "student_id": "stud2"},
        ]
        self.client = app.app.test_client()

    def tearDown(self):
        (app._sb_get, app._sb_post, app._sb_patch, app._sb_delete, app._ensure_video, app._verify_token) = self._orig

    def as_user(self, uid):
        self.current_user = uid

    def hdr(self):
        return {"Authorization": "Bearer x"}


class TestCreateAssignment(AssignmentTestBase):
    def test_teacher_creates_whole_class_assignment(self):
        r = self.client.post("/api/assignments", json={
            "url": "https://youtu.be/jNQXAC9I4Ss",
            "title": "Zoo", "class_ids": ["c1"],
        }, headers=self.hdr())
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(self.db.tables["assignments"]), 1)
        self.assertEqual(len(self.db.tables["assignment_targets"]), 1)

    def test_student_cannot_create(self):
        self.as_user("stud1")
        r = self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        self.assertEqual(r.status_code, 403)

    def test_cannot_assign_to_other_teachers_class(self):
        self.db.tables["classes"].append({"class_id": "c2", "class_name": "Other", "teacher_id": "teachX", "is_active": True})
        r = self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c2"]}, headers=self.hdr())
        self.assertEqual(r.status_code, 403)

    def test_requires_a_target(self):
        r = self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss"}, headers=self.hdr())
        self.assertEqual(r.status_code, 400)

    def test_bad_url_rejected(self):
        r = self.client.post("/api/assignments", json={"url": "not a url", "class_ids": ["c1"]}, headers=self.hdr())
        self.assertEqual(r.status_code, 400)


class TestRosterExpansion(AssignmentTestBase):
    def test_whole_class_expands_to_all_students(self):
        self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        aid = self.db.tables["assignments"][0]["assignment_id"]
        ids = app._assignment_student_ids(aid)
        self.assertEqual(ids, {"stud1", "stud2"})

    def test_individual_target_only_that_student(self):
        self.client.post("/api/assignments", json={
            "url": "https://youtu.be/jNQXAC9I4Ss",
            "student_targets": [{"class_id": "c1", "student_id": "stud1"}],
        }, headers=self.hdr())
        aid = self.db.tables["assignments"][0]["assignment_id"]
        ids = app._assignment_student_ids(aid)
        self.assertEqual(ids, {"stud1"})


class TestStudentListing(AssignmentTestBase):
    def test_student_sees_class_assignment(self):
        self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        self.as_user("stud1")
        r = self.client.get("/api/assignments", headers=self.hdr())
        body = r.get_json()
        self.assertEqual(body["role"], "student")
        self.assertEqual(len(body["assignments"]), 1)

    def test_non_targeted_student_sees_nothing(self):
        # assign only to stud1 individually
        self.client.post("/api/assignments", json={
            "url": "https://youtu.be/jNQXAC9I4Ss",
            "student_targets": [{"class_id": "c1", "student_id": "stud1"}],
        }, headers=self.hdr())
        self.as_user("stud2")
        r = self.client.get("/api/assignments", headers=self.hdr())
        self.assertEqual(len(r.get_json()["assignments"]), 0)


class TestNoSkipProgress(AssignmentTestBase):
    def _make(self):
        self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        return self.db.tables["assignments"][0]["assignment_id"]

    def test_linear_progress_and_skip_clamped(self):
        aid = self._make()
        self.as_user("stud1")
        r = self.client.post(f"/api/assignments/{aid}/progress", json={"current_line_index": 1, "total_lines": 10}, headers=self.hdr())
        self.assertEqual(r.get_json()["progress"]["max_line_reached"], 1)
        r = self.client.post(f"/api/assignments/{aid}/progress", json={"current_line_index": 2, "total_lines": 10}, headers=self.hdr())
        self.assertEqual(r.get_json()["progress"]["max_line_reached"], 2)
        # Skip attempt to line 9 -> clamped to max+1 = 3
        r = self.client.post(f"/api/assignments/{aid}/progress", json={"current_line_index": 9, "total_lines": 10}, headers=self.hdr())
        pj = r.get_json()["progress"]
        self.assertEqual(pj["max_line_reached"], 3)
        self.assertEqual(pj["current_line_index"], 3)
        self.assertFalse(pj["completed"])

    def test_completion_when_reaching_last_line(self):
        aid = self._make()
        self.as_user("stud1")
        # walk to the end (total 3)
        for line in range(1, 4):
            r = self.client.post(f"/api/assignments/{aid}/progress", json={"current_line_index": line, "total_lines": 3}, headers=self.hdr())
        pj = r.get_json()["progress"]
        self.assertTrue(pj["completed"])

    def test_non_target_cannot_post_progress(self):
        aid = self._make()
        # a student not in the class
        self.db.tables["user_profiles"].append({"user_id": "outsider", "user_name": "X", "user_role": "student"})
        self.as_user("outsider")
        r = self.client.post(f"/api/assignments/{aid}/progress", json={"current_line_index": 1, "total_lines": 10}, headers=self.hdr())
        self.assertEqual(r.status_code, 403)


class TestDeleteAssignment(AssignmentTestBase):
    def test_owner_deletes(self):
        self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        aid = self.db.tables["assignments"][0]["assignment_id"]
        r = self.client.delete(f"/api/assignments/{aid}", headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.db.tables["assignments"]), 0)
        self.assertEqual(len(self.db.tables["assignment_targets"]), 0)

    def test_non_owner_cannot_delete(self):
        self.client.post("/api/assignments", json={"url": "https://youtu.be/jNQXAC9I4Ss", "class_ids": ["c1"]}, headers=self.hdr())
        aid = self.db.tables["assignments"][0]["assignment_id"]
        self.as_user("stud1")
        r = self.client.delete(f"/api/assignments/{aid}", headers=self.hdr())
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
