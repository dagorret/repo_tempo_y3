"""Persistent read-only audits and explicitly confirmed, revalidated account removals."""
from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

import httpx
from openpyxl import Workbook

from app import audit_sources as sources, audit_reports as reports
from app.config import get_settings
from app.db import SessionLocal
from app.models import (AuditRun, AuditLease, AuditDeletion, AuditDeletionRow, OperationLog,
                        ManagedUser, ManagedEnrollment, MatriculationState)
from app.services.graph import GraphClient
from app.services.moodle import MoodleClient


SUCCESS = {"deleted", "missing"}
ACTIVITY_NONE_180 = "Sin actividad informada (180 dias)"
ACTIVITY_HISTORICAL = "Actividad historica fuera de ventana"
ACTIVITY_RECENT = "Actividad registrada (180 dias)"
ACTIVITY_INCOMPLETE = "Datos incompletos"
VIEWS = {"all": "Todas las personas", "preinscriptos": "Preinscriptos",
         "pre_candidates": "Preinscriptos sin cursos/equipos", "pre_enrolled": "Preinscriptos con cursos",
         "aspirantes": "Aspirantes", "ingresantes": "Ingresantes",
         "candidates": "Candidatos a baja", "asp_candidates": "Aspirantes sin actividad",
         "incomplete": "Datos incompletos", "accounts": "Con cuentas"}


def environment_key(settings):
    values = [settings.sial_base_url.rstrip("/"), settings.moodle_base_url.rstrip("/"),
              settings.azure_tenant_id, settings.azure_client_id, settings.crm_domain]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def snapshot_path(run, settings=None):
    if not re.fullmatch(r"[a-f0-9]{32}", run.snapshot_key):
        raise ValueError("Identificador de auditoria invalido")
    return Path((settings or get_settings()).audit_data_dir) / run.snapshot_key / "auditoria.sqlite3"


