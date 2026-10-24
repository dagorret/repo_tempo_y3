from sqlalchemy.orm import Session

from app.importers import ImportedRow
from app.models import ManagedEnrollment, ManagedUser


def upsert_managed_user(
    db: Session,
    row: ImportedRow,
    person_type: str,
    azure_user_id: str = "",
    moodle_user_id: str = "",
) -> ManagedUser:
    user = db.query(ManagedUser).filter(ManagedUser.upn == row.upn).first()
    if not user:
        user = ManagedUser(
            person_type=person_type,
            document=row.document,
            first_name=row.first_name,
            last_name=row.last_name,
            full_name=row.full_name,
            upn=row.upn,
            contact_email=row.email,
            azure_user_id=azure_user_id or "",
            moodle_user_id=moodle_user_id or "",
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    user.person_type = person_type
    user.document = row.document
    user.first_name = row.first_name
    user.last_name = row.last_name
    user.full_name = row.full_name
    user.contact_email = row.email
    user.active = True
    if azure_user_id:
        user.azure_user_id = azure_user_id
    if moodle_user_id:
        user.moodle_user_id = moodle_user_id
    db.commit()
    db.refresh(user)
    return user


def upsert_managed_enrollment(
    db: Session,
    user: ManagedUser,
    course_shortname: str,
    role: str,
    course_name: str = "",
    moodle_course_id: str = "",
) -> ManagedEnrollment | None:
    if not course_shortname:
        return None
    enrollment = (
        db.query(ManagedEnrollment)
        .filter(
            ManagedEnrollment.user_id == user.id,
            ManagedEnrollment.course_shortname == course_shortname,
        )
        .first()
    )
    if not enrollment:
        enrollment = ManagedEnrollment(
            user_id=user.id,
            course_shortname=course_shortname,
            course_name=course_name,
            moodle_course_id=moodle_course_id,
            role=role,
            active=True,
        )
        db.add(enrollment)
    else:
        enrollment.role = role
        if course_name:
            enrollment.course_name = course_name
        if moodle_course_id:
            enrollment.moodle_course_id = moodle_course_id
        enrollment.active = True
    db.commit()
    db.refresh(enrollment)
    return enrollment
