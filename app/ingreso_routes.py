from datetime import date
import io
import json
import math
import secrets
from urllib.parse import urlencode
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy.exc import IntegrityError

from app import ingreso as logic, ingreso_jobs as jobs
from app.audit_sources import add_sheet
from app.config import get_settings
from app.db import get_db
from app.models import (IngresoCampaign, IngresoStudent, IngresoJob, IngresoTask, IngresoAccount, LocalUser,
                        OperationLog)


def create_ingreso_router(templates, require_user):
    router = APIRouter()

    def actor(request: Request, db=Depends(get_db)):
        username = require_user(request)
        if not db.query(LocalUser).filter_by(username=username, active=True).first():
            raise HTTPException(403, "Usuario no habilitado")
        return username

    def token(request):
        if "ingreso_csrf" not in request.session:
            request.session["ingreso_csrf"] = secrets.token_urlsafe(32)
        return request.session["ingreso_csrf"]

    def check(request, value):
        expected = request.session.get("ingreso_csrf", "")
        if not expected or not secrets.compare_digest(expected, value):
            raise HTTPException(403, "Formulario vencido. Volver a abrir Ingreso.")

    def render(request, name, **context):
        return templates.TemplateResponse(name, {"request": request, "csrf": token(request),
            "states": logic.STATES, "kinds": logic.KINDS, "write_enabled": get_settings().ingreso_allow_writes and not get_settings().crm_dry_run, **context})

    def redirect(message="", campaign_id=None, job_id=None, settings=False):
        path = "/settings/ingreso" if settings else f"/ingreso/jobs/{job_id}" if job_id else "/ingreso"
        return RedirectResponse(path + "?" + urlencode({"message": message, **({"campaign_id": campaign_id} if campaign_id else {})}), status_code=303)

    def campaign_for(db, campaign_id):
        campaign = db.get(IngresoCampaign, campaign_id)
        if campaign is None:
            raise HTTPException(404, "Campana inexistente")
        return campaign

    def job_for(db, job_id):
        job = db.get(IngresoJob, job_id)
        if job is None:
            raise HTTPException(404, "Tarea inexistente")
        return job

    def people_for(db, campaign):
        people = db.query(IngresoStudent).filter_by(campaign_id=campaign.id).order_by(IngresoStudent.last_name, IngresoStudent.first_name, IngresoStudent.dni).all()
        tasks = db.query(IngresoTask, IngresoJob.kind).join(IngresoJob).filter(IngresoJob.campaign_id == campaign.id).order_by(IngresoTask.id.desc()).all()
        latest = {}
        for task, kind in tasks:
            latest.setdefault((task.student_id, kind), task)
        accounts = {a.upn: a for a in db.query(IngresoAccount).filter(IngresoAccount.upn.in_([p.upn for p in people])).all()} if people else {}
        rules = json.loads(campaign.rules_json)
        result = []
        for p in people:
            registrations = json.loads(p.registrations_json)
            targets, issues = logic.desired_courses(registrations, rules)
            result.append({"student": p, "registrations": registrations,
                           "conditions": sorted({s for r in registrations for s in r["states"]}),
                           "targets": targets, "issues": issues, "account": accounts.get(p.upn),
                           "microsoft": latest.get((p.id, "microsoft")), "moodle": latest.get((p.id, "moodle"))})
        return result

    @router.get("/ingreso")
    def page(request: Request, campaign_id: int | None = None, section: str = "sial", q: str = "", condition: str = "",
             page: int = 1, per_page: int = 50, message: str = "", db=Depends(get_db), username: str = Depends(actor)):
        logic.recover(db)
        campaigns = db.query(IngresoCampaign).order_by(IngresoCampaign.id.desc()).all()
        campaign = campaign_for(db, campaign_id) if campaign_id else (campaigns[0] if campaigns else None)
        section = section if section in ("sial", "microsoft", "moodle", "history") else "sial"
        people = people_for(db, campaign) if campaign else []
        counts = {s: sum(s in p["conditions"] for p in people) for s in logic.SIAL_STATES}
        counts.update(total=len(people), current=sum(p["student"].source_status == "current" for p in people),
                      pending_sync=sum(p["student"].source_status == "pending_sync" for p in people))
        search = q.strip().casefold()
        filtered = [p for p in people if (not condition or condition in p["conditions"] or condition == p["student"].source_status)
                    and (not search or search in " ".join(str(getattr(p["student"], k)) for k in ("dni", "upn", "first_name", "last_name", "email")).casefold())]
        per_page = per_page if per_page in (25, 50, 100, 200, 500) else 50
        pages = max(1, math.ceil(len(filtered) / per_page))
        page = min(max(1, page), pages)
        def link(p=1, s=section):
            return "/ingreso?" + urlencode({"campaign_id": campaign.id, "section": s, "q": q, "condition": condition, "page": p, "per_page": per_page})
        history = db.query(IngresoJob).filter_by(campaign_id=campaign.id).order_by(IngresoJob.id.desc()).limit(100).all() if campaign else []
        active = next((j for j in history if j.status in ("queued", "running", "queued_execute")), None)
        return render(request, "ingreso.html", username=username, campaigns=campaigns, campaign=campaign, section=section,
                      people=filtered[(page-1)*per_page:page*per_page], total=len(filtered), counts=counts, page=page, pages=pages,
                      per_page=per_page, q=q, condition=condition, link=link, history=history, active=active, message=message)

    @router.post("/ingreso/{campaign_id}/prepare/{kind}")
    def prepare(request: Request, campaign_id: int, kind: str, background: BackgroundTasks,
                token: str = Form(""), db=Depends(get_db), username: str = Depends(actor)):
        check(request, token)
        if kind not in logic.KINDS:
            raise HTTPException(400, "Tipo de tarea invalido")
        campaign_for(db, campaign_id)
        settings = get_settings()
        if kind == "sync" and not (settings.sial_username and settings.sial_password):
            return redirect("Configurar las credenciales SIAL en Configuracion / Auditoria.", campaign_id)
        try:
            owner = "ingreso:" + uuid.uuid4().hex
            logic.acquire(db, owner)
            job = IngresoJob(campaign_id=campaign_id, kind=kind, actor=username, environment=jobs.environment(settings))
            db.add(job)
            db.commit()
        except ValueError as exc:
            return redirect(str(exc), campaign_id)
        background.add_task(jobs.run_job, job.id, owner)
        return redirect(job_id=job.id)

    @router.get("/ingreso/jobs/{job_id}")
    def job_page(request: Request, job_id: int, view: str = "all", page: int = 1, per_page: int = 50,
                 message: str = "", db=Depends(get_db), username: str = Depends(actor)):
        logic.recover(db)
        job = job_for(db, job_id)
        campaign = campaign_for(db, job.campaign_id)
        query = db.query(IngresoTask).filter_by(job_id=job.id)
        if view in ("ready", "blocked", "done", "error", "exists", "partial"):
            query = query.filter_by(status=view)
        else:
            view = "all"
        total = query.count()
        per_page = per_page if per_page in (25, 50, 100, 200, 500) else 50
        pages = max(1, math.ceil(total/per_page))
        page = min(max(1, page), pages)
        tasks = query.order_by(IngresoTask.id).offset((page-1)*per_page).limit(per_page).all()
        rows = [{"task": task, "data": json.loads(task.snapshot_json)} for task in tasks]
        eligible = query.filter(IngresoTask.status.in_(["ready", "error"])).count()
        def link(p=1, v=view):
            return f"/ingreso/jobs/{job.id}?" + urlencode({"view": v, "page": p, "per_page": per_page})
        counts = {s: db.query(IngresoTask).filter_by(job_id=job.id, status=s).count() for s in ("ready", "blocked", "done", "error", "exists", "partial")}
        return render(request, "ingreso_job.html", username=username, job=job, campaign=campaign, rows=rows,
                      counts=counts, eligible=eligible, link=link, page=page, pages=pages, per_page=per_page,
                      total=total, view=view, message=message)

    @router.get("/ingreso/jobs/{job_id}/progress")
    def progress(job_id: int, db=Depends(get_db), username: str = Depends(actor)):
        logic.recover(db)
        job = job_for(db, job_id)
        return {"status": job.status, "message": job.message, "processed": job.processed, "total": job.total}

    @router.post("/ingreso/jobs/{job_id}/execute")
    def execute(request: Request, job_id: int, background: BackgroundTasks, token: str = Form(""),
                task_id: list[int] = Form([]), all_filtered: str = Form(""), view: str = Form("all"),
                confirmation: str = Form(""), db=Depends(get_db), username: str = Depends(actor)):
        check(request, token)
        job = job_for(db, job_id)
        settings = get_settings()
        try:
            logic.check_writes(settings)
            if job.kind == "sync" or job.status not in ("preview", "partial", "completed", "interrupted", "error"):
                raise ValueError("La tarea no esta disponible para ejecucion.")
            if confirmation != "PROCESAR":
                raise ValueError("Escribir PROCESAR para confirmar la ejecucion real.")
            if job.environment != jobs.environment(settings):
                raise ValueError("Configuracion modificada. Preparar otra vista previa.")
            campaign = campaign_for(db, job.campaign_id)
            query = db.query(IngresoTask).filter(IngresoTask.job_id == job.id, IngresoTask.status.in_(["ready", "error"]))
            if view != "all":
                query = query.filter_by(status=view)
            eligible = {t.id: t for t in query.all()}
            selected = set(eligible) if all_filtered == "yes" else set(task_id)
            if not selected or not selected.issubset(eligible):
                raise ValueError("Seleccion vacia o contiene filas no habilitadas.")
            for key in selected:
                task = eligible[key]
                student = db.get(IngresoStudent, task.student_id)
                if student.source_status != "current" or task.fingerprint != logic.fingerprint(student, campaign, job.kind):
                    raise ValueError("SIAL o las reglas cambiaron. Preparar otra vista previa.")
            owner = "ingreso:" + uuid.uuid4().hex
            logic.acquire(db, owner)
            # Recheck status after the persistent lease prevents double submission across workers.
            db.refresh(job)
            if job.status in ("queued", "queued_execute", "running"):
                db.rollback()
                raise ValueError("La tarea ya esta en proceso.")
            db.query(IngresoTask).filter_by(job_id=job.id).update({"selected": False})
            db.query(IngresoTask).filter(IngresoTask.id.in_(selected)).update({"selected": True})
            job.status, job.actor, job.message = "queued_execute", username, "Ejecucion confirmada; en cola."
            db.add(OperationLog(actor=username, action="ingreso_confirm", status="queued", detail=f"Ingreso #{job.id}, {len(selected)} filas."))
            db.commit()
        except ValueError as exc:
            return redirect(str(exc), job_id=job.id)
        background.add_task(jobs.run_job, job.id, owner, execute=True)
        return redirect(job_id=job.id)

    @router.get("/ingreso/{campaign_id}/excel")
    def excel(campaign_id: int, db=Depends(get_db), username: str = Depends(actor)):
        campaign = campaign_for(db, campaign_id)
        wb = Workbook()
        wb.remove(wb.active)
        people = people_for(db, campaign)
        rows = []
        registrations = []
        for p in people:
            s = p["student"]
            rows.append([s.dni, s.last_name, s.first_name, s.upn, s.email, s.phone, "; ".join(p["conditions"]),
                         s.source_status, "; ".join(t["course"] + " / " + t["group"] for t in p["targets"]),
                         " ".join(p["issues"]), p["microsoft"].status if p["microsoft"] else "pending",
                         p["account"].email_status if p["account"] else "pending", p["moodle"].status if p["moodle"] else "pending",
                         s.last_seen_at, s.personal_message])
            for r in p["registrations"]:
                registrations.append([s.dni, r["career_id"], r["career"], r["modality"], "; ".join(r["commissions"]), "; ".join(r["states"]), r["date"]])
        add_sheet(wb, "Estudiantes", ["DNI", "Apellido", "Nombre", "UPN", "Email personal", "Telefono", "Condiciones SIAL",
                  "Ultima consulta", "Destinos previstos Moodle", "Pendientes de distribucion", "Ultima tarea Microsoft", "Correo", "Ultima tarea Moodle", "Visto en SIAL", "Datos personales"], rows)
        add_sheet(wb, "Inscripciones", ["DNI", "Carrera ID", "Carrera", "Modalidad", "Comisiones", "Condiciones", "Fecha inscripcion"], registrations)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="ingreso_{campaign.id}.xlsx"', "Cache-Control": "no-store"})

    @router.get("/settings/ingreso")
    def settings_page(request: Request, campaign_id: int | None = None, message: str = "", db=Depends(get_db), username: str = Depends(actor)):
        campaign = campaign_for(db, campaign_id) if campaign_id else None
        return render(request, "ingreso_settings.html", username=username, campaign=campaign,
                      campaigns=db.query(IngresoCampaign).order_by(IngresoCampaign.id.desc()).all(),
                      rules=json.loads(campaign.rules_json) if campaign else logic.default_rules(), message=message)

    @router.post("/settings/ingreso")
    async def settings_save(request: Request, db=Depends(get_db), username: str = Depends(actor)):
        form = await request.form()
        check(request, str(form.get("token", "")))
        cid = str(form.get("campaign_id", ""))
        owner = "ingreso-config:" + uuid.uuid4().hex
        acquired = False
        try:
            campaign = campaign_for(db, int(cid)) if cid else None
            name = str(form.get("name", "")).strip()
            start, end = str(form.get("start_date", "")), str(form.get("end_date", ""))
            if not name or len(name) > 120 or date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("Revisar nombre y fechas de la campana.")
            fields = [form.getlist(k) for k in ("career_id", "career_name", "career_kind", "career_courses")]
            if len({len(x) for x in fields}) != 1:
                raise ValueError("Tabla de carreras incompleta.")
            rules = {"careers": {}, "accounting": {"P": {}, "D": {}}}
            for key, label, kind, codes in zip(*fields):
                key = key.strip()
                if not key and not label.strip() and not codes.strip():
                    continue
                if key in rules["careers"]:
                    raise ValueError("ID de carrera repetido.")
                rules["careers"][key] = {"name": label.strip(), "kind": kind, "courses": sorted(set(c.strip() for c in codes.split(",") if c.strip()))}
            for group, mapping in logic.default_rules()["accounting"].items():
                for commission in mapping:
                    rules["accounting"][group][commission] = str(form.get(group + ":" + commission, ""))
            logic.validate_rules(rules)
            logic.acquire(db, owner)
            acquired = True
            period_changed = False
            previous_period = ""
            if not campaign:
                campaign = IngresoCampaign(name=name, start_date=start, end_date=end, rules_json=logic.dump(rules))
                db.add(campaign)
            else:
                db.refresh(campaign)
                period_changed = (campaign.start_date, campaign.end_date) != (start, end)
                if period_changed:
                    previous_period = f" Fechas: {campaign.start_date}/{campaign.end_date} -> {start}/{end}."
                    # Keep history and confirmed operations, but never execute previews for the old period.
                    db.query(IngresoStudent).filter_by(campaign_id=campaign.id).update({"source_status": "pending_sync"})
                    job_ids = db.query(IngresoJob.id).filter_by(campaign_id=campaign.id).scalar_subquery()
                    db.query(IngresoTask).filter(IngresoTask.job_id.in_(job_ids), IngresoTask.status.in_(["ready", "error"])).update(
                        {"status": "blocked", "selected": False,
                         "message": IngresoTask.message + " Periodo modificado. Actualizar desde SIAL y preparar otra vista previa."},
                        synchronize_session=False)
                campaign.name, campaign.start_date, campaign.end_date, campaign.rules_json = name, start, end, logic.dump(rules)
            db.add(OperationLog(actor=username, action="ingreso_config", status="saved", detail="Configuracion local de campana: " + name + previous_period))
            db.commit()
            message = ("Fechas actualizadas. Actualizar desde SIAL para consultar el nuevo periodo. Historial conservado; vistas previas anteriores no ejecutables."
                       if period_changed else "Configuracion guardada. No se realizaron cambios externos.")
            return redirect(message, campaign.id)
        except (ValueError, IntegrityError) as exc:
            db.rollback()
            message = "Ya existe una campana con ese nombre." if isinstance(exc, IntegrityError) else str(exc)
            return redirect(message, int(cid) if cid.isdigit() else None, settings=True)
        finally:
            if acquired:
                logic.release(db, owner)

    return router
