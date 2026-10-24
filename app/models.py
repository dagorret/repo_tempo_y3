from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class LocalUser(Base):
    __tablename__ = "local_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default="operator")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    person_type: Mapped[str] = mapped_column(String(30))
    course_shortname: Mapped[str] = mapped_column(String(120), default="")
    moodle_group_name: Mapped[str] = mapped_column(String(120), default="")
    moodle_group_autocreate: Mapped[bool] = mapped_column(Boolean, default=True)
    moodle_group_mode: Mapped[str] = mapped_column(String(20), default="tolerant")
    created_by: Mapped[str] = mapped_column(String(80))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    email_message: Mapped[str] = mapped_column(Text, default="")
    email_subject: Mapped[str] = mapped_column(String(255), default="")
    run_status: Mapped[str] = mapped_column(String(30), default="pending")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    success_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    people: Mapped[list["ImportedPerson"]] = relationship(back_populates="batch")


class ImportedPerson(Base):
    __tablename__ = "imported_people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    person_type: Mapped[str] = mapped_column(String(30))
    document: Mapped[str] = mapped_column(String(60), default="")
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str] = mapped_column(String(120))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    upn: Mapped[str] = mapped_column(String(255), index=True)
    azure_user_id: Mapped[str] = mapped_column(String(80), default="")
    moodle_user_id: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    azure_status: Mapped[str] = mapped_column(String(40), default="pending")
    moodle_status: Mapped[str] = mapped_column(String(40), default="pending")
    email_status: Mapped[str] = mapped_column(String(40), default="pending")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[ImportBatch] = relationship(back_populates="people")


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditRun(Base):
    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_key: Mapped[str] = mapped_column(String(40), unique=True)
    campaign_start: Mapped[str] = mapped_column(String(10))
    campaign_end: Mapped[str] = mapped_column(String(10), default="")
    environment: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="En cola")
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLease(Base):
    __tablename__ = "audit_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AuditDeletion(Base):
    __tablename__ = "audit_deletions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    actor: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="preview")
    message: Mapped[str] = mapped_column(Text, default="Vista previa; sin cambios externos.")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditDeletionRow(Base):
    __tablename__ = "audit_deletion_rows"
    __table_args__ = (UniqueConstraint("deletion_id", "dni", name="uq_audit_deletion_dni"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deletion_id: Mapped[int] = mapped_column(ForeignKey("audit_deletions.id"), index=True)
    dni: Mapped[str] = mapped_column(String(60))
    snapshot_json: Mapped[str] = mapped_column(Text)
    moodle_status: Mapped[str] = mapped_column(String(30), default="pending")
    microsoft_status: Mapped[str] = mapped_column(String(30), default="pending")
    message: Mapped[str] = mapped_column(Text, default="Pendiente de confirmacion")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IngresoCampaign(Base):
    __tablename__ = "ingreso_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    rules_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IngresoStudent(Base):
    __tablename__ = "ingreso_students"
    __table_args__ = (UniqueConstraint("campaign_id", "dni", name="uq_ingreso_campaign_dni"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("ingreso_campaigns.id"), index=True)
    dni: Mapped[str] = mapped_column(String(60), index=True)
    upn: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(120), default="")
    registrations_json: Mapped[str] = mapped_column(Text, default="[]")
    source_status: Mapped[str] = mapped_column(String(30), default="current")
    personal_status: Mapped[str] = mapped_column(String(30), default="pending")
    personal_message: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IngresoAccount(Base):
    __tablename__ = "ingreso_accounts"

    upn: Mapped[str] = mapped_column(String(255), primary_key=True)
    remote_id: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    license_status: Mapped[str] = mapped_column(String(30), default="pending")
    email_status: Mapped[str] = mapped_column(String(30), default="pending")
    email_to: Mapped[str] = mapped_column(String(255), default="")
    credential_encrypted: Mapped[str] = mapped_column(Text, default="")
    created_here: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IngresoPlacement(Base):
    __tablename__ = "ingreso_placements"
    __table_args__ = (UniqueConstraint("student_id", "course", "group_name", name="uq_ingreso_placement"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("ingreso_students.id"), index=True)
    course: Mapped[str] = mapped_column(String(120))
    group_name: Mapped[str] = mapped_column(String(2))
    moodle_user_id: Mapped[int] = mapped_column(Integer)
    moodle_course_id: Mapped[int] = mapped_column(Integer)
    moodle_group_id: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IngresoJob(Base):
    __tablename__ = "ingreso_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("ingreso_campaigns.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    environment: Mapped[str] = mapped_column(String(64))
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="En cola")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IngresoTask(Base):
    __tablename__ = "ingreso_tasks"
    __table_args__ = (UniqueConstraint("job_id", "student_id", name="uq_ingreso_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("ingreso_jobs.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("ingreso_students.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_json: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IngresoLease(Base):
    __tablename__ = "ingreso_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class CourseConversionRule(Base):
    __tablename__ = "course_conversion_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actual_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    old_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    category_rule: Mapped[str] = mapped_column(String(20), default="")
    registration_rule: Mapped[str] = mapped_column(String(40), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ManagedUser(Base):
    __tablename__ = "managed_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_type: Mapped[str] = mapped_column(String(30), index=True)
    document: Mapped[str] = mapped_column(String(60), default="", index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    full_name: Mapped[str] = mapped_column(String(255), default="")
    upn: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    azure_user_id: Mapped[str] = mapped_column(String(80), default="")
    moodle_user_id: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enrollments: Mapped[list["ManagedEnrollment"]] = relationship(back_populates="user")


class ManagedEnrollment(Base):
    __tablename__ = "managed_enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_shortname", name="uq_user_course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("managed_users.id"))
    course_shortname: Mapped[str] = mapped_column(String(120), index=True)
    course_name: Mapped[str] = mapped_column(String(255), default="")
    moodle_course_id: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(30), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped[ManagedUser] = relationship(back_populates="enrollments")


class MatriculationRun(Base):
    __tablename__ = "matriculation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    source_type: Mapped[str] = mapped_column(String(30), default="lincu", index=True)
    status: Mapped[str] = mapped_column(String(30), default="preview")
    total_source_rows: Mapped[int] = mapped_column(Integer, default=0)
    desired_rows: Mapped[int] = mapped_column(Integer, default=0)
    enroll_rows: Mapped[int] = mapped_column(Integer, default=0)
    group_change_rows: Mapped[int] = mapped_column(Integer, default=0)
    course_change_rows: Mapped[int] = mapped_column(Integer, default=0)
    unenroll_rows: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    course_alert_threshold: Mapped[int] = mapped_column(Integer, default=5)
    course_alert_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    execution_total_rows: Mapped[int] = mapped_column(Integer, default=0)
    execution_processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    verification_total_courses: Mapped[int] = mapped_column(Integer, default=0)
    verification_processed_courses: Mapped[int] = mapped_column(Integer, default=0)
    verification_scope: Mapped[str] = mapped_column(String(255), default="")
    verification_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    rows: Mapped[list["MatriculationRunRow"]] = relationship(back_populates="run")
    course_alerts: Mapped[list["MatriculationCourseAlert"]] = relationship(back_populates="run")


class MatriculationRunRow(Base):
    __tablename__ = "matriculation_run_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("matriculation_runs.id"), index=True)
    action: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="preview", index=True)
    verification_status: Mapped[str] = mapped_column(String(30), default="")
    document: Mapped[str] = mapped_column(String(60), default="", index=True)
    upn: Mapped[str] = mapped_column(String(255), default="", index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    source_code: Mapped[str] = mapped_column(String(40), default="")
    source_course: Mapped[str] = mapped_column(String(255), default="")
    logical_course_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    course_shortname: Mapped[str] = mapped_column(String(120), default="", index=True)
    previous_course_shortname: Mapped[str] = mapped_column(String(120), default="")
    group_name: Mapped[str] = mapped_column(String(20), default="")
    previous_group_name: Mapped[str] = mapped_column(String(20), default="")
    moodle_user_id: Mapped[str] = mapped_column(String(80), default="")
    moodle_course_id: Mapped[str] = mapped_column(String(80), default="")
    previous_moodle_course_id: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped[MatriculationRun] = relationship(back_populates="rows")


class MatriculationCourseAlert(Base):
    __tablename__ = "matriculation_course_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("matriculation_runs.id"), index=True)
    document: Mapped[str] = mapped_column(String(60), default="", index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    modality: Mapped[str] = mapped_column(String(120), default="")
    course_count: Mapped[int] = mapped_column(Integer, default=0)
    course_codes: Mapped[str] = mapped_column(Text, default="")
    course_names: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped[MatriculationRun] = relationship(back_populates="course_alerts")


class MatriculationState(Base):
    __tablename__ = "matriculation_states"
    __table_args__ = (UniqueConstraint("document", "course_shortname", name="uq_matriculation_document_course"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[str] = mapped_column(String(60), default="", index=True)
    upn: Mapped[str] = mapped_column(String(255), default="", index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    logical_course_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    course_shortname: Mapped[str] = mapped_column(String(120), default="", index=True)
    course_name: Mapped[str] = mapped_column(String(255), default="")
    group_name: Mapped[str] = mapped_column(String(20), default="")
    moodle_user_id: Mapped[str] = mapped_column(String(80), default="")
    moodle_course_id: Mapped[str] = mapped_column(String(80), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
