from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app import ingreso as logic, ingreso_jobs as jobs, ingreso_routes as routes
from app.config import Settings
from app.db import Base, get_db
from app.models import (IngresoCampaign, IngresoStudent, IngresoJob, IngresoTask, IngresoAccount,
                        IngresoPlacement, IngresoLease, LocalUser)
from app.services.graph import ServiceResult
from app.services.moodle import MoodleResult
from app.services.mailer import MailResult


def inscription(career="3", modality="1", commission="COMISION 3"):
    return {"career_id": career, "career": "Carrera de prueba", "date": "2026-01-10", "modality": modality,
            "commissions": [commission] if commission else [], "states": ["preinscriptos"]}


def api_person(dni="99888777", career=3, modality="1", commission="COMISION 3"):
    return {"nrodoc": dni, "carreras": [{"id_carrera": career, "nombre_carrera": "Carrera prueba",
        "fecha_inscri": "10/01/2026 00:00:00", "modalidad": modality,
        "comisiones": [{"id_comision": 999, "nombre_comision": commission}] if commission else []}]}


class IngresoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(_env_file=None, crm_domain="example.invalid", crm_secret_key="test-only-secret-key-at-least-32-bytes",
            crm_database_url="sqlite://", crm_dry_run=False, ingreso_allow_writes=False, ingreso_step_pause=0,
            sial_base_url="https://sial.example.invalid/api", sial_username="fake", sial_password="fake",
            azure_tenant_id="fake", azure_client_id="fake", azure_client_secret="fake", azure_student_sku_id="fake-sku",
            moodle_base_url="https://moodle.example.invalid", moodle_token="fake", moodle_create_missing_users=True,
            smtp_host="smtp.example.invalid", smtp_username="fake", smtp_password="fake", smtp_from_email="crm@example.invalid")
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "db.sqlite3"), connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        self.rules = logic.default_rules()
        self.rules["accounting"]["P"]["COMISION 3"] = "I1"
        self.rules["accounting"]["D"]["COMISION 02"] = "I2"
        with self.session() as db:
            self.campaign = IngresoCampaign(name="Test", start_date="2025-12-01", end_date="2026-03-31", rules_json=logic.dump(self.rules))
            db.add(self.campaign)
            db.flush()
            self.student = IngresoStudent(campaign_id=self.campaign.id, dni="99888777", upn="a99888777@example.invalid",
                first_name="Alumno", last_name="Prueba", email="personal@example.invalid", phone="123", personal_status="ok",
                registrations_json=logic.dump([inscription()]))
            db.add(self.student)
            db.add(LocalUser(username="test", password_hash="not-used", active=True))
            db.commit()
        self.graph = MagicMock()
        self.graph.get_user.return_value = ServiceResult("missing")
        self.graph.create_user.return_value = ServiceResult("created", data={"id": "ms-test"})
        self.graph.assign_license.return_value = ServiceResult("licensed")
        self.mailer = MagicMock(enabled=True)
        self.mailer.send_credentials.return_value = MailResult("sent")
        self.moodle = MagicMock()
        self.moodle.settings = self.settings
        self.moodle.get_user_by_username.return_value = MoodleResult("missing")
        self.moodle.get_course_by_shortname.side_effect = lambda c: MoodleResult("ok", data={"id": 10+int(c[1:]), "shortname": c, "fullname": "Course " + c})
        self.moodle.get_course_groups.return_value = MoodleResult("ok", data=[])
        self.moodle.create_user.return_value = MoodleResult("created", data={"id": 7})
        self.moodle.enrol_user.return_value = MoodleResult("enrolled")
        self.moodle.create_group.side_effect = lambda cid, name: MoodleResult("created", data={"id": cid+100, "name": name})
        self.moodle.add_group_member.return_value = MoodleResult("added")
        self.moodle.delete_group_member.return_value = MoodleResult("removed")
        for target, value in (("app.ingreso_jobs.get_settings", self.settings), ("app.ingreso_routes.get_settings", self.settings),
                              ("app.ingreso_jobs.IngresoGraphClient", self.graph), ("app.ingreso_jobs.MoodleClient", self.moodle),
                              ("app.ingreso_jobs.Mailer", self.mailer)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, kind, with_task=False):
        with self.session() as db:
            job = IngresoJob(campaign_id=self.campaign.id, kind=kind, actor="test", environment=jobs.environment(self.settings), status="preview")
            db.add(job)
            db.flush()
            if with_task:
                task = IngresoTask(job_id=job.id, student_id=self.student.id, status="ready", selected=True,
                    fingerprint=logic.fingerprint(self.student, self.campaign, kind),
                    snapshot_json=logic.dump(logic.student_snapshot(self.student, self.campaign, kind)))
                db.add(task)
            db.commit()
            return job.id

    def run_job(self, job_id, execute=False):
        with self.session() as db:
            logic.acquire(db, "test-owner")
            db.commit()
        jobs.run_job(job_id, "test-owner", execute=execute, session_factory=self.session, settings=self.settings)

    def client(self):
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test-secret")
        @app.get("/test-session")
        def login(request: Request):
            request.session["username"] = "test"
        def require_user(request):
            if not request.session.get("username"):
                raise HTTPException(403)
            return request.session["username"]
        templates = Jinja2Templates(directory="app/templates")
        templates.env.filters["local_datetime"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else ""
        app.include_router(routes.create_ingreso_router(templates, require_user))
        def local_db():
            with self.session() as db:
                yield db
        app.dependency_overrides[get_db] = local_db
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_degree_routing_preserves_leading_zero_and_splits_accounting(self):
        p, issues = logic.desired_courses([inscription()], self.rules)
        self.assertFalse(issues)
        self.assertEqual({t["course"] for t in p}, {"I1", "I3", "I4", "I5", "I6"})
        d, issues = logic.desired_courses([inscription(modality="2", commission="COMISION 02")], self.rules)
        self.assertEqual({(t["course"], t["group"]) for t in d}, {(c,"D") for c in ("I2","I3","I4","I5","I6")})
        self.assertNotEqual(logic.normalized("Comisión 01"), logic.normalized("Comision 1"))

    def test_technical_degrees_are_exclusive_and_multiple_careers_combine(self):
        for career, course in (("7","I7"), ("8","I8")):
            p, issues = logic.desired_courses([inscription(career, commission="")], self.rules)
            self.assertFalse(issues)
            self.assertEqual(p, [{"course":course,"group":"P"}])
        registrations = [inscription(), inscription(), inscription("8", commission="COMISION TGE")]
        p, issues = logic.desired_courses(registrations, self.rules)
        self.assertEqual(len(p), 6)
        self.assertFalse(issues)

    def test_missing_conflicting_or_unassigned_commission_never_guesses_accounting(self):
        for registration, rules in ((inscription(commission=""), self.rules), (inscription(), logic.default_rules()),
                                    (inscription(modality="2", commission="COMISION 3"), self.rules)):
            targets, issues = logic.desired_courses([registration], rules)
            self.assertTrue(issues)
            self.assertFalse({t["course"] for t in targets} & {"I1","I2"})
            self.assertEqual(len(targets), 4)
        rules = json.loads(logic.dump(self.rules))
        rules["accounting"]["P"]["COMISION 1"] = "I2"
        r = inscription()
        r["commissions"].append("COMISION 1")
        targets, issues = logic.desired_courses([r], rules)
        self.assertTrue(issues)
        self.assertEqual(len(targets), 4)

    def test_multiple_degree_careers_never_assign_both_accounting_courses(self):
        self.rules["accounting"]["P"]["COMISION 1"] = "I2"
        targets, issues = logic.desired_courses([inscription("3"), inscription("4", commission="COMISION 1")], self.rules)
        self.assertTrue(any("I1 e I2" in issue for issue in issues))
        self.assertEqual({t["course"] for t in targets}, {"I3", "I4", "I5", "I6"})
        self.rules["accounting"]["P"]["COMISION 1"] = "I1"
        targets, issues = logic.desired_courses([inscription("3"), inscription("4", commission="COMISION 1")], self.rules)
        self.assertFalse(issues)
        self.assertEqual(len(targets), 5)

    def test_existing_other_accounting_course_does_not_receive_second_enrollment(self):
        self.settings.ingreso_allow_writes = True
        self.moodle.get_user_by_username.return_value = MoodleResult("ok", data={"id":7,"username":self.student.upn})
        self.moodle.get_user_courses.return_value = MoodleResult("ok", data=[{"id":12,"shortname":"I2"}])
        preview = self.job("moodle")
        self.run_job(preview)
        with self.session() as db:
            task = db.query(IngresoTask).filter_by(job_id=preview).one()
            plan = json.loads(task.snapshot_json)["plan"]
            self.assertEqual({t["course"] for t in plan["targets"]}, {"I3", "I4", "I5", "I6"})
            self.assertTrue(any("matricula actual en I2" in issue for issue in plan["routing_issues"]))
        execution = self.job("moodle", True)
        self.run_job(execution, execute=True)
        self.assertEqual({call.args[1] for call in self.moodle.enrol_user.call_args_list}, {13,14,15,16})
        with self.session() as db:
            task = db.query(IngresoTask).filter_by(job_id=execution).one()
            self.assertEqual(task.status, "partial")
            self.assertIn("matricula actual en I2", task.message)
        self.mailer.send_credentials.assert_not_called()

    def test_sial_three_conditions_union_and_daily_transition_do_not_duplicate(self):
        data = {s: [] for s in logic.SIAL_STATES}
        data["preinscriptos"] = [api_person()]
        data["aspirantes"] = [api_person()]
        people = logic.normalize_sial(data, self.campaign)
        self.assertEqual(len(people), 1)
        self.assertEqual(len(people["99888777"]), 1)
        with self.session() as db:
            logic.persist_sial(db, self.campaign, people, self.settings.crm_domain)
            data["preinscriptos"] = []
            people = logic.normalize_sial(data, self.campaign)
            logic.persist_sial(db, self.campaign, people, self.settings.crm_domain)
            self.assertEqual(db.query(IngresoStudent).count(), 1)
            self.assertEqual(json.loads(db.get(IngresoStudent,self.student.id).registrations_json)[0]["states"], ["aspirantes"])
            logic.persist_sial(db, self.campaign, {}, self.settings.crm_domain)
            self.assertEqual(db.query(IngresoStudent).count(), 1)
            self.assertEqual(db.get(IngresoStudent,self.student.id).source_status,"not_seen")

    def test_campaign_boundaries_and_invalid_snapshot_do_not_erase_people(self):
        data = {s: [] for s in logic.SIAL_STATES}
        p = api_person()
        p["carreras"][0]["fecha_inscri"] = "01/12/2026"
        data["preinscriptos"] = [p]
        self.assertEqual(logic.normalize_sial(data,self.campaign), {})
        data.pop("ingresantes")
        with self.assertRaises(ValueError):
            logic.normalize_sial(data,self.campaign)
        with self.session() as db:
            self.assertEqual(db.get(IngresoStudent,self.student.id).source_status,"current")

    def test_sync_job_reads_sial_only_and_persists_personal_details(self):
        data = {s: [] for s in logic.SIAL_STATES}
        data["aspirantes"] = [api_person()]
        response = MagicMock(status_code=200)
        response.json.return_value = [{"nrodoc":"99888777","nombre":"Actualizado","apellido":"Prueba","email":"nuevo@example.invalid","telefono":"111"}]
        with patch.object(jobs,"sial_lists",return_value=data), patch.object(jobs,"retry_get",return_value=response):
            self.run_job(self.job("sync"))
        with self.session() as db:
            self.assertEqual(db.get(IngresoStudent,self.student.id).first_name,"Actualizado")
            self.assertEqual(db.query(IngresoJob).one().status,"completed")
        self.graph.get_user.assert_not_called()
        self.moodle.get_user_by_username.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_failed_sial_list_keeps_previous_snapshot(self):
        with patch.object(jobs,"sial_lists",side_effect=RuntimeError("SIAL HTTP 503")):
            self.run_job(self.job("sync"))
        with self.session() as db:
            self.assertEqual(db.query(IngresoJob).one().status,"error")
            self.assertEqual(db.get(IngresoStudent,self.student.id).source_status,"current")

    def test_both_previews_read_only_even_when_global_crm_is_production(self):
        for kind in ("microsoft","moodle"):
            self.run_job(self.job(kind))
        self.graph.create_user.assert_not_called()
        self.graph.assign_license.assert_not_called()
        self.moodle.create_user.assert_not_called()
        self.moodle.enrol_user.assert_not_called()
        self.moodle.create_group.assert_not_called()
        self.mailer.send_credentials.assert_not_called()
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).filter_by(status="ready").count(),2)
            self.assertEqual(db.query(IngresoAccount).count(),0)

    def test_unknown_microsoft_is_blocked_not_treated_as_missing(self):
        self.graph.get_user.return_value = ServiceResult("error")
        self.run_job(self.job("microsoft"))
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).one().status,"blocked")
        self.graph.create_user.assert_not_called()

    def test_worker_safety_lock_blocks_all_writes(self):
        for kind in ("microsoft","moodle"):
            self.run_job(self.job(kind,True), execute=True)
        self.graph.get_user.assert_not_called()
        self.graph.create_user.assert_not_called()
        self.moodle.create_user.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_existing_microsoft_is_untouched_and_never_emailed(self):
        self.settings.ingreso_allow_writes = True
        self.graph.get_user.return_value = ServiceResult("exists", data={"id":"old-ms","userPrincipalName":self.student.upn})
        self.run_job(self.job("microsoft",True), execute=True)
        self.graph.create_user.assert_not_called()
        self.graph.assign_license.assert_not_called()
        self.mailer.send_credentials.assert_not_called()
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).one().status,"done")

    def test_new_microsoft_creates_licenses_emails_only_once(self):
        self.settings.ingreso_allow_writes = True
        self.run_job(self.job("microsoft",True), execute=True)
        self.graph.create_user.assert_called_once()
        self.graph.assign_license.assert_called_once_with("ms-test","student")
        self.mailer.send_credentials.assert_called_once()
        args = self.mailer.send_credentials.call_args.args
        self.assertEqual(args[:2], (self.student.email,self.student.upn))
        self.assertEqual(args[2], self.graph.create_user.call_args.args[3])
        with self.session() as db:
            account = db.get(IngresoAccount,self.student.upn)
            self.assertEqual(account.email_status,"sent")
            self.assertEqual(account.credential_encrypted,"")
        self.graph.get_user.return_value = ServiceResult("exists",data={"id":"ms-test","userPrincipalName":self.student.upn})
        self.run_job(self.job("microsoft",True), execute=True)
        self.assertEqual(self.mailer.send_credentials.call_count,1)
        self.assertEqual(self.graph.create_user.call_count,1)

    def test_license_failure_can_resume_without_recreating_or_resetting_password(self):
        self.settings.ingreso_allow_writes = True
        self.graph.assign_license.return_value = ServiceResult("error")
        self.run_job(self.job("microsoft",True),execute=True)
        password = self.graph.create_user.call_args.args[3]
        self.mailer.send_credentials.assert_not_called()
        with self.session() as db:
            account = db.get(IngresoAccount,self.student.upn)
            self.assertNotIn(password, account.credential_encrypted)
            self.assertEqual(jobs.cipher(self.settings).decrypt(account.credential_encrypted.encode()).decode(),password)
        self.graph.assign_license.return_value = ServiceResult("licensed")
        self.graph.get_user.return_value = ServiceResult("exists",data={"id":"ms-test","userPrincipalName":self.student.upn})
        self.run_job(self.job("microsoft",True),execute=True)
        self.assertEqual(self.graph.create_user.call_count,1)
        self.mailer.send_credentials.assert_called_once()

    def test_uncertain_creation_or_mail_is_never_blindly_repeated(self):
        self.settings.ingreso_allow_writes = True
        self.graph.create_user.side_effect = RuntimeError("transport")
        self.run_job(self.job("microsoft",True),execute=True)
        self.graph.create_user.side_effect = None
        self.graph.get_user.return_value = ServiceResult("exists",data={"id":"ms-test","userPrincipalName":self.student.upn})
        self.run_job(self.job("microsoft",True),execute=True)
        self.assertEqual(self.graph.create_user.call_count,1)
        self.mailer.send_credentials.assert_not_called()

    def test_moodle_is_independent_of_graph_and_mail_and_handles_technical_degrees(self):
        self.settings.ingreso_allow_writes = True
        with self.session() as db:
            s = db.get(IngresoStudent,self.student.id)
            s.registrations_json = logic.dump([inscription("7",commission="")])
            db.commit()
            self.student = s
        self.run_job(self.job("moodle",True),execute=True)
        self.graph.get_user.assert_not_called()
        self.graph.create_user.assert_not_called()
        self.mailer.send_credentials.assert_not_called()
        self.moodle.enrol_user.assert_called_once_with(7,17,"student")
        self.moodle.add_group_member.assert_called_once_with(117,7)
        with self.session() as db:
            self.assertEqual(db.query(IngresoPlacement).one().course,"I7")

    def test_no_commission_can_apply_common_courses_without_claiming_accounting_completed(self):
        self.settings.ingreso_allow_writes = True
        with self.session() as db:
            self.campaign = db.get(IngresoCampaign,self.campaign.id)
            self.campaign.rules_json = logic.dump(logic.default_rules())
            db.commit()
        self.run_job(self.job("moodle",True),execute=True)
        self.assertEqual({c.args[1] for c in self.moodle.enrol_user.call_args_list},{13,14,15,16})
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).one().status,"partial")
        self.mailer.send_credentials.assert_not_called()

    def test_changed_source_blocks_a_stale_preview_before_remote_calls(self):
        self.settings.ingreso_allow_writes = True
        job = self.job("microsoft",True)
        with self.session() as db:
            db.get(IngresoStudent,self.student.id).source_status = "not_seen"
            db.commit()
        self.run_job(job,execute=True)
        self.graph.get_user.assert_not_called()
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).one().status,"error")

    def test_lease_and_interrupted_work_never_auto_resume_real_operations(self):
        with self.session() as db:
            logic.acquire(db,"one")
            db.commit()
        with self.session() as db:
            with self.assertRaises(ValueError):
                logic.acquire(db,"two")
            db.get(IngresoLease,1).expires_at = datetime.utcnow()-timedelta(seconds=1)
            db.commit()
        job = self.job("microsoft",True)
        with self.session() as db:
            db.get(IngresoJob,job).status = "running"
            db.commit()
            logic.recover(db)
            self.assertEqual(db.get(IngresoJob,job).status,"interrupted")
        self.graph.create_user.assert_not_called()

    def test_pages_excel_csrf_and_execution_block_no_external_side_effects(self):
        client = self.client()
        self.assertEqual(client.get("/ingreso").status_code,403)
        client.get("/test-session")
        page = client.get("/ingreso")
        self.assertEqual(page.status_code,200)
        self.assertIn("Modo preparaci",page.text)
        token = re.search(r'name="token" value="([^"]+)"',page.text)[1]
        self.assertEqual(client.post(f"/ingreso/{self.campaign.id}/prepare/sync").status_code,403)
        job = self.job("moodle",True)
        with patch.object(jobs,"run_job") as worker:
            result = client.post(f"/ingreso/jobs/{job}/execute",data={"token":token,"all_filtered":"yes","confirmation":"PROCESAR"})
            self.assertIn("modo preparacion",result.text)
            worker.assert_not_called()
        for section in ("sial","microsoft","moodle","history"):
            self.assertEqual(client.get("/ingreso?section="+section).status_code,200)
        settings_page = client.get(f"/settings/ingreso?campaign_id={self.campaign.id}")
        self.assertIn('name="D:COMISION 02"',settings_page.text)
        workbook = load_workbook(io.BytesIO(client.get(f"/ingreso/{self.campaign.id}/excel").content))
        self.assertEqual(workbook["Estudiantes"]["A2"].value,self.student.dni)
        self.graph.get_user.assert_not_called()
        self.moodle.get_user_by_username.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_existing_moodle_enrollment_and_group_are_not_recreated(self):
        self.settings.ingreso_allow_writes = True
        self.moodle.get_user_by_username.return_value = MoodleResult("ok",data={"id":7,"username":self.student.upn})
        self.moodle.get_user_courses.return_value = MoodleResult("ok",data=[{"id":c} for c in (11,13,14,15,16)])
        self.moodle.get_course_groups.side_effect = lambda cid: MoodleResult("ok",data=[{"id":cid+100,"name":"P"}])
        self.moodle.get_group_members.side_effect = lambda gid: MoodleResult("ok",data=[{"groupid":gid,"userids":[7]}])
        self.run_job(self.job("moodle",True),execute=True)
        for method in (self.moodle.create_user,self.moodle.enrol_user,self.moodle.create_group,self.moodle.add_group_member):
            method.assert_not_called()
        with self.session() as db:
            self.assertEqual(db.query(IngresoTask).one().status,"done")

    def test_group_switch_removes_only_managed_membership_not_custom_groups(self):
        self.settings.ingreso_allow_writes = True
        with self.session() as db:
            self.student = db.get(IngresoStudent,self.student.id)
            self.student.registrations_json = logic.dump([inscription("7",commission="")])
            db.add(IngresoPlacement(student_id=self.student.id,course="I7",group_name="D",moodle_user_id=7,moodle_course_id=17,moodle_group_id=217))
            db.commit()
        self.moodle.get_user_by_username.return_value = MoodleResult("ok",data={"id":7,"username":self.student.upn})
        self.moodle.get_user_courses.return_value = MoodleResult("ok",data=[{"id":17}])
        self.moodle.get_course_groups.return_value = MoodleResult("ok",data=[{"id":117,"name":"P"},{"id":217,"name":"D"},{"id":317,"name":"Custom"}])
        self.moodle.get_group_members.return_value = MoodleResult("ok",data=[{"groupid":117,"userids":[]}])
        self.run_job(self.job("moodle",True),execute=True)
        self.moodle.add_group_member.assert_called_once_with(117,7)
        self.moodle.delete_group_member.assert_called_once_with(217,7)
        self.moodle.unenrol_user.assert_not_called()

    def test_multiple_careers_same_course_different_groups_enroll_only_once(self):
        self.settings.ingreso_allow_writes = True
        with self.session() as db:
            self.student = db.get(IngresoStudent,self.student.id)
            self.student.registrations_json = logic.dump([inscription("7",commission=""),inscription("8",modality="2",commission="")])
            self.campaign = db.get(IngresoCampaign,self.campaign.id)
            rules = json.loads(self.campaign.rules_json)
            rules["careers"]["8"]["courses"] = ["I7"]
            self.campaign.rules_json = logic.dump(rules)
            db.commit()
        self.run_job(self.job("moodle",True),execute=True)
        self.moodle.enrol_user.assert_called_once_with(7,17,"student")
        self.assertEqual(self.moodle.add_group_member.call_count,2)

    def test_unknown_course_or_failed_enrollment_read_blocks_before_any_moodle_write(self):
        self.settings.ingreso_allow_writes = True
        self.moodle.get_course_by_shortname.side_effect = lambda c: MoodleResult("missing")
        self.run_job(self.job("moodle",True),execute=True)
        self.moodle.create_user.assert_not_called()
        self.moodle.enrol_user.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_uncertain_smtp_delivery_is_not_resent_on_retry(self):
        self.settings.ingreso_allow_writes = True
        self.mailer.send_credentials.return_value = MailResult("error","timeout")
        self.run_job(self.job("microsoft",True),execute=True)
        self.graph.get_user.return_value = ServiceResult("exists",data={"id":"ms-test","userPrincipalName":self.student.upn})
        self.run_job(self.job("microsoft",True),execute=True)
        self.assertEqual(self.mailer.send_credentials.call_count,1)
        self.assertEqual(self.graph.create_user.call_count,1)
        with self.session() as db:
            self.assertEqual(db.get(IngresoAccount,self.student.upn).email_status,"sending")

    def test_settings_save_and_rule_changes_invalidate_only_moodle_preview(self):
        client = self.client()
        client.get("/test-session")
        page = client.get("/settings/ingreso?campaign_id="+str(self.campaign.id))
        token = re.search(r'name="token" value="([^"]+)"',page.text)[1]
        old_ms = logic.fingerprint(self.student,self.campaign,"microsoft")
        old_moodle = logic.fingerprint(self.student,self.campaign,"moodle")
        rules = json.loads(logic.dump(self.rules))
        rules["accounting"]["P"]["COMISION 3"] = "I2"
        data = {"token":token,"campaign_id":str(self.campaign.id),"name":"Test","start_date":"2025-12-01","end_date":"2026-03-31",
                "career_id":list(rules["careers"]),"career_name":[r["name"] for r in rules["careers"].values()],
                "career_kind":[r["kind"] for r in rules["careers"].values()],"career_courses":[",".join(r["courses"]) for r in rules["careers"].values()]}
        for group,mapping in rules["accounting"].items():
            data.update({group+":"+commission:course for commission,course in mapping.items()})
        response = client.post("/settings/ingreso",data=data)
        self.assertEqual(response.status_code,200)
        with self.session() as db:
            campaign = db.get(IngresoCampaign,self.campaign.id)
            self.assertNotEqual(old_moodle,logic.fingerprint(self.student,campaign,"moodle"))
            self.assertEqual(old_ms,logic.fingerprint(self.student,campaign,"microsoft"))
        self.graph.create_user.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_contradictory_same_career_modalities_require_review(self):
        with self.session() as db:
            self.student = db.get(IngresoStudent,self.student.id)
            self.student.registrations_json = logic.dump([inscription(),inscription(modality="2",commission="COMISION 02")])
            db.commit()
            with self.assertRaisesRegex(ValueError,"contradictorias"):
                jobs.moodle_plan(db,self.student,self.rules,self.moodle,{})
        self.moodle.get_user_by_username.assert_not_called()

    def settings_form(self, client, start="2025-11-01", end="2026-04-30"):
        client.get("/test-session")
        page = client.get(f"/settings/ingreso?campaign_id={self.campaign.id}")
        token = re.search(r'name="token" value="([^"]+)"', page.text)[1]
        data = {"token": token, "campaign_id": str(self.campaign.id), "name": "Test", "start_date": start, "end_date": end,
                "career_id": list(self.rules["careers"]), "career_name": [r["name"] for r in self.rules["careers"].values()],
                "career_kind": [r["kind"] for r in self.rules["careers"].values()],
                "career_courses": [",".join(r["courses"]) for r in self.rules["careers"].values()]}
        for group, mapping in self.rules["accounting"].items():
            data.update({group+":"+commission: course for commission, course in mapping.items()})
        return data

    def test_edit_queried_campaign_dates_preserves_history_and_blocks_old_previews(self):
        self.job("sync")
        old = {kind: logic.fingerprint(self.student, self.campaign, kind) for kind in ("microsoft", "moodle")}
        for kind in old:
            self.job(kind, True)
        with self.session() as db:
            db.add(IngresoPlacement(student_id=self.student.id, course="I1", group_name="P",
                                   moodle_user_id=7, moodle_course_id=11, moodle_group_id=111))
            db.commit()
        client = self.client()
        response = client.post("/settings/ingreso", data=self.settings_form(client))
        self.assertIn("Fechas actualizadas", response.text)
        with self.session() as db:
            campaign = db.get(IngresoCampaign, self.campaign.id)
            student = db.get(IngresoStudent, self.student.id)
            self.assertEqual((campaign.start_date, campaign.end_date), ("2025-11-01", "2026-04-30"))
            self.assertEqual(student.source_status, "pending_sync")
            self.assertEqual(db.query(IngresoJob).count(), 3)
            self.assertEqual(db.query(IngresoPlacement).count(), 1)
            self.assertEqual(db.query(IngresoTask).filter_by(status="blocked", selected=False).count(), 2)
            for kind in old:
                self.assertNotEqual(old[kind], logic.fingerprint(self.student, campaign, kind))
            logic.persist_sial(db, campaign, {student.dni: [inscription()]}, self.settings.crm_domain)
            self.assertEqual(student.source_status, "current")
            self.assertEqual(db.query(IngresoTask).filter_by(status="blocked").count(), 2)
        self.graph.create_user.assert_not_called()
        self.moodle.enrol_user.assert_not_called()
        self.mailer.send_credentials.assert_not_called()

    def test_date_change_requires_new_sial_data_even_when_refresh_fails(self):
        client = self.client()
        client.post("/settings/ingreso", data=self.settings_form(client))
        with patch.object(jobs, "sial_lists", side_effect=RuntimeError("SIAL HTTP 503")):
            self.run_job(self.job("sync"))
        for kind in ("microsoft", "moodle"):
            self.run_job(self.job(kind))
        with self.session() as db:
            self.assertEqual(db.get(IngresoStudent, self.student.id).source_status, "pending_sync")
            self.assertEqual(db.query(IngresoTask).filter_by(status="blocked").count(), 2)
        self.graph.get_user.assert_not_called()
        self.moodle.get_user_by_username.assert_not_called()

    def test_invalid_dates_and_active_job_do_not_change_campaign(self):
        client = self.client()
        response = client.post("/settings/ingreso", data=self.settings_form(client, "2026-05-01", "2026-04-01"))
        self.assertIn("Revisar nombre y fechas", response.text)
        with self.session() as db:
            logic.acquire(db, "another-worker")
            db.commit()
        response = client.post("/settings/ingreso", data=self.settings_form(client))
        self.assertIn("Hay una tarea de Ingreso en proceso", response.text)
        with self.session() as db:
            self.assertEqual(db.get(IngresoCampaign, self.campaign.id).start_date, "2025-12-01")
            self.assertEqual(db.get(IngresoStudent, self.student.id).source_status, "current")


if __name__ == "__main__":
    unittest.main()
