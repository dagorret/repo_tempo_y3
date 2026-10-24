from functools import lru_cache
from pathlib import Path
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


def _serialize_env_value(value: str) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]*", text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f"\"{escaped}\""


def update_env_file(updates: dict[str, str], env_file: str | Path = ".env") -> None:
    path = Path(env_file)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_keys: set[str] = set()
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue
        if "=" not in line:
            # Limpia lineas sueltas/malformadas para evitar que .env se corrompa.
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key in updates:
            result.append(f"{key}={_serialize_env_value(updates[key])}")
            updated_keys.add(key)
        else:
            result.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            result.append(f"{key}={_serialize_env_value(value)}")

    if result and not result[-1].endswith("\n"):
        path.write_text("\n".join(result) + "\n", encoding="utf-8")
    else:
        path.write_text("\n".join(result), encoding="utf-8")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    crm_secret_key: str = "dev-secret-change-me"
    crm_default_password: str = "Cambiar123!"
    crm_database_url: str = "sqlite:///./data/crm_fce.sqlite3"
    crm_domain: str = "eco.unrc.edu.ar"
    crm_dry_run: bool = True
    crm_base_url: str = "http://localhost:8000"
    crm_batch_workers: int = 5
    matriculation_course_alert_threshold: int = 5

    sial_base_url: str = "https://sisinfo.unrc.edu.ar/webservice/sial/V2/04"
    sial_username: str = ""
    sial_password: str = ""
    audit_data_dir: str = "data/auditorias_crm"
    audit_campaign_start: str = "2025-12-01"
    audit_campaign_end: str = ""
    ingreso_allow_writes: bool = False
    ingreso_step_pause: float = 2.0

    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_usage_location: str = "AR"
    azure_student_sku_id: str = ""
    azure_teacher_sku_id: str = ""

    moodle_base_url: str = ""
    moodle_token: str = ""
    moodle_create_missing_users: bool = False
    moodle_auth_method: str = "oidc"
    moodle_student_role_id: int = 5
    moodle_teacher_role_id: int = 3

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "FCE"
    smtp_subject: str = "Cuenta Microsoft 365 - FCE"
    smtp_message_template: str = (
        "Hola.\n\n"
        "Se creo tu cuenta Microsoft 365 para acceder a Teams y Moodle.\n"
        "Nombre: {full_name}\n"
        "Usuario: {upn}\n"
        "Contrasena temporal: {password}\n\n"
        "Al ingresar por primera vez, Microsoft puede pedir cambiar la contrasena.\n\n"
        "FCE"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
