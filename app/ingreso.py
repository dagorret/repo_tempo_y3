"""Campaign-scoped SIAL data and admission routing, with no external writes."""
from datetime import date, datetime, timedelta
import hashlib
import json
import re
import unicodedata

from sqlalchemy.dialects.sqlite import insert

from app.auditing import environment_key, registration_date
from app.models import IngresoStudent, IngresoJob, IngresoTask, IngresoLease


KINDS = {"sync": "Consulta SIAL", "microsoft": "Microsoft / Teams", "moodle": "Moodle"}
STATES = {"queued": "En cola", "running": "En proceso", "preview": "Vista previa", "ready": "Pendiente",
          "pending": "Sin verificar", "done": "Completado", "exists": "Cuenta existente", "created": "Cuenta creada",
          "missing": "No existe", "completed": "Completado", "partial": "Con incidencias", "error": "Error",
          "blocked": "Revisar", "interrupted": "Interrumpido", "current": "Vigente", "not_seen": "No visto en ultima consulta",
          "sent": "Enviado", "not_needed": "No corresponde", "licensed": "Asignada", "sending": "Envio por verificar",
          "pending_sync": "Pendiente de actualizar periodo"}
SIAL_STATES = ("preinscriptos", "aspirantes", "ingresantes")


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def normalized(value):
    return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper().split())


def default_rules():
    return {"careers": {
        "2": {"name": "Licenciatura en Economia", "kind": "degree", "courses": ["I3", "I4", "I5", "I6"]},
        "3": {"name": "Contador Publico", "kind": "degree", "courses": ["I3", "I4", "I5", "I6"]},
        "4": {"name": "Licenciatura en Administracion", "kind": "degree", "courses": ["I3", "I4", "I5", "I6"]},
        "7": {"name": "Tecnicatura en Gestion Agropecuaria y Agroalimentaria", "kind": "exclusive", "courses": ["I7"]},
        "8": {"name": "Tecnicatura Universitaria en Gestion Empresarial", "kind": "exclusive", "courses": ["I8"]}},
        "accounting": {"P": {f"COMISION {n}": "" for n in range(1, 6)},
                       "D": {f"COMISION 0{n}": "" for n in range(1, 4)}}}


