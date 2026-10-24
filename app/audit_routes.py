from contextlib import closing
from datetime import date
import json
import math
import secrets
from urllib.parse import urlencode, urlparse
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import auditing as audit
from app.config import get_settings, update_env_file
from app.db import get_db
from app.models import AuditRun, AuditDeletion, AuditDeletionRow, LocalUser, OperationLog
from app.security import verify_password


def create_audit_router(templates, require_user):
    router = APIRouter()

    def actor(request: Request, db: Session = Depends(get_db)):
        name = require_user(request)
        user = db.query(LocalUser).filter_by(username=name, active=True).first()
        if not user:
            raise HTTPException(403, "Usuario no habilitado")
        return name

    def csrf(request):
        if "audit_csrf" not in request.session:
            request.session["audit_csrf"] = secrets.token_urlsafe(32)
        return request.session["audit_csrf"]

    def check_csrf(request, value):
        expected = request.session.get("audit_csrf", "")
        if not expected or not secrets.compare_digest(expected, value):
            raise HTTPException(403, "La sesion del formulario vencio. Volver a abrir Auditoria.")

    def render(request, name, **context):
        return templates.TemplateResponse(name, {"request": request, "csrf": csrf(request), **context})

    def redirect(message="", run_id=None, batch_id=None):
        params = {"message": message}
        if run_id:
            params["run_id"] = run_id
        if batch_id:
            return RedirectResponse(f"/auditoria/bajas/{batch_id}?" + urlencode(params), status_code=303)
        return RedirectResponse("/auditoria?" + urlencode(params), status_code=303)

    def get_run(db, run_id):
        run = db.get(AuditRun, run_id)
        if not run:
            raise HTTPException(404, "Auditoria inexistente")
        return run

    @router.get("/auditoria")
    def page(request: Request, run_id: int | None = None, view: str = "all", q: str = "",
             page: int = 1, per_page: int = 50, message: str = "", db: Session = Depends(get_db), username: str = Depends(actor)):
        audit.recover_stale(db)
        history = db.query(AuditRun).order_by(AuditRun.id.desc()).all()
        run = get_run(db, run_id) if run_id else (history[0] if history else None)
        view = view if view in audit.VIEWS else "all"
        per_page = per_page if per_page in (25, 50, 100, 200, 500) else 50
        people = audit.people_for_run(run) if run else []
        counts = audit.counts_for(people)
        # Candidate counts are a historical assessment, not a new authorization.
        completed = set()
        batches = []
        if run:
            batches = db.query(AuditDeletion).filter_by(run_id=run.id).order_by(AuditDeletion.id.desc()).all()
            completed = {r.dni for r in db.query(AuditDeletionRow).join(AuditDeletion, AuditDeletion.id == AuditDeletionRow.deletion_id)
                         .filter(AuditDeletion.run_id == run.id, AuditDeletionRow.moodle_status.in_(audit.SUCCESS), AuditDeletionRow.microsoft_status.in_(audit.SUCCESS)).all()}
        for p in people:
            p["removed"] = p["dni"] in completed
        filtered = audit.filter_people(people, view, q)
        total = len(filtered)
        pages = max(1, math.ceil(total / per_page))
        page = min(max(1, page), pages)
        candidate_count = sum(bool(p["candidate"]) for p in filtered)
        selected_count = sum(p["deletion_eligible"] and not p["removed"] for p in filtered)
        def link(p=page, v=view):
            return "/auditoria?" + urlencode({"run_id": run.id, "view": v, "q": q, "page": p, "per_page": per_page})
        return render(request, "audit.html", username=username, run=run, history=history, counts=counts,
                      views=audit.VIEWS, view=view, q=q, page=page, pages=pages, per_page=per_page,
                      people=filtered[(page - 1) * per_page:page * per_page], total=total,
                      eligible_count=selected_count, link=link, batches=batches, message=message,
                      candidate_count=candidate_count, campaign_closed=bool(run and audit.campaign_closed(run)),
                      config=get_settings(), ready=bool(get_settings().sial_username and get_settings().sial_password))

    @router.post("/auditoria/iniciar")
    def start(request: Request, background: BackgroundTasks, token: str = Form(""),
              db: Session = Depends(get_db), username: str = Depends(actor)):
        check_csrf(request, token)
        settings = get_settings()
        if not all((settings.sial_username, settings.sial_password, settings.moodle_base_url, settings.moodle_token,
                    settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret)):
            return redirect("Falta configurar SIAL, Moodle o Microsoft. Revisar Configuracion / Auditoria.")
        try:
            date.fromisoformat(settings.audit_campaign_start)
            owner = "audit:" + uuid.uuid4().hex
            audit.acquire_lease(db, owner)
            run = AuditRun(snapshot_key=uuid.uuid4().hex, campaign_start=settings.audit_campaign_start,
                           campaign_end=settings.audit_campaign_end, environment=audit.environment_key(settings), actor=username)
            db.add(run)
            db.commit()
        except ValueError as exc:
            return redirect(str(exc))
        background.add_task(audit.audit_job, run.id, owner)
        return redirect(run_id=run.id)

    @router.get("/auditoria/{run_id}/progreso")
    def progress(run_id: int, db: Session = Depends(get_db), username: str = Depends(actor)):
        audit.recover_stale(db)
        run = get_run(db, run_id)
        return {"status": run.status, "progress": run.progress, "message": run.message}

    @router.get("/auditoria/{run_id}/excel")
    def excel(run_id: int, db: Session = Depends(get_db), username: str = Depends(actor)):
        run = get_run(db, run_id)
        if run.status in ("queued", "running") or not audit.snapshot_path(run).exists():
            raise HTTPException(409, "La auditoria todavia no esta disponible")
        rows = db.query(AuditDeletionRow, AuditDeletion.actor).join(AuditDeletion, AuditDeletionRow.deletion_id == AuditDeletion.id).filter(AuditDeletion.run_id == run_id).all()
        log = [[r.deletion_id, r.dni, r.moodle_status, r.microsoft_status, r.message,
                r.checked_at, name] for r, name in rows]
        output = audit.export_excel(run, audit.people_for_run(run), log)
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="auditoria_{run.id}.xlsx"', "Cache-Control": "no-store"})

    @router.get("/auditoria/{run_id}/persona/{dni}")
    def person(request: Request, run_id: int, dni: str, db: Session = Depends(get_db), username: str = Depends(actor)):
        run = get_run(db, run_id)
        people = audit.people_for_run(run)
        p = next((x for x in people if x["dni"] == dni), None)
        if not p:
            raise HTTPException(404, "Persona inexistente")
        with closing(audit.read_snapshot(run)) as snap:
            courses = [dict(r) for r in snap.execute("SELECT * FROM moodle_cursos WHERE dni=?", (dni,))]
            teams = [dict(r) for r in snap.execute("SELECT * FROM teams_pertenencias JOIN equipos USING(equipo_id) WHERE dni=?", (dni,))]
            issues = [dict(r) for r in snap.execute("SELECT * FROM incidencias WHERE dni=? OR dni IS NULL", (dni,))]
        return render(request, "audit_person.html", username=username, run=run, person=p, courses=courses, teams=teams, issues=issues)

    @router.post("/auditoria/{run_id}/bajas/previa")
    def preview(request: Request, run_id: int, token: str = Form(""), dni: list[str] = Form([]),
                all_filtered: str = Form(""), view: str = Form("all"), q: str = Form(""),
                db: Session = Depends(get_db), username: str = Depends(actor)):
        check_csrf(request, token)
        run = get_run(db, run_id)
        if run.status not in ("completed", "partial"):
            return redirect("Finalizar la auditoria antes de preparar bajas.", run_id)
        people = audit.filter_people(audit.people_for_run(run), view, q)
        completed = {r.dni for r in db.query(AuditDeletionRow).join(AuditDeletion).filter(
            AuditDeletion.run_id == run.id, AuditDeletionRow.moodle_status.in_(audit.SUCCESS), AuditDeletionRow.microsoft_status.in_(audit.SUCCESS)).all()}
        eligible = {p["dni"]: p for p in people if p["deletion_eligible"] and p["dni"] not in completed}
        ids = set(eligible) if all_filtered == "yes" else set(dni)
        if not ids or not ids.issubset(eligible):
            return redirect("Seleccion vacia o contiene personas no habilitadas para baja.", run_id)
        batch = AuditDeletion(run_id=run.id, actor=username)
        db.add(batch)
        db.flush()
        for doc in sorted(ids):
            db.add(AuditDeletionRow(deletion_id=batch.id, dni=doc, snapshot_json=json.dumps(eligible[doc], ensure_ascii=True)))
        db.add(OperationLog(actor=username, action="audit_delete_preview", status="preview", detail=f"Lote #{batch.id}: {len(ids)} personas, sin cambios externos."))
        db.commit()
        return redirect(batch_id=batch.id)

    @router.get("/auditoria/bajas/{batch_id}")
    def deletion_page(request: Request, batch_id: int, message: str = "", db: Session = Depends(get_db), username: str = Depends(actor)):
        audit.recover_stale(db)
        batch = db.get(AuditDeletion, batch_id)
        if not batch:
            raise HTTPException(404, "Lote inexistente")
        rows = db.query(AuditDeletionRow).filter_by(deletion_id=batch_id).all()
        people = [{"row": r, "person": json.loads(r.snapshot_json)} for r in rows]
        run = get_run(db, batch.run_id)
        blocked = 0
        for entry in people:
            eligible, reason = audit.eligibility(entry["person"], run)
            entry["block_reason"] = "" if eligible else reason
            if not eligible and (entry["row"].moodle_status not in audit.SUCCESS or entry["row"].microsoft_status not in audit.SUCCESS):
                blocked += 1
        pending = sum(r.moodle_status not in audit.SUCCESS or r.microsoft_status not in audit.SUCCESS for r in rows)
        return render(request, "audit_delete.html", username=username, batch=batch, run=run,
                      people=people, pending=pending, blocked_count=blocked, message=message, config=get_settings())

    @router.post("/auditoria/bajas/{batch_id}/confirmar")
    def confirm(request: Request, batch_id: int, background: BackgroundTasks, token: str = Form(""),
                confirmation: str = Form(""), password: str = Form(""), understood: str = Form(""),
                db: Session = Depends(get_db), username: str = Depends(actor)):
        check_csrf(request, token)
        batch = db.get(AuditDeletion, batch_id)
        if not batch:
            raise HTTPException(404, "Lote inexistente")
        user = db.query(LocalUser).filter_by(username=username, active=True).one()
        if confirmation != "ELIMINAR" or understood != "yes" or not verify_password(password, user.password_hash):
            return redirect("Confirmacion incompleta o contrasena del CRM incorrecta.", batch_id=batch_id)
        if get_settings().crm_dry_run:
            return redirect("Modo simulacion: no se permiten bajas reales.", batch_id=batch_id)
        run = get_run(db, batch.run_id)
        if not audit.campaign_closed(run) or audit.environment_key(get_settings()) != run.environment:
            return redirect("Campana abierta o configuracion de servicios cambiada. Generar una nueva auditoria.", batch_id=batch_id)
        rows = db.query(AuditDeletionRow).filter_by(deletion_id=batch.id).all()
        if any(not audit.eligibility(json.loads(r.snapshot_json), run)[0] for r in rows
               if r.moodle_status not in audit.SUCCESS or r.microsoft_status not in audit.SUCCESS):
            return redirect("El lote contiene personas que no cumplen el criterio vigente. Preparar una nueva seleccion desde el informe.", batch_id=batch_id)
        try:
            owner = "delete:" + uuid.uuid4().hex
            audit.acquire_lease(db, owner)
            changed = db.query(AuditDeletion).filter(AuditDeletion.id == batch_id,
                AuditDeletion.status.in_(["preview", "partial", "error", "interrupted"])).update({
                    "status": "queued", "actor": username, "message": "Baja confirmada; en cola."})
            if not changed:
                db.rollback()
                return redirect("El lote ya fue ejecutado o esta en proceso.", batch_id=batch_id)
            db.add(OperationLog(actor=username, action="audit_delete_confirmed", status="queued", detail=f"Lote #{batch_id}, auditoria #{run.id}"))
            db.commit()
        except ValueError as exc:
            return redirect(str(exc), batch_id=batch_id)
        background.add_task(audit.deletion_job, batch_id, owner)
        return redirect(batch_id=batch_id)

    @router.get("/auditoria/bajas/{batch_id}/progreso")
    def deletion_progress(batch_id: int, db: Session = Depends(get_db), username: str = Depends(actor)):
        audit.recover_stale(db)
        batch = db.get(AuditDeletion, batch_id)
        if not batch:
            raise HTTPException(404, "Lote inexistente")
        return {"status": batch.status, "message": batch.message}

    @router.get("/settings/audit")
    def config_page(request: Request, message: str = "", username: str = Depends(actor)):
        return render(request, "audit_settings.html", username=username, config=get_settings(), message=message)

    @router.post("/settings/audit")
    def config_save(request: Request, token: str = Form(""), campaign_start: str = Form(...), campaign_end: str = Form(""),
                    base_url: str = Form(...), sial_username: str = Form(""), sial_password: str = Form(""),
                    db: Session = Depends(get_db), username: str = Depends(actor)):
        check_csrf(request, token)
        try:
            start = date.fromisoformat(campaign_start)
            end = date.fromisoformat(campaign_end) if campaign_end else None
            if (end and end < start) or start > date.today():
                raise ValueError("Revisar las fechas de campana.")
            url = urlparse(base_url)
            if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
                raise ValueError("SIAL requiere una URL HTTPS sin credenciales, parametros ni fragmentos.")
            updates = {"AUDIT_CAMPAIGN_START": start.isoformat(), "AUDIT_CAMPAIGN_END": end.isoformat() if end else "",
                       "SIAL_BASE_URL": base_url.rstrip("/")}
            if sial_username.strip():
                updates["SIAL_USERNAME"] = sial_username.strip()
            if sial_password:
                updates["SIAL_PASSWORD"] = sial_password
            update_env_file(updates)
            get_settings.cache_clear()
            templates.env.globals["settings"] = get_settings()
            db.add(OperationLog(actor=username, action="audit_config", status="saved", detail="Configuracion SIAL/campana actualizada; sin credenciales en bitacora."))
            db.commit()
            message = "Configuracion guardada. Se aplicara a las nuevas auditorias."
        except ValueError as exc:
            message = str(exc)
        return RedirectResponse("/settings/audit?" + urlencode({"message": message}), status_code=303)

    return router
