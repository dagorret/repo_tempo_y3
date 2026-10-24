import unittest

from app.matriculation import CourseRule, build_course_load_alerts


class MatriculationCourseLoadTest(unittest.TestCase):
    def test_counts_unique_logical_courses_and_includes_threshold(self) -> None:
        content = (
            "CODIGO+MATERIA+MODALIDAD+CATEDRA+NRODOC+APE.ALUMNO+NOM.ALUMNO+TELEFONO+MAIL+\n"
            "100+MATEMATICA I+A DISTANCIA+0+43928369+Abba+Alexis Rodrigo+3584000000+alexis@example.com+\n"
            "100+MATEMATICA I+A DISTANCIA+0+43928369+Abba+Alexis Rodrigo+3584000000+alexis@example.com+\n"
            "101+METODOLOGIA+A DISTANCIA+0+43928369+Abba+Alexis Rodrigo+3584000000+alexis@example.com+\n"
            "100+MATEMATICA I+PRESENCIAL+0+40906351+Perez+Ana++ana@example.com+\n"
        ).encode("utf-8")

        alerts = build_course_load_alerts(
            content,
            threshold=2,
            rules=[CourseRule("100"), CourseRule("101")],
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].document, "43928369")
        self.assertEqual(alerts[0].course_count, 2)
        self.assertEqual(alerts[0].phone, "3584000000")
        self.assertEqual(alerts[0].email, "alexis@example.com")
        self.assertEqual([course[0] for course in alerts[0].courses], ["100", "101"])

    def test_unknown_course_still_counts_for_academic_report(self) -> None:
        content = (
            "CODIGO+MATERIA+MODALIDAD+CATEDRA+NRODOC+APE.ALUMNO+NOM.ALUMNO+TELEFONO+MAIL+\n"
            "100+MATEMATICA I+PRESENCIAL+0+40906351+Perez+Ana++ana@example.com+\n"
            "5050+MATERIA SIN CONVERSION+PRESENCIAL+0+40906351+Perez+Ana++ana@example.com+\n"
        ).encode("utf-8")

        alerts = build_course_load_alerts(
            content,
            threshold=2,
            rules=[CourseRule("100")],
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].course_count, 2)
        self.assertEqual([course[0] for course in alerts[0].courses], ["100", "5050"])


if __name__ == "__main__":
    unittest.main()