def read_snapshot(run, settings=None):
    db = sqlite3.connect(snapshot_path(run, settings).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def campaign_closed(run, today=None):
    return bool(run.campaign_end and date.fromisoformat(run.campaign_end) < (today or date.today()))


def registration_date(value):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


def in_campaign(careers, run):
    if not careers:
        return False
    start = date.fromisoformat(run.campaign_start)
    end = date.fromisoformat(run.campaign_end) if run.campaign_end else date.today()
    dates = [registration_date(c.get("fecha_inscri", c.get("fecha_inscripcion"))) for c in careers]
    return all(d is not None and start <= d <= end for d in dates)


def _moodle_without_courses(p):
    return p["moodle_estado"] == "sin_usuario" or (
        p["moodle_cursos_estado"] == "consultado" and p["moodle_cursos_cantidad"] == 0
    )


def _teams_without_memberships(p):
    return p["microsoft_estado"] == "sin_usuario" or (
        p["microsoft_estado"] == "encontrado"
        and p["teams_estado"] == "consultado"
        and p["teams_cantidad"] == 0
    )


def _account_age_block_reason(p, today=None):
    today = today or date.today()
    try:
        created = datetime.fromisoformat(str(p["microsoft_creada"]).replace("Z", "+00:00")).date()
    except (ValueError, TypeError, AttributeError):
        return "Microsoft: antiguedad de cuenta sin verificar; revision manual necesaria."
    if (today - created).days < 180:
        return "Microsoft: cuenta con menos de 180 dias; revision manual necesaria."
    return ""


def _activity_block_reason(p, today=None):
    if p["microsoft_estado"] == "sin_usuario":
        return ""
    activity = p.get("actividad")
    if activity == ACTIVITY_NONE_180:
        return _account_age_block_reason(p, today)
    if activity == ACTIVITY_RECENT:
        return "Microsoft 365/Teams registra actividad dentro de los ultimos 180 dias; baja bloqueada."
    if activity == ACTIVITY_HISTORICAL:
        return "Microsoft 365/Teams informa actividad historica; requiere revision manual antes de una baja."
    return "No se pudo confirmar ausencia de actividad Microsoft 365/Teams durante 180 dias; baja bloqueada."


def candidate_assessment(p, run, today=None):
    if not in_campaign(p.get("inscripciones", []), run):
        return "", "Inscripcion fuera de la campana o fecha sin verificar."
    state = p["estados_sial"]
    if state not in ("preinscriptos", "aspirantes"):
        return "", "Ingresante o condiciones SIAL superpuestas; conservar."
    if any(p[k] not in ("encontrado", "sin_usuario") for k in ("moodle_estado", "microsoft_estado")):
        return "", "Consulta de identidad incompleta."
    if p["moodle_estado"] == p["microsoft_estado"] == "sin_usuario":
        return "", "Sin cuentas que eliminar."
    if not _moodle_without_courses(p):
        if p["moodle_cursos_cantidad"] is not None and p["moodle_cursos_cantidad"] > 0:
            return "", "Conservar: tiene matriculas en Moodle, aunque SIAL indique preinscripto o aspirante."
        return "", "Cursos Moodle sin verificar; baja bloqueada."
    if not _teams_without_memberships(p):
        if p["teams_estado"] == "consultado" and p["teams_cantidad"] is not None and p["teams_cantidad"] > 0:
            return "", "Conservar: tiene equipos Teams directos."
        return "", "Equipos Teams sin verificar; baja bloqueada."
    if state == "preinscriptos":
        return "preinscripto", (
            "Preinscripto sin cursos Moodle ni equipos Teams directos. "
            "La seleccion requiere ademas ausencia de actividad Microsoft 365/Teams en 180 dias."
        )
    activity_reason = _activity_block_reason(p, today)
    if activity_reason:
        return "", activity_reason
    return "aspirante", "Sin cursos Moodle ni equipos directos; sin uso Microsoft informado en 180 dias."


def eligibility(p, run, today=None):
    if not campaign_closed(run, today):
        return "", "Campana abierta: bajas deshabilitadas."
    candidate, reason = candidate_assessment(p, run, today)
    if not candidate:
        return "", reason
    activity_reason = _activity_block_reason(p, today)
    if activity_reason:
        return "", activity_reason
    return candidate, reason


def people_for_run(run, settings=None):
    if not snapshot_path(run, settings).exists():
        return []
    with closing(read_snapshot(run, settings)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "evaluacion_actividad" not in tables:
            return []
        people = [dict(r) for r in db.execute("""SELECT p.*, e.actividad,e.ultima_actividad,e.motivo,
            r.decision_moodle,r.decision_microsoft,r.responsable,r.observaciones
            FROM personas p LEFT JOIN evaluacion_actividad e USING(dni)
            LEFT JOIN revisiones r USING(dni) ORDER BY apellido,nombre,dni""")]
        careers = {}
        for r in db.execute("SELECT * FROM sial_inscripciones"):
            careers.setdefault(r["dni"], []).append(dict(r))
    for p in people:
        p["inscripciones"] = careers.get(p["dni"], [])
        p["candidate"], p["reason"] = candidate_assessment(p, run)
        eligible, block_reason = eligibility(p, run)
        completed = run.status in ("completed", "partial")
        p["deletion_eligible"] = bool(eligible) and completed
        p["deletion_block_reason"] = ("" if p["deletion_eligible"] else
            block_reason if not eligible else "Auditoria sin finalizar; bajas deshabilitadas.")
        p["incomplete"] = any(p[k] in ("pendiente", "error") for k in (
            "moodle_estado", "moodle_cursos_estado", "microsoft_estado", "teams_estado")) or p.get("actividad") in (None, ACTIVITY_INCOMPLETE)
    return people


def filter_people(people, view="all", query=""):
    query = query.strip().casefold()
    def matches(p):
        condition = {
            "all": True, "candidates": bool(p["candidate"]),
            "pre_candidates": p["candidate"] == "preinscripto",
            "pre_enrolled": "preinscriptos" in p["estados_sial"].split("; ") and (p["moodle_cursos_cantidad"] or 0) > 0,
            "asp_candidates": p["candidate"] == "aspirante", "incomplete": p["incomplete"],
            "accounts": "encontrado" in (p["moodle_estado"], p["microsoft_estado"]),
            **{s: s in p["estados_sial"].split("; ") for s in ("preinscriptos", "aspirantes", "ingresantes")},
        }.get(view, False)
        return condition and (not query or query in " ".join(str(p.get(k) or "") for k in (
            "dni", "upn", "nombre", "apellido")).casefold())
    return [p for p in people if matches(p)]


def counts_for(people):
    return {k: len(filter_people(people, k)) for k in VIEWS}


def acquire_lease(db, owner):
    # One persistent lease serializes this module across browsers and server workers.
    from sqlalchemy.dialects.sqlite import insert
    now = datetime.utcnow()
    db.execute(insert(AuditLease).values(id=1, owner="", expires_at=now).on_conflict_do_nothing())
    changed = db.query(AuditLease).filter(AuditLease.id == 1, AuditLease.expires_at <= now).update(
        {"owner": owner, "expires_at": now + timedelta(minutes=30)})
    if not changed:
        db.rollback()
        raise ValueError("Hay una auditoria o baja en ejecucion. Esperar a que termine.")


def heartbeat(db, owner):
    now = datetime.utcnow()
    changed = db.query(AuditLease).filter(AuditLease.id == 1, AuditLease.owner == owner,
        AuditLease.expires_at > now).update({"expires_at": now + timedelta(minutes=30)})
    if not changed:
        raise RuntimeError("La tarea perdio su bloqueo. No se continuara automaticamente.")
    db.commit()


def release_lease(db, owner):
    db.query(AuditLease).filter(AuditLease.owner == owner).update({"expires_at": datetime.utcnow()})
    db.commit()


def recover_stale(db):
    lease = db.get(AuditLease, 1)
    if lease and lease.expires_at > datetime.utcnow():
        return
    for model in (AuditRun, AuditDeletion):
        db.query(model).filter(model.status.in_(["queued", "running"])).update({
            "status": "interrupted", "message": "Proceso interrumpido; resultados parciales conservados."})
    db.commit()


def safe_error(exc, settings=None):
    settings = settings or get_settings()
    if isinstance(exc, httpx.TimeoutException):
        return "Tiempo de espera agotado al consultar el servicio; reintentar."
    if isinstance(exc, httpx.HTTPError):
        return "Error de conexion con el servicio (" + type(exc).__name__ + ")."
    message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
    for secret in (settings.sial_password, settings.sial_username, settings.moodle_token, settings.azure_client_secret):
        if secret:
            message = message.replace(secret, "[reservado]")
    return message[:700] or type(exc).__name__


def audit_job(run_id, owner, session_factory=SessionLocal, settings=None):
    settings = settings or get_settings()
    with session_factory() as db:
        run = db.get(AuditRun, run_id)
        try:
            heartbeat(db, owner)
            run.status = "running"
            db.commit()
            def report(phase, **values):
                starts = {"SIAL": 2, "poblacion": 5, "usuarios_moodle": 6,
                          "cursos_moodle": 12, "usuarios_microsoft": 60,
                          "equipos_teams": 76, "informe_uso": 92}
                spans = {"usuarios_moodle": 5, "cursos_moodle": 47, "usuarios_microsoft": 15, "equipos_teams": 15}
                done, total = values.get("procesados", 0), values.get("total", 0)
                run.progress = max(run.progress, starts.get(phase, run.progress) + int(spans.get(phase, 0) * done / max(total, 1)))
                run.message = phase.replace("_", " ") + (f": {done}/{total}" if total else ": " + str(values.get("estado", values.get("informe", ""))))
                heartbeat(db, owner)
            with closing(sources.open_db(snapshot_path(run, settings).parent)) as snapshot:
                reports.init_reports(snapshot)
                sources.meta(snapshot, "inicio", sources.now())
                sources.fetch_sial(snapshot, run.campaign_start.replace("-", "") + "0000", settings, report, include_ingresantes=True)
                for phase, func in (("Moodle", sources.fetch_moodle), ("Microsoft", sources.fetch_graph)):
                    try:
                        kwargs = {"probe_permissions": False} if phase == "Microsoft" else {}
                        func(snapshot, settings, report, **kwargs)
                    except Exception as exc:
                        snapshot.rollback()
                        with snapshot:
                            sources.incident(snapshot, phase, None, "consulta", safe_error(exc, settings))
                run.message = "Completando nombres e informes Microsoft 365"
                heartbeat(db, owner)
                sources.fetch_missing_names(snapshot, settings)
                reports.collect(snapshot, settings, report)
                reports.evaluate(snapshot)
                sources.meta(snapshot, "fin", sources.now())
            people = people_for_run(run, settings)
            counts = counts_for(people)
            run.counts_json = json.dumps(counts)
            run.status = "partial" if counts["incomplete"] else "completed"
            run.progress = 100
            run.message = f"{len(people)} personas. {counts['incomplete']} con datos incompletos."
        except Exception as exc:
            db.rollback()
            run = db.get(AuditRun, run_id)
            run.status = "error"
            run.message = safe_error(exc, settings)
        finally:
            run.finished_at = datetime.utcnow()
            db.add(OperationLog(actor=run.actor, action="audit_read", status=run.status,
                                detail=f"Auditoria #{run.id}: {run.message}"))
            db.commit()
            release_lease(db, owner)


def export_excel(run, people, deletions=(), settings=None):
    wb = Workbook()
    wb.remove(wb.active)
    counts = counts_for(people)
    strict_preinscriptos = [
        p for p in people
        if p["estados_sial"] == "preinscriptos"
        and _moodle_without_courses(p)
        and _teams_without_memberships(p)
    ]
    enabled_preinscriptos = [p for p in strict_preinscriptos if p["deletion_eligible"]]
    summary = [(VIEWS[k], v) for k, v in counts.items()]
    summary.extend([
        ("Preinscriptos sin cursos/equipos (incluye sin cuentas)", len(strict_preinscriptos)),
        ("Preinscriptos habilitados para baja", len(enabled_preinscriptos)),
    ])
    sources.add_sheet(wb, "Resumen", ["Dato", "Cantidad"], summary)
    notes = [
        ("Campana", run.campaign_start + " a " + (run.campaign_end or "abierta")),
        ("Auditoria", f"#{run.id}; {sources.local_date(run.created_at.isoformat() + '+00:00')}; {run.status}"),
        ("Preinscriptos", "Para ser candidato se exige condicion SIAL exclusivamente preinscripto, sin cursos Moodle y sin equipos Teams directos. Para seleccionarlo se exige ademas actividad Microsoft 365/Teams ausente y verificada durante 180 dias; las bajas solo se habilitan con campana cerrada."),
        ("Aspirantes", "Se exige ausencia verificada de cursos Moodle, equipos Teams directos y actividad Microsoft 365/Teams durante 180 dias; consultas completas."),
        ("SIAL", "La API no informa documentos adeudados ni actividades de ingreso pendientes. No se infieren estos motivos."),
        ("Actividad 180 dias", "Sin actividad informada exige filas validas en los informes Microsoft 365 y Teams, sin fechas de uso ni acciones Teams positivas. Actividad reciente, historica, informes incompletos o cuentas con menos de 180 dias bloquean la seleccion."),
        ("Limites", "Equipos significa membresias directas de joinedTeams; no cubre equipos accesibles solo por canales compartidos. Sin fila en un informe es desconocido, no inactivo."),
        ("Bajas", "Requieren seleccion, confirmacion y revalidacion actual de SIAL, Moodle, equipos Teams y actividad. Microsoft elimina la cuenta Microsoft 365 completa, no solo Teams."),
        ("Decisiones", "Las anotaciones realizadas en Excel no se importan automaticamente al CRM."),
    ]
    sources.add_sheet(wb, "Alcance", ["Criterio", "Detalle"], notes, {2:110})
    headers = ["DNI", "Apellido", "Nombre", "Condicion SIAL", "Usuario", "Cuenta Moodle", "Cursos Moodle",
               "Ultimo acceso Moodle", "Cuenta Microsoft", "Equipos Teams directos", "Actividad Microsoft 365/Teams (180 dias)",
               "Ultima actividad", "Candidato", "Motivo", "Decision Moodle", "Decision Microsoft", "Responsable", "Observaciones",
               "Habilitado para preparar baja", "Motivo del bloqueo"]
    def row(p):
        return [p[k] for k in ("dni", "apellido", "nombre", "estados_sial", "upn", "moodle_estado", "moodle_cursos_cantidad")] + [
            sources.local_date(p["moodle_ultimo_acceso"]), p["microsoft_estado"], p["teams_cantidad"], p["actividad"],
            p["ultima_actividad"], p["candidate"], p["reason"], p["decision_moodle"], p["decision_microsoft"], p["responsable"], p["observaciones"],
            "Si" if p["deletion_eligible"] else "No", p["deletion_block_reason"]]
    for title, view in (("Personas", "all"), ("Preinscriptos", "preinscriptos"), ("Aspirantes", "aspirantes"),
                        ("Preins_sin_cursos_teams", "pre_candidates"), ("Preinscriptos_con_cursos", "pre_enrolled"),
                        ("Ingresantes", "ingresantes"), ("Candidatos_a_revision", "candidates"), ("Datos_incompletos", "incomplete")):
        sources.add_sheet(wb, title, headers, [row(p) for p in filter_people(people, view)], {5:38, 14:75})
    without_courses_teams = [p for p in people if _moodle_without_courses(p) and _teams_without_memberships(p)]
    sources.add_sheet(wb, "Revision_sin_cursos_ni_equipos", headers, [row(p) for p in without_courses_teams], {5:38, 14:75})
    sources.add_sheet(wb, "Actividad_o_revision_180", headers,
                      [row(p) for p in without_courses_teams if p["microsoft_estado"] == "encontrado" and p["actividad"] != ACTIVITY_NONE_180], {5:38, 14:75})
    sources.add_sheet(wb, "Bajas_preinscriptos", headers, [row(p) for p in enabled_preinscriptos], {5:38, 14:75})
    with closing(read_snapshot(run, settings)) as snap:
        sources.add_sheet(wb, "Inscripciones_SIAL", ["DNI", "Condicion", "Carrera ID", "Carrera", "Modalidad", "Comisiones", "Inscripcion", "Consulta"],
                          snap.execute("SELECT * FROM sial_inscripciones"), {4:65})
        sources.add_sheet(wb, "Cursos_Moodle", ["DNI", "Curso ID", "Codigo", "Curso", "Visible", "Inicio", "Fin", "Ultimo acceso", "Consulta"],
                          snap.execute("SELECT * FROM moodle_cursos"), {4:65})
        sources.add_sheet(wb, "Equipos_Teams", ["DNI", "Equipo ID", "Equipo", "Archivado", "Consulta"], snap.execute(
            "SELECT t.dni,t.equipo_id,e.nombre,e.archivado,t.consultado FROM teams_pertenencias t JOIN equipos e USING(equipo_id)"), {3:65})
        sources.add_sheet(wb, "Fuentes", ["Informe", "Endpoint", "Estado", "Consulta", "Actualizacion", "Dias", "Filas totales", "Filas coincidentes", "SHA256", "Columnas", "Detalle"], snap.execute("SELECT * FROM informes_uso"))
        sources.add_sheet(wb, "Incidencias", ["ID", "Origen", "DNI", "Codigo", "Detalle", "Consulta"], snap.execute("SELECT * FROM incidencias"), {5:70})
        for label in reports.REPORTS:
            report = snap.execute("SELECT columnas_json FROM informes_uso WHERE informe=?", (label,)).fetchone()
            fields = json.loads(report[0]) if report and report[0] else []
            rows = [[r["dni"], r["identidad"], *[json.loads(r["datos_json"]).get(f) for f in fields]]
                    for r in snap.execute("SELECT * FROM uso_filas WHERE informe=?", (label,))]
            sources.add_sheet(wb, "Uso_" + label, ["DNI", "Cruce", *fields], rows)
    sources.add_sheet(wb, "Bitacora_bajas", ["Lote", "DNI", "Moodle", "Microsoft", "Detalle", "Verificado", "Responsable"], deletions, {5:80})
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def fresh_states(run, settings):
    data = sources.sial_lists(run.campaign_start.replace("-", "") + "0000", settings, lambda *a, **k: None)
    people = {}
    for state, rows in data.items():
        for row in rows:
            entry = people.setdefault(str(row["nrodoc"]).strip(), {"states": set(), "careers": []})
            entry["states"].add(state)
            entry["careers"].extend(row.get("carreras") or [])
    return people


def verify_sial(p, states, run):
    current = states.get(p["dni"])
    if not current or current["states"] != {p["estados_sial"]} or not in_campaign(current["careers"], run):
        raise ValueError("SIAL cambio de condicion/campana o no confirma al alumno; baja bloqueada.")


def check_identity(result, expected_id, upn, system):
    if result.status == "missing":
        return None
    if result.status not in ("exists", "ok") or not isinstance(result.data, dict):
        raise RuntimeError(f"{system}: no se pudo verificar la identidad ({result.status}).")
    data = result.data
    actual_upn = data.get("username" if system == "Moodle" else "userPrincipalName", "")
    if not expected_id or str(data.get("id")) != str(expected_id) or actual_upn.casefold() != upn.casefold():
        raise ValueError(f"{system}: cambio de identidad o cuenta recreada; generar una nueva auditoria.")
    return data


def fresh_usage(people, settings):
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE metadatos(clave TEXT PRIMARY KEY, valor TEXT)")
        db.execute("CREATE TABLE personas(dni TEXT PRIMARY KEY,upn TEXT,microsoft_id TEXT,microsoft_estado TEXT)")
        db.executemany("INSERT INTO personas VALUES (?,?,?,?)", [(p["dni"], p["upn"], p["microsoft_id"], p["microsoft_estado"]) for p in people])
        reports.init_reports(db)
        reports.collect(db, settings, lambda *a, **k: None)
        rows = {}
        for r in db.execute("SELECT * FROM uso_filas"):
            rows.setdefault((r["dni"], r["informe"]), []).append(json.loads(r["datos_json"]))
        return rows


def verify_inactivity(p, usage, today=None):
    today = today or date.today()
    rows = [usage.get((p["dni"], label), []) for label in reports.REPORTS]
    activity, _, _ = reports.classify(p, *rows)
    if activity != ACTIVITY_NONE_180:
        if activity == ACTIVITY_RECENT:
            raise ValueError("Microsoft 365/Teams registra actividad dentro de los ultimos 180 dias; baja bloqueada.")
        if activity == ACTIVITY_HISTORICAL:
            raise ValueError("Microsoft 365/Teams informa actividad historica; revision manual necesaria.")
        raise ValueError("Microsoft: no se pudo confirmar ausencia de actividad durante 180 dias; baja bloqueada.")
    for group in rows:
        refreshed = reports.parse_date(group[0]["Report Refresh Date"])
        if not refreshed or not 0 <= (today - refreshed).days <= 7:
            raise ValueError("Microsoft: informe desactualizado; baja bloqueada.")
    age_block = _account_age_block_reason(p, today)
    if age_block:
        raise ValueError(age_block)


def sync_local(db, p, moodle_status, microsoft_status):
    user = db.query(ManagedUser).filter(ManagedUser.upn == p["upn"]).first()
    if moodle_status in SUCCESS:
        db.query(MatriculationState).filter(MatriculationState.upn == p["upn"]).update({"active": False})
        if user:
            db.query(ManagedEnrollment).filter(ManagedEnrollment.user_id == user.id).update({"active": False})
            user.moodle_user_id = ""
    if user and microsoft_status in SUCCESS:
        user.azure_user_id = ""
    if user and moodle_status in SUCCESS and microsoft_status in SUCCESS:
        user.active = False


def deletion_job(deletion_id, owner, session_factory=SessionLocal, settings=None):
    settings = settings or get_settings()
    with session_factory() as db:
        batch = db.get(AuditDeletion, deletion_id)
        run = db.get(AuditRun, batch.run_id)
        reader = None
        try:
            heartbeat(db, owner)
            if settings.crm_dry_run:
                raise ValueError("Modo simulacion: las bajas reales estan deshabilitadas.")
            if environment_key(settings) != run.environment or not campaign_closed(run):
                raise ValueError("Campana abierta o servicios cambiaron; generar una nueva auditoria.")
            batch.status = "running"
            batch.message = "Verificando SIAL, Moodle, Teams y actividad antes de las bajas"
            db.commit()
            rows = db.query(AuditDeletionRow).filter_by(deletion_id=batch.id).order_by(AuditDeletionRow.id).all()
            people = [json.loads(r.snapshot_json) for r in rows]
            states = fresh_states(run, settings)
            states_at = datetime.utcnow()
            usage_people = [
                p for p in people
                if p.get("candidate") in {"preinscripto", "aspirante"}
                and p.get("microsoft_estado") == "encontrado"
            ]
            usage = fresh_usage(usage_people, settings) if usage_people else {}
            usage_at = datetime.utcnow()
            moodle, graph = MoodleClient(settings), GraphClient(settings)
            for index, (row, p) in enumerate(zip(rows, people), 1):
                if row.moodle_status in SUCCESS and row.microsoft_status in SUCCESS:
                    continue
                try:
                    heartbeat(db, owner)
                    candidate, reason = eligibility(p, run)
                    if not candidate or candidate != p["candidate"]:
                        raise ValueError("El lote no cumple el criterio vigente: " + reason)
                    if datetime.utcnow() - states_at > timedelta(minutes=2):
                        states = fresh_states(run, settings)
                        states_at = datetime.utcnow()
                    verify_sial(p, states, run)
                    mu = check_identity(moodle.get_user_by_username(p["upn"]), p["moodle_id"], p["upn"], "Moodle")
                    gu = check_identity(graph.get_user(p["upn"]), p["microsoft_id"], p["upn"], "Microsoft")
                    if (mu and row.moodle_status in SUCCESS) or (gu and row.microsoft_status in SUCCESS):
                        raise ValueError("Una cuenta eliminada reaparecio; revision manual necesaria.")
                    # Check every Moodle course before either account is removed, including old previews.
                    if mu:
                        courses = moodle.get_user_courses(mu["id"])
                        if courses.status != "ok" or courses.data != []:
                            raise ValueError("Moodle: tiene cursos o no se pudo comprobar; baja bloqueada en ambos sistemas.")
                    if gu:
                        if datetime.utcnow() - usage_at > timedelta(minutes=15):
                            usage = fresh_usage(usage_people, settings)
                            usage_at = datetime.utcnow()
                        verify_inactivity(p, usage)
                        if reader is None:
                            reader = sources.GraphReadOnly(settings)
                        response = reader.get(f"/users/{quote(gu['id'])}/joinedTeams")
                        if response.status_code != 200 or reader.list_pages(response.json()) != []:
                            raise ValueError("Microsoft: tiene equipos Teams directos o no se pudo comprobar; baja bloqueada.")
                    row.checked_at = datetime.utcnow()
                    row.message = "Identidad y condicion verificadas."
                    db.commit()
                    for system, remote, client in (("moodle", mu, moodle), ("microsoft", gu, graph)):
                        field = system + "_status"
                        if getattr(row, field) in SUCCESS:
                            continue
                        heartbeat(db, owner)
                        if remote is None:
                            status = "missing"
                        else:
                            try:
                                result = client.delete_user_by_id(remote["id"]) if system == "moodle" else client.delete_user(remote["id"])
                                status = result.status if result.status in SUCCESS else "error"
                                if status == "error":
                                    row.message += f" {system}: " + safe_error(RuntimeError(result.message or result.status), settings)
                            except Exception as exc:
                                status = "error"
                                row.message += f" {system}: " + safe_error(exc, settings)
                        setattr(row, field, status)
                        sync_local(db, p, row.moodle_status, row.microsoft_status)
                        db.add(OperationLog(actor=batch.actor, action="audit_delete_" + system, status=status,
                            detail=f"Lote #{batch.id}, auditoria #{run.id}, DNI {p['dni']}: {row.message}"))
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    row.message = safe_error(exc, settings)
                    db.add(OperationLog(actor=batch.actor, action="audit_delete_blocked", status="blocked",
                        detail=f"Lote #{batch.id}, DNI {p['dni']}: {row.message}"))
                    db.commit()
                batch.message = f"Verificados {index}/{len(rows)}."
                db.commit()
            remaining = sum(r.moodle_status not in SUCCESS or r.microsoft_status not in SUCCESS for r in rows)
            batch.status = "partial" if remaining else "completed"
            batch.message = f"Lote finalizado: {len(rows) - remaining}/{len(rows)} resueltos; {remaining} bloqueados o con error."
        except Exception as exc:
            db.rollback()
            batch = db.get(AuditDeletion, deletion_id)
            batch.status = "error"
            batch.message = safe_error(exc, settings)
        finally:
            if reader:
                reader.client.close()
            batch.finished_at = datetime.utcnow()
            db.add(OperationLog(actor=batch.actor, action="audit_delete_batch", status=batch.status,
                                detail=f"Lote #{batch.id}: {batch.message}"))
            db.commit()
            release_lease(db, owner)
