import unittest
from unittest.mock import patch
import asyncio
import io

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db import Base
from app.main import (
    MATRICULATION_ACTIONS,
    _create_matriculation_preview,
    _parse_matriculation_verification_scope,
    _deactivate_local_enrollment_state,
    _deactivate_local_user_state,
    _process_fast_matriculation_enrolments,
    _process_matriculation_course_change,
    _process_matriculation_enroll,
    _queue_matriculation_execution,
    _recover_interrupted_matriculation_runs,
    _run_matriculation_verification,
    matriculation_course_load_export,
    matriculation_page,
)
from app.config import get_settings
from app.models import (
    CourseConversionRule,
    ManagedEnrollment,
    ManagedUser,
    MatriculationRunRow,
    MatriculationCourseAlert,
    MatriculationRun,
    MatriculationState,
)
from app.services.moodle import MoodleResult


HEADER = (
    "ANIO+CODIGO+MATERIA+PERIODO+MODALIDAD+LOCALIDAD+CATEDRA+TIPO+NRODOC+"
    "APE.ALUMNO+NOM.ALUMNO+FECHA_INSCRI+HORA_INSCRI+DIRE_RESIDE+TELEFONO+MAIL+"
)


def lincu_row(code: str, course: str, modality: str, category: str, document: str) -> str:
    return (
        f"2026+{code}+{course}+1er. Cuatrimestre+{modality}+Rio Cuarto+{category}+DNI+"
        f"{document}+Apellido+Nombre+10/03/2026+14:27+Calle 1++persona{document}@example.com+"
    )


class MatriculationSemesterLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
        self.db.add_all(
            [
                CourseConversionRule(actual_code="100", sort_order=10),
                CourseConversionRule(actual_code="101", sort_order=20),
                CourseConversionRule(actual_code="102", old_code="3", category_rule="2", sort_order=30),
                CourseConversionRule(actual_code="106", old_code="14", sort_order=40),
            ]
        )
        self.db.add_all(
            [
                self.state("11111111", "101", "101", "D"),
                self.state("22222222", "102", "102-2", "D"),
                self.state("33333333", "106", "106", "D"),
                self.state("44444444", "100", "100", "D"),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    @staticmethod
    def state(document: str, logical: str, shortname: str, group: str) -> MatriculationState:
        return MatriculationState(
            document=document,
            upn=f"a{document}@eco.unrc.edu.ar",
            first_name="Nombre",
            last_name="Apellido",
            logical_course_code=logical,
            course_shortname=shortname,
            group_name=group,
            moodle_user_id=document,
            moodle_course_id=f"9{document}",
            active=True,
        )

    def test_new_lincu_preserves_other_semesters_and_only_replaces_same_subject(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("106", "DERECHO PUBLICO", "A DISTANCIA", "0", "11111111"),
                lincu_row("3", "PRINCIPIOS DE ADMINISTRACION", "A DISTANCIA", "4", "22222222"),
                lincu_row("106", "DERECHO PUBLICO", "PRESENCIAL", "0", "33333333"),
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "44444444"),
            ]
        ).encode("utf-8")

        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        rows = {
            row.document: row
            for row in self.db.query(MatriculationRunRow)
            .filter(MatriculationRunRow.run_id == run.id)
            .all()
        }

        self.assertEqual(rows["11111111"].action, "enroll")
        self.assertEqual(rows["22222222"].action, "course_change")
        self.assertEqual(rows["22222222"].previous_course_shortname, "102-2")
        self.assertEqual(rows["22222222"].course_shortname, "102-4")
        self.assertEqual(rows["33333333"].action, "group_change")
        self.assertEqual(rows["44444444"].action, "unchanged")
        self.assertEqual(run.unenroll_rows, 0)
        self.assertEqual(run.course_change_rows, 1)

        old_course = (
            self.db.query(MatriculationState)
            .filter(
                MatriculationState.document == "11111111",
                MatriculationState.course_shortname == "101",
            )
            .one()
        )
        self.assertTrue(old_course.active)

    def test_partial_course_change_is_offered_as_a_retry(self) -> None:
        self.db.add(self.state("22222222", "102", "102-4", "D"))
        self.db.commit()
        content = "\n".join(
            [
                HEADER,
                lincu_row("3", "PRINCIPIOS DE ADMINISTRACION", "A DISTANCIA", "4", "22222222"),
            ]
        ).encode("utf-8")

        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        row = (
            self.db.query(MatriculationRunRow)
            .filter(MatriculationRunRow.run_id == run.id)
            .one()
        )

        self.assertEqual(row.action, "course_change")
        self.assertEqual(row.previous_course_shortname, "102-2")
        self.assertEqual(row.course_shortname, "102-4")

    def test_legacy_unenroll_rows_are_not_executable(self) -> None:
        self.assertNotIn("unenroll", MATRICULATION_ACTIONS)

    def test_interrupted_run_is_unlocked_for_error_retries(self) -> None:
        run = MatriculationRun(status="completed", message="Ejecucion anterior finalizada.")
        self.db.add(run)
        self.db.flush()
        self.db.add(
            MatriculationRunRow(
                run_id=run.id,
                action="course_change",
                status="running",
                document="22222222",
                course_shortname="102-4",
                previous_course_shortname="102-2",
            )
        )
        self.db.commit()

        _recover_interrupted_matriculation_runs(self.db)
        self.db.refresh(run)
        row = self.db.query(MatriculationRunRow).filter(MatriculationRunRow.run_id == run.id).one()

        self.assertEqual(run.status, "completed_with_errors")
        self.assertEqual(run.error_rows, 1)
        self.assertEqual(row.status, "error")
        self.assertIn("1 filas disponibles para reintentar", run.message)

    def test_execute_all_pending_excludes_unchanged_rows(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "44444444"),
                lincu_row("101", "METODOLOGIA", "A DISTANCIA", "0", "77777777"),
            ]
        ).encode("utf-8")

        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        rows = {
            row.document: row
            for row in self.db.query(MatriculationRunRow)
            .filter(MatriculationRunRow.run_id == run.id)
            .all()
        }
        queued_ids = _queue_matriculation_execution(run, self.db)

        self.assertEqual(rows["44444444"].action, "unchanged")
        self.assertNotIn(rows["44444444"].id, queued_ids)
        self.assertIn(rows["77777777"].id, queued_ids)

    def test_verification_scope_requires_numeric_course_codes(self) -> None:
        self.assertEqual(_parse_matriculation_verification_scope("230, 0105, 230"), ("230", "105"))
        for value in ("", "230, ABC", "103-0D"):
            with self.assertRaises(ValueError):
                _parse_matriculation_verification_scope(value)

    def test_verification_only_queries_selected_lincu_courses_and_restores_deleted_enrollment(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "44444444"),
                lincu_row("101", "METODOLOGIA", "A DISTANCIA", "0", "11111111"),
            ]
        ).encode("utf-8")
        run = _create_matriculation_preview(
            content,
            "lincu.txt",
            "ivan",
            self.db,
            verification_scope=("100",),
        )
        rows = {
            row.document: row
            for row in self.db.query(MatriculationRunRow).filter_by(run_id=run.id).all()
        }
        self.assertEqual(run.status, "verifying")
        self.assertEqual(run.verification_scope, "100")
        self.assertEqual(rows["44444444"].verification_status, "pending")
        self.assertEqual(rows["11111111"].verification_status, "")

        moodle = VerificationMoodle({"100": set()})
        _run_matriculation_verification(
            run.id,
            "ivan",
            session_factory=sessionmaker(bind=self.db.get_bind(), autoflush=False, autocommit=False),
            moodle=moodle,
        )
        self.db.expire_all()
        updated_run = self.db.get(MatriculationRun, run.id)
        updated_rows = {
            row.document: row
            for row in self.db.query(MatriculationRunRow).filter_by(run_id=run.id).all()
        }
        self.assertEqual(moodle.queried_shortnames, ["100"])
        self.assertEqual(updated_run.status, "preview")
        self.assertEqual(updated_rows["44444444"].action, "enroll")
        self.assertEqual(updated_rows["44444444"].verification_status, "missing")
        self.assertEqual(updated_rows["11111111"].action, "unchanged")
        self.assertEqual(updated_rows["11111111"].verification_status, "")

    def test_verification_rejects_scope_not_found_in_uploaded_lincu(self) -> None:
        content = "\n".join([HEADER, lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "44444444")]).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "Ninguno de los codigos"):
            _create_matriculation_preview(content, "lincu.txt", "ivan", self.db, verification_scope=("230",))

    def test_simple_enrolments_are_sent_to_moodle_in_batches(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "77777771"),
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "77777772"),
            ]
        ).encode("utf-8")
        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        rows = (
            self.db.query(MatriculationRunRow)
            .filter(MatriculationRunRow.run_id == run.id)
            .order_by(MatriculationRunRow.id)
            .all()
        )
        moodle = FastScenarioMoodle()

        fallback = _process_fast_matriculation_enrolments(self.db, moodle, run, rows, {})
        self.db.commit()

        self.assertEqual(fallback, [])
        self.assertTrue(all(row.status == "done" for row in rows))
        self.assertEqual(run.execution_processed_rows, 2)
        self.assertEqual(moodle.operations, ["lookup:2", "bulk_enrol:2", "bulk_group:2"])

    def test_preview_persists_course_load_report_for_current_file(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "88888888"),
                lincu_row("101", "METODOLOGIA", "A DISTANCIA", "0", "88888888"),
            ]
        ).encode("utf-8")
        test_settings = get_settings().model_copy(
            update={"matriculation_course_alert_threshold": 2}
        )

        with patch("app.main.settings", test_settings):
            run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)

        alert = (
            self.db.query(MatriculationCourseAlert)
            .filter(MatriculationCourseAlert.run_id == run.id)
            .one()
        )
        self.assertEqual(run.course_alert_threshold, 2)
        self.assertEqual(run.course_alert_count, 1)
        self.assertEqual(alert.document, "88888888")
        self.assertEqual(alert.course_count, 2)
        self.assertIn("100 - MATEMATICA I", alert.course_names)

    def test_matriculation_page_counts_each_student_once(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("100", "MATEMATICA I", "A DISTANCIA", "0", "88888888"),
                lincu_row("101", "METODOLOGIA", "A DISTANCIA", "0", "88888888"),
                lincu_row("101", "METODOLOGIA", "A DISTANCIA", "0", "99999999"),
            ]
        ).encode("utf-8")
        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/matriculation",
                "headers": [],
                "query_string": b"",
                "session": {"username": "ivan"},
            }
        )

        response = matriculation_page(
            request,
            run_id=run.id,
            view="all",
            db=self.db,
        )

        self.assertEqual(response.context["unique_student_count"], 2)
        self.assertIn("Alumnos únicos: 2", response.body.decode("utf-8"))

    def test_matriculation_page_exposes_opt_in_scoped_moodle_verification(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/matriculation",
                "headers": [],
                "query_string": b"",
                "session": {"username": "ivan"},
            }
        )

        response = matriculation_page(request, db=self.db)
        body = response.body.decode("utf-8")

        self.assertIn('id="verify-moodle"', body)
        self.assertIn('id="verification-courses"', body)
        self.assertIn('placeholder="Ej: 230, 105"', body)
        self.assertIn('courses.disabled = !checkbox.checked', body)

    def test_course_load_export_generates_readable_excel(self) -> None:
        run = MatriculationRun(
            filename="lincu.txt",
            created_by="ivan",
            course_alert_threshold=5,
            course_alert_count=1,
        )
        self.db.add(run)
        self.db.flush()
        self.db.add(
            MatriculationCourseAlert(
                run_id=run.id,
                document="43928369",
                first_name="Alexis Rodrigo",
                last_name="Abba",
                phone="3584000000",
                email="alexis@example.com",
                modality="A DISTANCIA",
                course_count=5,
                course_codes="100, 101, 102, 106, 107",
                course_names="100 - MATEMATICA I; 101 - METODOLOGIA",
            )
        )
        self.db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/matriculation/runs/{run.id}/course-load.xlsx",
                "headers": [],
                "query_string": b"",
                "session": {"username": "ivan"},
            }
        )

        response = matriculation_course_load_export(run.id, request, self.db)

        async def collect_body() -> bytes:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        workbook = load_workbook(io.BytesIO(asyncio.run(collect_body())))
        sheet = workbook["Carga academica"]
        self.assertEqual(sheet["A1"].value, "DNI")
        self.assertEqual(sheet["A2"].value, "43928369")
        self.assertEqual(sheet["G2"].value, 5)
        self.assertEqual(sheet["H2"].value, "100 - MATEMATICA I; 101 - METODOLOGIA")

    def test_local_cleanup_deactivates_enrollment_and_matriculation_state(self) -> None:
        user = ManagedUser(
            person_type="student",
            document="55555555",
            upn="a55555555@eco.unrc.edu.ar",
            active=True,
        )
        self.db.add(user)
        self.db.flush()
        enrollment = ManagedEnrollment(
            user_id=user.id,
            course_shortname="104",
            role="student",
            active=True,
        )
        state = self.state("55555555", "104", "104", "D")
        self.db.add_all([enrollment, state])
        self.db.commit()

        _deactivate_local_enrollment_state(self.db, user, enrollment)
        self.db.commit()
        self.assertFalse(enrollment.active)
        self.assertFalse(state.active)
        self.assertTrue(user.active)

        _deactivate_local_user_state(self.db, user)
        self.db.commit()
        self.assertFalse(user.active)

    def test_course_change_enrols_new_course_before_removing_previous_one(self) -> None:
        content = "\n".join(
            [
                HEADER,
                lincu_row("3", "PRINCIPIOS DE ADMINISTRACION", "A DISTANCIA", "4", "22222222"),
            ]
        ).encode("utf-8")
        run = _create_matriculation_preview(content, "lincu.txt", "ivan", self.db)
        row = (
            self.db.query(MatriculationRunRow)
            .filter(MatriculationRunRow.run_id == run.id)
            .one()
        )
        moodle = FakeMoodle()

        _process_matriculation_course_change(self.db, moodle, row, {})
        self.db.commit()

        self.assertEqual(row.status, "done")
        self.assertEqual(moodle.operations[0], "enrol:204")
        self.assertEqual(moodle.operations[-1], f"unenrol:{row.previous_moodle_course_id}")
        old_state = (
            self.db.query(MatriculationState)
            .filter(
                MatriculationState.document == "22222222",
                MatriculationState.course_shortname == "102-2",
            )
            .one()
        )
        new_state = (
            self.db.query(MatriculationState)
            .filter(
                MatriculationState.document == "22222222",
                MatriculationState.course_shortname == "102-4",
            )
            .one()
        )
        self.assertFalse(old_state.active)
        self.assertTrue(new_state.active)

    def test_existing_moodle_user_is_enrolled_without_being_recreated(self) -> None:
        row = self.enrollment_row("60000001")
        moodle = ScenarioMoodle(user_exists=True, already_enrolled=False, already_grouped=False)

        _process_matriculation_enroll(self.db, moodle, row, {})

        self.assertEqual(row.status, "done")
        self.assertNotIn("create_user", moodle.operations)
        self.assertIn("enrol", moodle.operations)
        self.assertIn("add_group", moodle.operations)

    def test_already_enrolled_user_is_not_enrolled_twice_and_missing_group_is_added(self) -> None:
        row = self.enrollment_row("60000002")
        moodle = ScenarioMoodle(user_exists=True, already_enrolled=True, already_grouped=False)

        _process_matriculation_enroll(self.db, moodle, row, {})

        self.assertEqual(row.status, "done")
        self.assertNotIn("create_user", moodle.operations)
        self.assertNotIn("enrol", moodle.operations)
        self.assertIn("add_group", moodle.operations)

    def test_already_enrolled_and_grouped_user_causes_no_external_write(self) -> None:
        row = self.enrollment_row("60000002")
        moodle = ScenarioMoodle(user_exists=True, already_enrolled=True, already_grouped=True)

        _process_matriculation_enroll(self.db, moodle, row, {})

        self.assertEqual(row.status, "done")
        self.assertEqual(moodle.operations, [])
        self.assertIn("Ya estaba matriculado y en el grupo", row.message)

    def test_missing_moodle_user_is_created_only_after_course_exists(self) -> None:
        row = self.enrollment_row("60000004")
        moodle = ScenarioMoodle(user_exists=False, already_enrolled=False, already_grouped=False)

        _process_matriculation_enroll(self.db, moodle, row, {})

        self.assertEqual(row.status, "done")
        self.assertEqual(moodle.operations[:2], ["create_user", "enrol"])

        missing_course_row = self.enrollment_row("60000005")
        missing_course = ScenarioMoodle(
            user_exists=False,
            already_enrolled=False,
            already_grouped=False,
            course_exists=False,
        )
        _process_matriculation_enroll(self.db, missing_course, missing_course_row, {})
        self.assertEqual(missing_course_row.status, "error")
        self.assertEqual(missing_course.operations, [])

    @staticmethod
    def enrollment_row(document: str) -> MatriculationRunRow:
        return MatriculationRunRow(
            action="enroll",
            status="preview",
            document=document,
            upn=f"a{document}@eco.unrc.edu.ar",
            first_name="Nombre",
            last_name="Apellido",
            email=f"persona{document}@example.com",
            logical_course_code="104",
            course_shortname="104",
            group_name="D",
        )


