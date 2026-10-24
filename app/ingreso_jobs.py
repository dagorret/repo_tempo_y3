"""Persistent admission jobs. Preview paths only read remote services."""
import base64
from datetime import datetime
import hashlib
import json
import re
import time

from cryptography.fernet import Fernet
import httpx

from app import ingreso as logic
from app.audit_sources import sial_lists, retry_get
from app.auditing import safe_error
from app.config import get_settings
from app.db import SessionLocal
from app.importers import ImportedRow
from app.models import (IngresoCampaign, IngresoStudent, IngresoJob, IngresoTask, IngresoAccount,
                        IngresoPlacement, OperationLog)
from app.security import generate_temporary_password
from app.services.graph import GraphClient
from app.services.mailer import Mailer
from app.services.moodle import MoodleClient
from app.user_registry import upsert_managed_user, upsert_managed_enrollment


class IngresoGraphClient(GraphClient):
    def __init__(self, settings):
        super().__init__(settings)
        self.cached_headers = None
        self.headers_at = 0

    def _headers(self):
        if self.cached_headers is None or time.monotonic() - self.headers_at > 1200:
            self.cached_headers = super()._headers()
            self.headers_at = time.monotonic()
        return self.cached_headers.copy()


def environment(settings):
    values = [logic.environment_key(settings), settings.smtp_host, settings.smtp_username,
              settings.smtp_from_email, settings.smtp_subject, settings.smtp_message_template,
              settings.azure_student_sku_id, settings.moodle_auth_method, settings.moodle_student_role_id]
    return hashlib.sha256(logic.dump(values).encode()).hexdigest()


def cipher(settings):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.crm_secret_key.encode()).digest()))


def imported(student):
    return ImportedRow(student.dni, student.first_name, student.last_name,
                       f"{student.first_name} {student.last_name}".strip(), student.email, student.upn)


def require_data(result, statuses, service):
    if result.status not in statuses or not isinstance(result.data, dict) or not result.data.get("id"):
        raise ValueError(f"{service}: no se obtuvo una identidad valida ({result.status}).")
    return result.data


def microsoft_plan(db, student, graph, settings):
    user = graph.get_user(student.upn)
    account = db.get(IngresoAccount, student.upn)
    if user.status == "exists":
        data = require_data(user, {"exists"}, "Microsoft")
        if str(data.get("userPrincipalName", "")).casefold() != student.upn.casefold():
            raise ValueError("Microsoft devolvio otra identidad; revisar.")
        if account and account.status in ("creating", "uncertain"):
            raise ValueError("Creacion anterior sin confirmacion: revisar Microsoft. No se enviaran credenciales ni se cambiara la contrasena.")
        if account and account.created_here and (account.license_status != "licensed" or account.email_status != "sent"):
            if account.remote_id != str(data["id"]):
                raise ValueError("La cuenta Microsoft cambio de ID; revisar antes de continuar.")
            if account.email_status == "sending":
                raise ValueError("Envio anterior sin confirmacion. Revisar el correo; no se reenviara automaticamente.")
            return {"action": "resume", "remote_id": str(data["id"]), "message": "Completar licencia/correo de la cuenta creada por Ingreso."}
        return {"action": "exists", "remote_id": str(data["id"]), "message": "Cuenta existente. Sin cambios de perfil, contrasena ni correo."}
    if user.status != "missing":
        raise ValueError(f"Microsoft: no se pudo comprobar si existe ({user.status}); no se creara una cuenta.")
    if account and (account.status in ("creating", "uncertain") or account.created_here):
        raise ValueError("Existe una operacion anterior para esta cuenta. Revisar antes de recrearla.")
    if student.personal_status != "ok" or not student.first_name or not student.last_name:
        raise ValueError("Faltan datos personales SIAL verificados para crear la cuenta.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", student.email):
        raise ValueError("Falta un correo personal valido para enviar las credenciales.")
    if not settings.azure_student_sku_id or not Mailer(settings).enabled:
        raise ValueError("Configurar licencia de estudiantes y SMTP antes de preparar la creacion.")
    return {"action": "create", "message": "Crear cuenta Microsoft, asignar licencia y enviar correo de bienvenida."}


