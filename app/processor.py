from sqlalchemy.orm import Session

from app.config import Settings
from app.importers import ImportedRow
from app.models import ImportedPerson
from app.security import generate_temporary_password
from app.services.graph import GraphClient
from app.services.mailer import Mailer
from app.services.moodle import MoodleClient


def process_person(
    db: Session,
    person: ImportedPerson,
    source: ImportedRow,
    settings: Settings,
    graph: GraphClient,
    moodle: MoodleClient,
    mailer: Mailer,
    course_shortnames: list[str],
    email_subject: str = "",
    email_message: str = "",
    course_lookup: dict[str, dict] | None = None,
    moodle_group_name: str = "",
    group_ids_by_course: dict[str, int] | None = None,
    moodle_group_mode: str = "tolerant",
) -> ImportedPerson:
    messages: list[str] = []
    course_lookup = course_lookup or {}
    group_ids_by_course = group_ids_by_course or {}
    strict_group_mode = str(moodle_group_mode or "tolerant").strip().lower() == "strict"

    if settings.crm_dry_run:
        person.status = "dry_run"
        person.azure_status = "dry_run"
        person.moodle_status = "dry_run"
        person.email_status = "dry_run"
        person.message = "Simulacion: no se modifico Azure, Moodle ni SMTP."
        db.commit()
        return person

    password = generate_temporary_password()

    existing = graph.get_user(person.upn)
    if existing.status == "exists":
        person.azure_status = "exists"
        if isinstance(existing.data, dict):
            person.azure_user_id = str(existing.data.get("id", "") or "")
        messages.append("Azure: usuario existente.")
    elif existing.status in {"missing", "skipped"}:
        created = graph.create_user(person.upn, source.first_name, source.last_name, password)
        person.azure_status = created.status
        messages.append(f"Azure: {created.message}")
        if created.status == "created":
            created_id = ""
            if isinstance(created.data, dict):
                created_id = str(created.data.get("id", "")).strip()
                person.azure_user_id = created_id
            license_target = created_id or person.upn
            licensed = graph.assign_license(license_target, person.person_type)
            person.azure_status = licensed.status
            messages.append(f"Licencia: {licensed.message}")
    else:
        person.azure_status = "error"
        messages.append(f"Azure: {existing.message}")

    if course_shortnames:
        moodle_user = moodle.get_user_by_username(person.upn.lower())
        if moodle_user.status == "missing":
            moodle_user = moodle.create_user(
                person.upn,
                source.first_name,
                source.last_name,
                source.email,
            )
        if moodle_user.status in {"ok", "created"} and isinstance(moodle_user.data, dict):
            person.moodle_user_id = str(moodle_user.data.get("id", "") or "")
            moodle_statuses: list[str] = []
            abort_row = False
            for course_shortname in course_shortnames:
                if abort_row:
                    break
                cached_course = course_lookup.get(course_shortname)
                if cached_course and str(cached_course.get("id", "")).isdigit():
                    course_id = int(cached_course["id"])
                    enrolment = moodle.enrol_user(
                        int(moodle_user.data["id"]),
                        course_id,
                        person.person_type,
                    )
                    moodle_statuses.append(enrolment.status)
                    messages.append(f"Moodle {course_shortname}: {enrolment.message}")
                    if enrolment.status == "enrolled" and moodle_group_name:
                        group_id = group_ids_by_course.get(course_shortname, 0)
                        if group_id > 0:
                            grouped = moodle.add_group_member(int(group_id), int(moodle_user.data["id"]))
                            if grouped.status == "added":
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: {grouped.message}")
                            else:
                                if strict_group_mode:
                                    moodle_statuses.append("error")
                                    messages.append(f"Grupo {course_shortname}/{moodle_group_name}: {grouped.message}")
                                    abort_row = True
                                else:
                                    messages.append(f"Grupo {course_shortname}/{moodle_group_name}: warning - {grouped.message}")
                        else:
                            if strict_group_mode:
                                moodle_statuses.append("error")
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: no disponible.")
                                abort_row = True
                            else:
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: warning - no disponible.")
                    continue

                course = moodle.get_course_by_shortname(course_shortname)
                if course.status == "ok" and isinstance(course.data, dict) and str(course.data.get("id", "")).isdigit():
                    course_lookup[course_shortname] = course.data
                    enrolment = moodle.enrol_user(
                        int(moodle_user.data["id"]),
                        int(course.data["id"]),
                        person.person_type,
                    )
                    moodle_statuses.append(enrolment.status)
                    messages.append(f"Moodle {course_shortname}: {enrolment.message}")
                    if enrolment.status == "enrolled" and moodle_group_name:
                        group_id = group_ids_by_course.get(course_shortname, 0)
                        if group_id > 0:
                            grouped = moodle.add_group_member(int(group_id), int(moodle_user.data["id"]))
                            if grouped.status == "added":
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: {grouped.message}")
                            else:
                                if strict_group_mode:
                                    moodle_statuses.append("error")
                                    messages.append(f"Grupo {course_shortname}/{moodle_group_name}: {grouped.message}")
                                    abort_row = True
                                else:
                                    messages.append(f"Grupo {course_shortname}/{moodle_group_name}: warning - {grouped.message}")
                        else:
                            if strict_group_mode:
                                moodle_statuses.append("error")
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: no disponible.")
                                abort_row = True
                            else:
                                messages.append(f"Grupo {course_shortname}/{moodle_group_name}: warning - no disponible.")
                else:
                    moodle_statuses.append(course.status)
                    messages.append(f"Moodle {course_shortname}: {course.message}")

            if any(status == "error" for status in moodle_statuses):
                person.moodle_status = "error"
            elif any(status == "enrolled" for status in moodle_statuses):
                person.moodle_status = "enrolled"
            elif all(status == "missing" for status in moodle_statuses):
                person.moodle_status = "missing"
            else:
                person.moodle_status = moodle_statuses[0] if moodle_statuses else "skipped"
        else:
            person.moodle_status = moodle_user.status
            messages.append(f"Moodle: {moodle_user.message}")
    else:
        person.moodle_status = "skipped"
        messages.append("Moodle: sin curso informado.")

    if person.azure_status in {"created", "licensed"}:
        mail = mailer.send_credentials(
            source.email,
            person.upn,
            password,
            full_name=source.full_name,
            custom_subject=email_subject,
            custom_message=email_message,
        )
        person.email_status = mail.status
        messages.append(f"Correo: {mail.message}")
    else:
        person.email_status = "skipped"
        messages.append("Correo: no se envia porque Azure no creo una cuenta nueva.")

    if any(status == "error" for status in [person.azure_status, person.moodle_status, person.email_status]):
        person.status = "error"
    elif person.azure_status == "exists":
        person.status = "exists"
    else:
        person.status = "processed"

    person.message = " ".join(messages)
    db.commit()
    return person