class FakeMoodle:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_course_by_shortname(self, shortname: str) -> MoodleResult:
        return MoodleResult("ok", "Curso.", {"id": 204, "shortname": shortname, "fullname": "Curso nuevo"})

    def get_enrolled_users(self, course_id: int) -> MoodleResult:
        return MoodleResult("ok", "Usuarios.", [])

    def get_user_by_username(self, username: str) -> MoodleResult:
        return MoodleResult("ok", "Usuario.", {"id": 22, "username": username, "groups": []})

    def enrol_user(self, user_id: int, course_id: int, role: str) -> MoodleResult:
        self.operations.append(f"enrol:{course_id}")
        return MoodleResult("enrolled", "Matriculado.")

    def get_group_in_course_by_name(self, course_id: int, group_name: str) -> MoodleResult:
        return MoodleResult("ok", "Grupo.", {"id": course_id + 1, "name": group_name})

    def add_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        self.operations.append(f"group:{group_id}")
        return MoodleResult("added", "Agregado.")

    def delete_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        self.operations.append(f"ungroup:{group_id}")
        return MoodleResult("removed", "Quitado.")

    def unenrol_user(self, user_id: int, course_id: int, role: str) -> MoodleResult:
        self.operations.append(f"unenrol:{course_id}")
        return MoodleResult("unenrolled", "Desmatriculado.")