def moodle_plan(db, student, rules, moodle, cache):
    targets, issues = logic.desired_courses(json.loads(student.registrations_json), rules)
    blocking = [x for x in issues if not x.startswith(("Contabilidad pendiente:", "Falta comision de Contabilidad", "Comisiones de una misma inscripcion"))]
    if blocking or not targets:
        raise ValueError(" ".join(blocking or issues or ["Sin destinos configurados."]))
    user_result = moodle.get_user_by_username(student.upn)
    if user_result.status == "missing":
        if not moodle.settings.moodle_create_missing_users:
            raise ValueError("Falta usuario Moodle y su creacion esta desactivada en Configuracion.")
        if student.personal_status != "ok" or not student.first_name or not student.last_name:
            raise ValueError("Faltan nombre/apellido SIAL verificados para crear el usuario Moodle.")
        user = None
        enrolled = set()
        current_accounting = set()
    else:
        user = require_data(user_result, {"ok"}, "Moodle")
        if str(user.get("username", "")).casefold() != student.upn.casefold():
            raise ValueError("Moodle devolvio otra identidad; revisar.")
        courses = moodle.get_user_courses(user["id"])
        if courses.status != "ok" or not isinstance(courses.data, list):
            raise ValueError("No se pudieron verificar las matriculas Moodle actuales.")
        enrolled = {int(c["id"]) for c in courses.data}
        current_accounting = {c.get("shortname") for c in courses.data} & {"I1", "I2"}
    requested_accounting = {t["course"] for t in targets} & {"I1", "I2"}
    if requested_accounting and len(current_accounting | requested_accounting) > 1:
        issues.append("Contabilidad pendiente: matricula actual en " + ", ".join(sorted(current_accounting)) +
                      "; destino previsto " + ", ".join(sorted(requested_accounting)) +
                      ". Revisar antes de cambiar; no se agregara el otro curso.")
        targets = [t for t in targets if t["course"] not in ("I1", "I2")]
    details = []
    for target in targets:
        code = target["course"]
        if code not in cache:
            course = require_data(moodle.get_course_by_shortname(code), {"ok"}, "Curso " + code)
            if course.get("shortname") != code:
                raise ValueError("El nombre corto Moodle no coincide exactamente con " + code)
            groups = moodle.get_course_groups(course["id"])
            if groups.status != "ok" or not isinstance(groups.data, list):
                raise ValueError("No se pudieron consultar grupos del curso " + code)
            cache[code] = (course, groups.data)
        course, groups = cache[code]
        matches = [g for g in groups if logic.normalized(g.get("name")) == target["group"]]
        if len(matches) > 1:
            raise ValueError("Hay varios grupos con el mismo nombre en " + code)
        group_id = int(matches[0]["id"]) if matches else None
        is_member = False
        if user and group_id:
            members_key = ("members", group_id)
            if members_key not in cache:
                members = moodle.get_group_members(group_id)
                if members.status != "ok" or not isinstance(members.data, list):
                    raise ValueError("No se pudo verificar la pertenencia al grupo de " + code)
                matching = [g for g in members.data if str(g.get("groupid")) == str(group_id)]
                if len(matching) != 1 or not isinstance(matching[0].get("userids"), list):
                    raise ValueError("Respuesta de miembros incompleta en " + code)
                cache[members_key] = {str(x) for x in matching[0]["userids"]}
            is_member = str(user["id"]) in cache[members_key]
        details.append({**target, "course_id": int(course["id"]), "course_name": course.get("fullname", code),
                        "group_id": group_id, "enrolled": int(course["id"]) in enrolled, "member": is_member})
    desired = {(t["course"], t["group"]) for t in targets}
    desired_codes = {t["course"] for t in targets}
    tracked = db.query(IngresoPlacement).filter_by(student_id=student.id).all()
    remove_groups = [{"id": p.id, "course": p.course, "group": p.group_name, "group_id": p.moodle_group_id}
                     for p in tracked if p.course in desired_codes and (p.course, p.group_name) not in desired
                     and user and p.moodle_user_id == int(user["id"])]
    retained = sorted({p.course for p in tracked if p.course not in desired_codes})
    return {"user_id": int(user["id"]) if user else None, "targets": details, "remove_groups": remove_groups,
            "retained_courses": retained, "routing_issues": issues,
            "message": (" ".join(issues) + " Destinos restantes disponibles. " if issues else "") +
                       "Sin correos. Solo se ajustan grupos P/D gestionados por Ingreso."}


