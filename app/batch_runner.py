from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.importers import ImportedRow
from app.models import ImportBatch, ImportedPerson, OperationLog
from app.processor import process_person
from app.services.graph import GraphClient
from app.services.mailer import Mailer
from app.services.moodle import MoodleClient
from app.user_registry import upsert_managed_enrollment, upsert_managed_user


def _parse_course_shortnames(raw_value: str) -> list[str]:
    if not raw_value:
        return []
    values = [part.strip() for part in raw_value.replace(";", ",").split(",")]
    return [value for value in values if value]


def _build_row(person: ImportedPerson) -> ImportedRow:
    return ImportedRow(
        document=person.document,
        first_name=person.first_name,
        last_name=person.last_name,
        full_name=person.full_name or f"{person.first_name} {person.last_name}".strip(),
        email=person.email,
        upn=person.upn,
    )


def _load_course_info(moodle: MoodleClient, course_shortnames: list[str]) -> dict[str, dict[str, str]]:
    course_info_map: dict[str, dict[str, str]] = {}
    for course_code in course_shortnames:
        course = moodle.get_course_by_shortname(course_code)
        if course.status == "ok" and isinstance(course.data, dict):
            course_info_map[course_code] = {
                "course_name": str(
                    course.data.get("fullname")
                    or course.data.get("displayname")
                    or course.data.get("shortname")
                    or ""
                ),
                "moodle_course_id": str(course.data.get("id", "") or ""),
            }
    return course_info_map


def _resolve_group_ids(
    moodle: MoodleClient,
    course_info_map: dict[str, dict[str, str]],
    group_name: str,
    autocreate: bool,
) -> tuple[dict[str, int], list[str]]:
    resolved: dict[str, int] = {}
    issues: list[str] = []
    if not group_name.strip():
        return resolved, issues

    for course_shortname, info in course_info_map.items():
        course_id_raw = str(info.get("moodle_course_id", "") or "")
        if not course_id_raw.isdigit():
            issues.append(f"{course_shortname}: curso sin id Moodle.")
            continue
        course_id = int(course_id_raw)
        group = moodle.get_group_in_course_by_name(course_id, group_name)
        if group.status == "ok" and isinstance(group.data, dict):
            group_id = int(group.data.get("id", 0))
            if group_id > 0:
                resolved[course_shortname] = group_id
                continue

        if group.status == "missing" and autocreate:
            created = moodle.create_group(course_id, group_name)
            if created.status in {"created", "ok"} and isinstance(created.data, dict):
                group_id = int(created.data.get("id", 0))
                if group_id > 0:
                    resolved[course_shortname] = group_id
                    continue
            issues.append(f"{course_shortname}: no se pudo crear grupo '{group_name}' ({created.message}).")
            continue

        issues.append(f"{course_shortname}: grupo '{group_name}' no disponible ({group.message}).")

    return resolved, issues


def _increment_batch_progress(db: Session, batch_id: int, success: bool) -> None:
    db.execute(
        update(ImportBatch)
        .where(ImportBatch.id == batch_id)
        .values(
            processed_rows=ImportBatch.processed_rows + 1,
            success_rows=ImportBatch.success_rows + (1 if success else 0),
            error_rows=ImportBatch.error_rows + (0 if success else 1),
        )
    )
    db.commit()


def _set_batch_status(batch_id: int, status: str, actor: str, detail: str = "") -> None:
    db = SessionLocal()
    try:
        batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
        if not batch:
            return
        batch.run_status = status
        if status == "running":
            batch.started_at = datetime.utcnow()
            batch.finished_at = None
        if status in {"completed", "failed"}:
            batch.finished_at = datetime.utcnow()
        db.add(
            OperationLog(
                actor=actor,
                action="import_batch",
                status="ok" if status == "completed" else ("error" if status == "failed" else "running"),
                detail=f"Importacion {batch_id}: {status}. {detail}".strip(),
            )
        )
        db.commit()
    finally:
        db.close()