class ScenarioMoodle:
    def __init__(
        self,
        *,
        user_exists: bool,
        already_enrolled: bool,
        already_grouped: bool,
        course_exists: bool = True,
    ) -> None:
        self.user_exists = user_exists
        self.already_enrolled = already_enrolled
        self.already_grouped = already_grouped
        self.course_exists = course_exists
        self.operations: list[str] = []

    def user_data(self, username: str) -> dict:
        groups = [{"id": 105, "name": "D"}] if self.already_grouped else []
        return {"id": 42, "username": username, "groups": groups}

    def get_course_by_shortname(self, shortname: str) -> MoodleResult:
        if not self.course_exists:
            return MoodleResult("missing", "Curso inexistente.")
        return MoodleResult("ok", "Curso.", {"id": 104, "shortname": shortname, "fullname": "Matematica II"})

    def get_enrolled_users(self, course_id: int) -> MoodleResult:
        if not self.already_enrolled:
            return MoodleResult("ok", "Participantes.", [])
        return MoodleResult("ok", "Participantes.", [self.user_data("a60000002@eco.unrc.edu.ar")])

    def get_user_by_username(self, username: str) -> MoodleResult:
        if not self.user_exists:
            return MoodleResult("missing", "Usuario inexistente.")
        return MoodleResult("ok", "Usuario.", self.user_data(username))

    def create_user(self, upn: str, first_name: str, last_name: str, email: str) -> MoodleResult:
        self.operations.append("create_user")
        return MoodleResult("created", "Creado.", self.user_data(upn))

    def enrol_user(self, user_id: int, course_id: int, role: str) -> MoodleResult:
        self.operations.append("enrol")
        return MoodleResult("enrolled", "Matriculado.")

    def get_group_in_course_by_name(self, course_id: int, group_name: str) -> MoodleResult:
        return MoodleResult("ok", "Grupo.", {"id": 105, "name": group_name})

    def create_group(self, course_id: int, group_name: str) -> MoodleResult:
        self.operations.append("create_group")
        return MoodleResult("created", "Grupo creado.", {"id": 105, "name": group_name})

    def add_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        self.operations.append("add_group")
        return MoodleResult("added", "Agregado.")