def sync_job(db, job, campaign, settings, owner):
    data = sial_lists(campaign.start_date.replace("-", "") + "0000", settings,
                     lambda *a, **k: logic.heartbeat(db, owner), state_order=logic.SIAL_STATES)
    people = logic.normalize_sial(data, campaign)
    job.snapshot_json = logic.dump({"start": campaign.start_date, "end": campaign.end_date,
                                    "states_count": {k: len(v) for k, v in data.items()}, "people": people})
    logic.persist_sial(db, campaign, people, settings.crm_domain)
    students = db.query(IngresoStudent).filter_by(campaign_id=campaign.id, source_status="current").order_by(IngresoStudent.id).all()
    job.total = len(students)
    errors = 0
    with httpx.Client(timeout=60, auth=(settings.sial_username, settings.sial_password)) as client:
        for index, student in enumerate(students, 1):
            logic.heartbeat(db, owner)
            try:
                response = retry_get(client, settings.sial_base_url.rstrip("/") + "/alumnos/datospersonales/" + student.dni)
                data = response.json() if response.status_code == 200 else None
                if not isinstance(data, list) or len(data) != 1 or str(data[0].get("nrodoc", "")).strip() != student.dni:
                    raise ValueError("Datos personales SIAL sin identidad univoca.")
                for local, remote in (("first_name", "nombre"), ("last_name", "apellido"), ("email", "email"), ("phone", "telefono")):
                    value = data[0].get(remote) or ""
                    if not isinstance(value, (str, int)):
                        raise ValueError("Formato inesperado en datos personales: " + remote)
                    setattr(student, local, str(value).strip())
                student.personal_status, student.personal_message = "ok", ""
            except Exception as exc:
                errors += 1
                student.personal_status, student.personal_message = "error", safe_error(exc, settings)
            job.processed = index
            job.message = f"Datos personales: {index}/{job.total}; {errors} incidencias."
            db.commit()
    job.status = "partial" if errors else "completed"
    missing = db.query(IngresoStudent).filter_by(campaign_id=campaign.id, source_status="not_seen").count()
    job.message = f"{job.total} estudiantes vigentes; {missing} no vistos, conservados; {errors} incidencias personales."


def preview_job(db, job, campaign, settings, owner):
    students = db.query(IngresoStudent).filter_by(campaign_id=campaign.id).order_by(IngresoStudent.last_name, IngresoStudent.dni).all()
    job.total = len(students)
    client = IngresoGraphClient(settings) if job.kind == "microsoft" else MoodleClient(settings)
    cache = {}
    rules = json.loads(campaign.rules_json)
    for index, student in enumerate(students, 1):
        logic.heartbeat(db, owner)
        snapshot = logic.student_snapshot(student, campaign, job.kind)
        task = IngresoTask(job_id=job.id, student_id=student.id, fingerprint=logic.fingerprint(student, campaign, job.kind),
                           snapshot_json=logic.dump(snapshot))
        try:
            if student.source_status == "pending_sync":
                raise ValueError("Periodo modificado. Actualizar desde SIAL antes de preparar otra vista previa.")
            if student.source_status != "current":
                raise ValueError("No visto en la ultima consulta SIAL. Se conserva; requiere nueva verificacion.")
            plan = (microsoft_plan(db, student, client, settings) if job.kind == "microsoft" else
                    moodle_plan(db, student, rules, client, cache))
            snapshot["plan"] = plan
            task.status = "exists" if plan.get("action") == "exists" else "ready"
            task.message = plan["message"]
        except Exception as exc:
            task.status, task.message = "blocked", safe_error(exc, settings)
        task.snapshot_json = logic.dump(snapshot)
        db.add(task)
        job.processed = index
        job.message = f"Verificados {index}/{job.total}. Sin cambios externos."
        db.commit()
    job.status, job.message = "preview", f"Vista previa: {job.total} estudiantes. Sin cambios externos."


