import unittest
from unittest.mock import patch

from app.config import get_settings
from app.services.moodle import MoodleClient, MoodleResult


class MoodleClientPayloadTest(unittest.TestCase):
    def test_created_user_uses_institutional_upn_and_oidc_without_password(self) -> None:
        client = MoodleClient(get_settings())
        captured: dict = {}

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured["function"] = function
            captured["payload"] = payload
            return MoodleResult("ok", "OK", [{"id": 42, "username": payload["users[0][username]"]}])

        client._call = fake_call
        result = client.create_user(
            "A12345678@ECO.UNRC.EDU.AR",
            "Nombre",
            "Apellido",
            "personal@example.com",
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(captured["function"], "core_user_create_users")
        self.assertEqual(captured["payload"]["users[0][username]"], "a12345678@eco.unrc.edu.ar")
        self.assertEqual(captured["payload"]["users[0][email]"], "a12345678@eco.unrc.edu.ar")
        self.assertEqual(captured["payload"]["users[0][auth]"], "oidc")
        self.assertNotIn("users[0][password]", captured["payload"])

    def test_group_creation_sends_required_description_fields(self) -> None:
        client = MoodleClient(get_settings())
        captured: dict = {}

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured["function"] = function
            captured["payload"] = payload
            return MoodleResult("ok", "OK", [{"id": 99, "name": "D"}])

        client._call = fake_call
        result = client.create_group(104, "D")

        self.assertEqual(result.status, "created")
        self.assertEqual(captured["payload"]["groups[0][description]"], "")
        self.assertEqual(captured["payload"]["groups[0][descriptionformat]"], 1)

    def test_unenrol_user_sends_only_user_and_course(self) -> None:
        client = MoodleClient(get_settings())
        captured: dict = {}

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured["function"] = function
            captured["payload"] = payload
            return MoodleResult("ok", "OK")

        client._call = fake_call
        result = client.unenrol_user(42, 314, "student")

        self.assertEqual(result.status, "unenrolled")
        self.assertEqual(captured["function"], "enrol_manual_unenrol_users")
        self.assertEqual(
            captured["payload"],
            {
                "enrolments[0][userid]": 42,
                "enrolments[0][courseid]": 314,
            },
        )

    def test_bulk_enrolment_and_group_membership_send_all_rows(self) -> None:
        client = MoodleClient(get_settings())
        captured: list[tuple[str, dict]] = []

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured.append((function, payload))
            return MoodleResult("ok", "OK")

        client._call = fake_call
        enrolment = client.enrol_users([(42, 104, "student"), (43, 104, "student")])
        group = client.add_group_members([(105, 42), (105, 43)])

        self.assertEqual(enrolment.status, "enrolled")
        self.assertEqual(group.status, "added")
        self.assertEqual(captured[0][0], "enrol_manual_enrol_users")
        self.assertEqual(captured[0][1]["enrolments[0][userid]"], 42)
        self.assertEqual(captured[0][1]["enrolments[1][userid]"], 43)
        self.assertEqual(captured[0][1]["enrolments[0][roleid]"], get_settings().moodle_student_role_id)
        self.assertEqual(captured[1][0], "core_group_add_group_members")
        self.assertEqual(captured[1][1]["members[0][groupid]"], 105)
        self.assertEqual(captured[1][1]["members[1][userid]"], 43)

    def test_bulk_user_lookup_sends_multiple_usernames(self) -> None:
        client = MoodleClient(get_settings())
        captured: dict = {}

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured["function"] = function
            captured["payload"] = payload
            return MoodleResult("ok", "OK", [])

        client._call = fake_call
        result = client.get_users_by_usernames(["A1@ECO.UNRC.EDU.AR", "a2@eco.unrc.edu.ar"])

        self.assertEqual(result.status, "ok")
        self.assertEqual(captured["function"], "core_user_get_users_by_field")
        self.assertEqual(captured["payload"]["field"], "username")
        self.assertEqual(captured["payload"]["values[0]"], "a1@eco.unrc.edu.ar")
        self.assertEqual(captured["payload"]["values[1]"], "a2@eco.unrc.edu.ar")

    def test_user_lookup_retries_once_after_a_transient_error(self) -> None:
        client = MoodleClient(get_settings())
        calls = 0

        def fake_call(function: str, payload: dict) -> MoodleResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return MoodleResult("error", "Moodle respondio HTTP 503")
            return MoodleResult("ok", "OK", [{"id": 42, "username": "a1@eco.unrc.edu.ar"}])

        client._call = fake_call
        with patch("app.services.moodle.time.sleep"):
            result = client.get_user_by_username("a1@eco.unrc.edu.ar")

        self.assertEqual(calls, 2)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["id"], 42)

    def test_service_info_audit_only_calls_read_only_endpoint(self) -> None:
        client = MoodleClient(get_settings())
        captured: dict = {}

        def fake_call(function: str, payload: dict) -> MoodleResult:
            captured["function"] = function
            captured["payload"] = payload
            return MoodleResult(
                "ok",
                "OK",
                {
                    "sitename": "Moodle FCE",
                    "functions": [
                        {"name": "core_webservice_get_site_info", "version": "2026042000"},
                        {"name": "core_user_get_users_by_field", "version": "2026042000"},
                    ],
                },
            )

        client._call = fake_call
        result = client.get_service_info()

        self.assertEqual(result.status, "ok")
        self.assertEqual(captured["function"], "core_webservice_get_site_info")
        self.assertEqual(captured["payload"], {})


if __name__ == "__main__":
    unittest.main()