class FastScenarioMoodle(ScenarioMoodle):
    def __init__(self) -> None:
        super().__init__(user_exists=True, already_enrolled=False, already_grouped=False)

    def get_users_by_usernames(self, usernames: list[str]) -> MoodleResult:
        self.operations.append(f"lookup:{len(usernames)}")
        return MoodleResult("ok", "Usuarios.", [self.user_data(username) for username in usernames])

    def enrol_users(self, enrolments: list[tuple[int, int, str]]) -> MoodleResult:
        self.operations.append(f"bulk_enrol:{len(enrolments)}")
        return MoodleResult("enrolled", "Matriculados.")

    def add_group_members(self, memberships: list[tuple[int, int]]) -> MoodleResult:
        self.operations.append(f"bulk_group:{len(memberships)}")
        return MoodleResult("added", "Agregados.")


class VerificationMoodle:
    def __init__(self, users_by_shortname: dict[str, set[str]]) -> None:
        self.users_by_shortname = users_by_shortname
        self.queried_shortnames: list[str] = []

    def get_course_by_shortname(self, shortname: str) -> MoodleResult:
        self.queried_shortnames.append(shortname)
        if shortname not in self.users_by_shortname:
            return MoodleResult("missing", "Curso inexistente.")
        return MoodleResult("ok", "Curso.", {"id": len(self.queried_shortnames), "shortname": shortname})

    def get_enrolled_users(self, course_id: int) -> MoodleResult:
        shortname = self.queried_shortnames[course_id - 1]
        return MoodleResult(
            "ok",
            "Participantes.",
            [{"id": index + 1, "username": username} for index, username in enumerate(self.users_by_shortname[shortname])],
        )


if __name__ == "__main__":
    unittest.main()