def execute_microsoft(db, student, graph, settings):
    logic.check_writes(settings)
    plan = microsoft_plan(db, student, graph, settings)
    account = db.get(IngresoAccount, student.upn)
    if account is None:
        account = IngresoAccount(upn=student.upn)
        db.add(account)
        db.flush()
    if plan["action"] == "exists":
        account.status, account.remote_id = "exists", plan["remote_id"]
        if not account.created_here:
            account.email_status = "not_needed"
        db.commit()
        upsert_managed_user(db, imported(student), "student", azure_user_id=account.remote_id)
        return plan["message"]
    if plan["action"] == "create":
        password = generate_temporary_password()
        account.credential_encrypted = cipher(settings).encrypt(password.encode()).decode()
        account.status, account.email_to = "creating", student.email
        db.commit()
        # Ambiguous creation is never retried blindly or followed by a password email.
        try:
            created = graph.create_user(student.upn, student.first_name, student.last_name, password)
            remote = require_data(created, {"created"}, "Creacion Microsoft")
        except Exception:
            account.status = "uncertain"
            db.commit()
            raise ValueError("Microsoft no confirmo la creacion. Revisar antes de reintentar; no se enviaron credenciales.") from None
        account.remote_id, account.status, account.created_here = str(remote["id"]), "created", True
        db.commit()
    upsert_managed_user(db, imported(student), "student", azure_user_id=account.remote_id)
    if account.license_status != "licensed":
        logic.check_writes(settings)
        result = graph.assign_license(account.remote_id, "student")
        account.license_status = result.status
        db.commit()
        if result.status != "licensed":
            raise ValueError("Cuenta creada; no se pudo confirmar la licencia. El correo queda pendiente.")
    if account.email_status != "sent":
        if account.email_status == "sending":
            raise ValueError("Envio anterior sin confirmacion; revisar antes de reenviar.")
        if account.email_to != student.email:
            raise ValueError("Cambio el correo personal desde la creacion; revisar destinatario antes de enviar credenciales.")
        password = cipher(settings).decrypt(account.credential_encrypted.encode()).decode()
        logic.check_writes(settings)
        account.email_status = "sending"
        db.commit()
        result = Mailer(settings).send_credentials(student.email, student.upn, password, imported(student).full_name)
        if result.status != "sent":
            # SMTP may have accepted DATA before a disconnect. Avoid duplicate credential emails.
            raise ValueError("No se confirmo la entrega SMTP. Cuenta creada; correo marcado para revision, sin reenvio automatico.")
        account.email_status, account.credential_encrypted = "sent", ""
        db.commit()
    return "Cuenta Microsoft creada por Ingreso, licencia asignada y correo enviado."


def execute_moodle(db, student, campaign, moodle, settings):
    logic.check_writes(settings)
    plan = moodle_plan(db, student, json.loads(campaign.rules_json), moodle, {})
    user_id = plan["user_id"]
    if not user_id:
        created = moodle.create_user(student.upn, student.first_name, student.last_name, student.upn)
        user_id = int(require_data(created, {"created", "ok"}, "Creacion usuario Moodle")["id"])
    managed = upsert_managed_user(db, imported(student), "student", moodle_user_id=str(user_id))
    enrolled_courses = {t["course_id"] for t in plan["targets"] if t["enrolled"]}
    for target in plan["targets"]:
        logic.check_writes(settings)
        if target["course_id"] not in enrolled_courses:
            result = moodle.enrol_user(user_id, target["course_id"], "student")
            if result.status != "enrolled":
                raise ValueError("No se pudo confirmar la matricula en " + target["course"])
            enrolled_courses.add(target["course_id"])
        group_id = target["group_id"]
        if not group_id:
            result = moodle.create_group(target["course_id"], target["group"])
            if result.status not in ("created", "ok"):
                result = moodle.get_group_in_course_by_name(target["course_id"], target["group"])
            group_id = int(require_data(result, {"created", "ok"}, "Grupo " + target["group"])["id"])
        if not target["member"]:
            result = moodle.add_group_member(group_id, user_id)
            if result.status != "added":
                raise ValueError("Matricula confirmada, pero no se pudo agregar el grupo en " + target["course"])
        placement = db.query(IngresoPlacement).filter_by(student_id=student.id, course=target["course"], group_name=target["group"]).first()
        if placement is None:
            placement = IngresoPlacement(student_id=student.id, course=target["course"], group_name=target["group"])
            db.add(placement)
        placement.moodle_user_id, placement.moodle_course_id, placement.moodle_group_id = user_id, target["course_id"], group_id
        db.commit()
        upsert_managed_enrollment(db, managed, target["course"], "student", target["course_name"], str(target["course_id"]))
    for old in plan["remove_groups"]:
        logic.check_writes(settings)
        result = moodle.delete_group_member(old["group_id"], user_id)
        if result.status != "removed":
            raise ValueError("Nuevos grupos aplicados; no se pudo retirar el grupo anterior " + old["group"])
        db.delete(db.get(IngresoPlacement, old["id"]))
        db.commit()
    return {"message": "Matriculas y grupos confirmados. Sin correos." +
                       (" Cursos anteriores conservados: " + ", ".join(plan["retained_courses"]) if plan["retained_courses"] else ""),
            "issues": plan["routing_issues"]}


