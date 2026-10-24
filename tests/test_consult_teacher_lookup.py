import unittest

from app.main import _teacher_lookup_key, _teacher_upn_from_query


class ConsultTeacherLookupTest(unittest.TestCase):
    domain = "eco.unrc.edu.ar"

    def test_abbreviated_teacher_key_keeps_existing_behavior(self):
        self.assertEqual(
            _teacher_upn_from_query("I Branquér", self.domain),
            "ibranquer@eco.unrc.edu.ar",
        )
        self.assertEqual(_teacher_lookup_key("I Branquér", self.domain), "ibranquer")

    def test_full_teacher_username_preserves_hyphens_and_dots(self):
        username = "romina-yppolito@eco.unrc.edu.ar"
        self.assertEqual(_teacher_upn_from_query(username, self.domain), username)
        self.assertEqual(_teacher_lookup_key(username, self.domain), username)
        self.assertEqual(
            _teacher_upn_from_query("maria.jose@eco.unrc.edu.ar", self.domain),
            "maria.jose@eco.unrc.edu.ar",
        )

    def test_rejects_non_institutional_or_malformed_full_username(self):
        self.assertEqual(_teacher_upn_from_query("romina-yppolito@gmail.com", self.domain), "")
        self.assertEqual(_teacher_upn_from_query("romina@@eco.unrc.edu.ar", self.domain), "")
        self.assertEqual(_teacher_upn_from_query("-romina@eco.unrc.edu.ar", self.domain), "")


if __name__ == "__main__":
    unittest.main()