def validate_rules(rules):
    if not isinstance(rules, dict) or not isinstance(rules.get("careers"), dict) or not rules["careers"]:
        raise ValueError("Configurar al menos una carrera.")
    for key, rule in rules["careers"].items():
        if not key.isdigit() or rule.get("kind") not in ("degree", "exclusive") or not rule.get("name", "").strip():
            raise ValueError("Carrera, ID o tipo de distribucion invalido.")
        courses = rule.get("courses")
        if not isinstance(courses, list) or not courses or len(courses) > 20 or any(
                not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", c) for c in courses):
            raise ValueError("Indicar nombres cortos de curso validos, separados por coma.")
        if rule["kind"] == "degree" and set(courses) & {"I1", "I2"}:
            raise ValueError("I1/I2 se asignan por comision, no como cursos fijos de grado.")
    if set(rules.get("accounting", {})) != {"P", "D"}:
        raise ValueError("Configurar Contabilidad para P y D.")
    for group, mapping in rules["accounting"].items():
        if not isinstance(mapping, dict):
            raise ValueError("Mapa de comisiones invalido.")
        for commission, course in mapping.items():
            if not re.fullmatch(r"COMISION (?:[1-5]|0[1-3])", commission) or course not in ("", "I1", "I2"):
                raise ValueError("Contabilidad admite las comisiones indicadas y destinos I1/I2.")
            expected = "D" if commission.split()[-1].startswith("0") else "P"
            if expected != group:
                raise ValueError("La modalidad de la comision es incorrecta.")


def desired_courses(registrations, rules):
    targets, issues = set(), []
    if not registrations:
        return [], ["Sin inscripciones de esta campana."]
    per_career = {}
    for item in registrations:
        signature = (item["modality"], tuple(item["commissions"]))
        previous = per_career.setdefault(item["career_id"], signature)
        if previous != signature:
            issues.append("Inscripciones contradictorias para la misma carrera " + item["career_id"] + "; actualizar SIAL antes de matricular.")
    for item in registrations:
        rule = rules["careers"].get(item["career_id"])
        if not rule:
            issues.append("Carrera sin configurar: " + item["career_id"] + " " + item["career"])
            continue
        group = {"1": "P", "2": "D"}.get(item["modality"])
        if not group:
            issues.append("Modalidad sin reconocer en " + item["career"])
            continue
        targets.update((course, group) for course in rule["courses"])
        if rule["kind"] == "exclusive":
            continue
        # SIAL may also attach the student's technical-degree commission to a degree registration.
        commissions = [c for c in item["commissions"] if c not in ("COMISION TGE", "COMISION TGAA")]
        if not commissions:
            issues.append("Falta comision de Contabilidad en " + item["career"])
            continue
        choices = set()
        for commission in commissions:
            target = rules["accounting"][group].get(commission)
            if not target:
                issues.append(f"Contabilidad pendiente: {commission}, grupo {group}.")
            else:
                choices.add(target)
        if len(choices) > 1:
            issues.append("Comisiones de una misma inscripcion apuntan a I1 e I2; revisar.")
        else:
            targets.update((course, group) for course in choices)
    if {course for course, group in targets} >= {"I1", "I2"}:
        targets = {(course, group) for course, group in targets if course not in ("I1", "I2")}
        issues.append("Contabilidad pendiente: las inscripciones apuntan a I1 e I2. Revisar comisiones; no se asignaran ambos cursos.")
    return [{"course": c, "group": g} for c, g in sorted(targets)], sorted(set(issues))


def normalize_sial(data, campaign):
    if set(data) != set(SIAL_STATES):
        raise ValueError("Consulta incompleta: se requieren los tres estados SIAL.")
    start, end = date.fromisoformat(campaign.start_date), date.fromisoformat(campaign.end_date)
    people = {}
    for state in SIAL_STATES:
        if not isinstance(data[state], list):
            raise ValueError("Listado SIAL invalido: " + state)
        for person in data[state]:
            dni = str(person.get("nrodoc", "")).strip()
            if not dni.isdigit() or not isinstance(person.get("carreras"), list):
                raise ValueError("Identidad o carreras SIAL incompletas; no se reemplazaran datos locales.")
            for c in person["carreras"]:
                registered = registration_date(c.get("fecha_inscri"))
                if registered is None:
                    raise ValueError("Fecha de inscripcion SIAL sin reconocer; revisar la consulta.")
                if not start <= registered <= end:
                    continue
                commissions = c.get("comisiones") or []
                if not isinstance(commissions, list) or any(not isinstance(x, dict) or not x.get("nombre_comision") for x in commissions):
                    raise ValueError("Comisiones SIAL con estructura inesperada.")
                item = {"career_id": str(c.get("id_carrera", "")), "career": str(c.get("nombre_carrera", "")),
                        "date": registered.isoformat(), "modality": str(c.get("modalidad", "")),
                        "commissions": sorted(set(normalized(x["nombre_comision"]) for x in commissions))}
                key = dump(item)
                current = people.setdefault(dni, {}).setdefault(key, {**item, "states": []})
                if state not in current["states"]:
                    current["states"].append(state)
    return {dni: [items[k] for k in sorted(items)] for dni, items in people.items()}


def persist_sial(db, campaign, people, domain):
    existing = {p.dni: p for p in db.query(IngresoStudent).filter_by(campaign_id=campaign.id).all()}
    for student in existing.values():
        student.source_status = "not_seen"
    now = datetime.utcnow()
    for dni, registrations in people.items():
        student = existing.get(dni)
        if student is None:
            student = IngresoStudent(campaign_id=campaign.id, dni=dni, upn=f"a{dni}@{domain}".lower())
            db.add(student)
        student.registrations_json = dump(registrations)
        student.source_status = "current"
        student.last_seen_at = now
    db.commit()


def student_snapshot(student, campaign, kind):
    data = {"dni": student.dni, "upn": student.upn, "first_name": student.first_name, "last_name": student.last_name,
            "email": student.email, "personal_status": student.personal_status, "source_status": student.source_status,
            "campaign_period": [campaign.start_date, campaign.end_date],
            "registrations": json.loads(student.registrations_json)}
    if kind == "moodle":
        data["rules"] = json.loads(campaign.rules_json)
    return data


def fingerprint(student, campaign, kind):
    return hashlib.sha256(dump(student_snapshot(student, campaign, kind)).encode()).hexdigest()


def check_writes(settings):
    if not settings.ingreso_allow_writes or settings.crm_dry_run:
        raise ValueError("Ingreso esta en modo preparacion: no se crean cuentas, matriculas ni se envian correos.")
    if len(settings.crm_secret_key) < 32:
        raise ValueError("Configurar una clave CRM segura de al menos 32 caracteres antes de habilitar Ingreso.")


def acquire(db, owner):
    now = datetime.utcnow()
    db.execute(insert(IngresoLease).values(id=1, owner="", expires_at=now).on_conflict_do_nothing())
    changed = db.query(IngresoLease).filter(IngresoLease.id == 1, IngresoLease.expires_at <= now).update(
        {"owner": owner, "expires_at": now + timedelta(minutes=30)})
    if not changed:
        db.rollback()
        raise ValueError("Hay una tarea de Ingreso en proceso. Esperar a que termine.")


def heartbeat(db, owner):
    now = datetime.utcnow()
    if not db.query(IngresoLease).filter(IngresoLease.id == 1, IngresoLease.owner == owner,
            IngresoLease.expires_at > now).update({"expires_at": now + timedelta(minutes=30)}):
        raise RuntimeError("La tarea perdio su bloqueo; no continuara automaticamente.")
    db.commit()


def release(db, owner):
    db.query(IngresoLease).filter(IngresoLease.owner == owner).update({"expires_at": datetime.utcnow()})
    db.commit()


def recover(db):
    lease = db.get(IngresoLease, 1)
    if lease and lease.expires_at > datetime.utcnow():
        return
    jobs = db.query(IngresoJob).filter(IngresoJob.status.in_(["queued", "running", "queued_execute"])).all()
    for job in jobs:
        job.status = "interrupted"
        job.message = "Tarea interrumpida. Preparar otra vista previa; no se reanudan operaciones reales automaticamente."
        db.query(IngresoTask).filter_by(job_id=job.id, status="running").update({"status": "error", "message": job.message})
    db.commit()