def execute_job(db, job, campaign, settings, owner):
    logic.check_writes(settings)
    if job.environment != environment(settings):
        raise ValueError("Cambio la configuracion de servicios, licencia o correo. Preparar una nueva vista previa.")
    tasks = db.query(IngresoTask).filter_by(job_id=job.id, selected=True).order_by(IngresoTask.id).all()
    job.total, job.processed = len(tasks), 0
    client = IngresoGraphClient(settings) if job.kind == "microsoft" else MoodleClient(settings)
    errors = 0
    for index, task in enumerate(tasks, 1):
        logic.heartbeat(db, owner)
        try:
            student = db.get(IngresoStudent, task.student_id)
            if task.status == "done":
                continue
            if student.source_status != "current" or task.fingerprint != logic.fingerprint(student, campaign, job.kind):
                raise ValueError("Los datos SIAL o las reglas cambiaron. Preparar una nueva vista previa.")
            task.status = "running"
            db.commit()
            if job.kind == "microsoft":
                task.message = execute_microsoft(db, student, client, settings)
                issues = []
            else:
                result = execute_moodle(db, student, campaign, client, settings)
                task.message, issues = result["message"], result["issues"]
            task.status = "partial" if issues else "done"
            if task.status == "partial":
                errors += 1
                task.message += " " + " ".join(issues)
        except Exception as exc:
            db.rollback()
            errors += 1
            task.status, task.message = "error", safe_error(exc, settings)
        task.selected = False
        job.processed = index
        job.message = f"Procesados {index}/{job.total}; {errors} errores."
        db.add(OperationLog(actor=job.actor, action="ingreso_" + job.kind, status=task.status,
                            detail=f"Tarea #{task.id}, estudiante local #{task.student_id}: {task.message}"))
        db.commit()
        if index < len(tasks):
            time.sleep(min(60, max(0, settings.ingreso_step_pause)))
    job.status = "partial" if errors else "completed"


def run_job(job_id, owner, execute=False, session_factory=SessionLocal, settings=None):
    settings = settings or get_settings()
    with session_factory() as db:
        job = db.get(IngresoJob, job_id)
        try:
            logic.heartbeat(db, owner)
            if execute:
                logic.check_writes(settings)
            if job.environment != environment(settings):
                raise ValueError("La configuracion cambio desde que se preparo la tarea.")
            job.status = "running"
            job.finished_at = None
            db.commit()
            campaign = db.get(IngresoCampaign, job.campaign_id)
            if execute:
                if job.kind not in ("microsoft", "moodle"):
                    raise ValueError("Tipo de ejecucion invalido.")
                execute_job(db, job, campaign, settings, owner)
            elif job.kind == "sync":
                sync_job(db, job, campaign, settings, owner)
            else:
                preview_job(db, job, campaign, settings, owner)
        except Exception as exc:
            db.rollback()
            job.status, job.message = "error", safe_error(exc, settings)
        finally:
            job.finished_at = datetime.utcnow()
            db.add(OperationLog(actor=job.actor, action="ingreso_job", status=job.status,
                                detail=f"Ingreso #{job.id}: {job.kind}; {job.message}"))
            db.commit()
            logic.release(db, owner)