def _process_person_worker(
    *,
    batch_id: int,
    person_id: int,
    settings: Settings,
    person_type: str,
    course_shortnames: list[str],
    course_info_map: dict[str, dict[str, str]],
    course_lookup: dict[str, dict],
    moodle_group_name: str,
    group_ids_by_course: dict[str, int],
    moodle_group_mode: str,
    email_subject: str,
    email_message: str,
) -> bool:
    db = SessionLocal()
    try:
        person = (
            db.query(ImportedPerson)
            .filter(ImportedPerson.id == person_id, ImportedPerson.batch_id == batch_id)
            .first()
        )
        if not person:
            return False

        row = _build_row(person)
        graph = GraphClient(settings)
        moodle = MoodleClient(settings)
        mailer = Mailer(settings)

        process_person(
            db,
            person,
            row,
            settings,
            graph,
            moodle,
            mailer,
            course_shortnames,
            email_subject=email_subject,
            email_message=email_message,
            course_lookup=course_lookup,
            moodle_group_name=moodle_group_name,
            group_ids_by_course=group_ids_by_course,
            moodle_group_mode=moodle_group_mode,
        )

        managed_user = upsert_managed_user(
            db,
            row,
            person_type=person_type,
            azure_user_id=person.azure_user_id,
            moodle_user_id=person.moodle_user_id,
        )
        for course_code in course_shortnames:
            info = course_info_map.get(course_code, {})
            upsert_managed_enrollment(
                db,
                managed_user,
                course_shortname=course_code,
                role=person_type,
                course_name=info.get("course_name", ""),
                moodle_course_id=info.get("moodle_course_id", ""),
            )

        db.refresh(person)
        return person.status != "error"
    except Exception as exc:
        person = (
            db.query(ImportedPerson)
            .filter(ImportedPerson.id == person_id, ImportedPerson.batch_id == batch_id)
            .first()
        )
        if person:
            person.status = "error"
            person.azure_status = "error"
            person.moodle_status = "error"
            person.email_status = "error"
            person.message = f"Error interno: {exc}"
            db.commit()
        return False
    finally:
        db.close()


def run_batch_in_background(batch_id: int, actor: str, settings: Settings) -> None:
    db = SessionLocal()
    try:
        batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
        if not batch:
            return
        people = (
            db.query(ImportedPerson.id)
            .filter(ImportedPerson.batch_id == batch_id)
            .order_by(ImportedPerson.id.asc())
            .all()
        )
        person_ids = [row[0] for row in people]
        course_shortnames = _parse_course_shortnames(batch.course_shortname)
        moodle_group_name = (batch.moodle_group_name or "").strip()
        moodle_group_autocreate = bool(batch.moodle_group_autocreate)
        moodle_group_mode = (
            "strict" if str(batch.moodle_group_mode or "tolerant").strip().lower() == "strict" else "tolerant"
        )
        person_type = batch.person_type
        email_subject = batch.email_subject
        email_message = batch.email_message
    finally:
        db.close()

    if not person_ids:
        _set_batch_status(batch_id, "failed", actor, "Sin filas para procesar.")
        return

    _set_batch_status(batch_id, "running", actor, f"Filas: {len(person_ids)}")
    moodle = MoodleClient(settings)
    course_info_map = _load_course_info(moodle, course_shortnames)
    group_ids_by_course, group_issues = _resolve_group_ids(
        moodle,
        course_info_map,
        moodle_group_name,
        moodle_group_autocreate,
    )
    if group_issues:
        _set_batch_status(batch_id, "running", actor, " ".join(group_issues))
    course_lookup = {
        shortname: {"id": int(info["moodle_course_id"])}
        for shortname, info in course_info_map.items()
        if str(info.get("moodle_course_id", "")).isdigit()
    }

    workers = max(1, min(settings.crm_batch_workers, 20))
    futures = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-worker") as executor:
        for person_id in person_ids:
            futures.append(
                executor.submit(
                    _process_person_worker,
                    batch_id=batch_id,
                    person_id=person_id,
                    settings=settings,
                    person_type=person_type,
                    course_shortnames=course_shortnames,
                    course_info_map=course_info_map,
                    course_lookup=course_lookup.copy(),
                    moodle_group_name=moodle_group_name,
                    group_ids_by_course=group_ids_by_course,
                    moodle_group_mode=moodle_group_mode,
                    email_subject=email_subject,
                    email_message=email_message,
                )
            )

        for future in as_completed(futures):
            success = False
            try:
                success = future.result()
            except Exception:
                success = False

            progress_db = SessionLocal()
            try:
                _increment_batch_progress(progress_db, batch_id, success)
            finally:
                progress_db.close()

    final_db = SessionLocal()
    try:
        batch = final_db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
        if not batch:
            return
        if batch.error_rows > 0:
            _set_batch_status(batch_id, "completed", actor, f"Finalizado con {batch.error_rows} errores.")
        else:
            _set_batch_status(batch_id, "completed", actor, "Finalizado sin errores.")
    finally:
        final_db.close()
