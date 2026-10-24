import unittest

from app.main import _ensure_consult_group_membership
from app.services.moodle import MoodleResult


class ConsultGroupMoodle:
    def __init__(self, *, group_exists: bool, already_member: bool) -> None:
        self.group_exists = group_exists
        self.already_member = already_member
        self.operations: list[str] = []

    def get_group_in_course_by_name(self, course_id: int, group_name: str) -> MoodleResult:
        self.operations.append("lookup_group")
        if not self.group_exists:
            return MoodleResult("missing", "Grupo inexistente.")
        return MoodleResult("ok", "Grupo encontrado.", {"id": 77, "name": group_name})

    def create_group(self, course_id: int, group_name: str) -> MoodleResult:
        self.operations.append("create_group")
        return MoodleResult("created", "Grupo creado.", {"id": 77, "name": group_name})

    def get_group_members(self, group_id: int) -> MoodleResult:
        self.operations.append("get_members")
        user_ids = [42] if self.already_member else []
        return MoodleResult("ok", "Miembros obtenidos.", [{"groupid": group_id, "userids": user_ids}])

    def add_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        self.operations.append("add_member")
        return MoodleResult("added", "Usuario agregado.")


class ConsultGroupTest(unittest.TestCase):
    def test_creates_missing_group_and_adds_user(self) -> None:
        moodle = ConsultGroupMoodle(group_exists=False, already_member=False)

        result = _ensure_consult_group_membership(moodle, 10, "Comision A", 42)

        self.assertEqual(result.status, "added")
        self.assertTrue(result.data["group_created"])
        self.assertEqual(moodle.operations, ["lookup_group", "create_group", "add_member"])

    def test_does_not_add_user_twice(self) -> None:
        moodle = ConsultGroupMoodle(group_exists=True, already_member=True)

        result = _ensure_consult_group_membership(moodle, 10, "P", 42)

        self.assertEqual(result.status, "exists")
        self.assertEqual(moodle.operations, ["lookup_group", "get_members"])

    def test_adds_user_to_existing_group(self) -> None:
        moodle = ConsultGroupMoodle(group_exists=True, already_member=False)

        result = _ensure_consult_group_membership(moodle, 10, "D", 42)

        self.assertEqual(result.status, "added")
        self.assertFalse(result.data["group_created"])
        self.assertEqual(moodle.operations, ["lookup_group", "get_members", "add_member"])


if __name__ == "__main__":
    unittest.main()
