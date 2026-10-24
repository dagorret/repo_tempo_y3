from dataclasses import dataclass
from io import BytesIO
import re
import unicodedata

import pandas as pd

from app.account_rules import build_student_upn, build_teacher_upn, clean_document, title_name


@dataclass
class ImportedRow:
    document: str
    first_name: str
    last_name: str
    full_name: str
    email: str
    upn: str


COLUMN_ALIASES = {
    "document": (
        "documento",
        "dni",
        "numero_documento",
        "numero_de_documento",
        "nro_documento",
        "nro_de_documento",
        "nrodoc",
        "doc",
        "legajo",
    ),
    "first_name": ("nombre", "nombres", "primer_nombre", "firstname", "first_name"),
    "last_name": ("apellido", "apellidos", "surname", "lastname", "last_name"),
    "full_name": (
        "apellido_y_nombre",
        "apellido_y_nombres",
        "apellidos_y_nombres",
        "apellido_nombre",
        "apellidos_nombres",
        "nombre_y_apellido",
        "nombres_y_apellidos",
        "nombre_completo",
        "alumno",
        "estudiante",
        "docente",
        "profesor",
        "persona",
    ),
    "email": ("email", "correo", "mail", "e-mail"),
}


def _normalize_column(column: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(column))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.strip().lower().replace("-", "_")
    ascii_text = re.sub(r"[\s/]+", "_", ascii_text)
    ascii_text = re.sub(r"[^a-z0-9_]+", "", ascii_text)
    return re.sub(r"_+", "_", ascii_text).strip("_")


def _column_map(columns: list[str]) -> dict[str, str]:
    normalized = {_normalize_column(column): column for column in columns}
    result = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                result[target] = normalized[alias]
                break
    return result


def preview_people_excel(content: bytes, limit: int = 5) -> tuple[list[str], list[dict[str, str]]]:
    frame = pd.read_excel(BytesIO(content), dtype=str).fillna("")
    rows = frame.head(limit).to_dict(orient="records")
    return [str(column) for column in frame.columns], rows


def suggest_column_mapping(columns: list[str]) -> dict[str, str]:
    return _column_map(columns)


def _split_full_name(value: str, column_name: str) -> tuple[str, str]:
    text = " ".join(str(value or "").replace(";", ",").split())
    if not text:
        return "", ""

    if "," in text:
        last_name, first_name = [part.strip() for part in text.split(",", 1)]
        return title_name(first_name), title_name(last_name)

    parts = text.split()
    if len(parts) == 1:
        return title_name(parts[0]), ""

    normalized_column = _normalize_column(column_name)
    if normalized_column.startswith("apellido"):
        return title_name(" ".join(parts[1:])), title_name(parts[0])
    return title_name(parts[0]), title_name(" ".join(parts[1:]))


def build_full_name(first_name: str, last_name: str) -> str:
    return " ".join(part for part in [title_name(first_name), title_name(last_name)] if part)


def parse_people_excel(
    content: bytes,
    person_type: str,
    domain: str,
    mapping: dict[str, str] | None = None,
) -> list[ImportedRow]:
    frame = pd.read_excel(BytesIO(content), dtype=str).fillna("")
    mapping = mapping or _column_map(list(frame.columns))
    mapping = {key: value for key, value in mapping.items() if value}

    has_separate_name = "first_name" in mapping and "last_name" in mapping
    has_full_name = "full_name" in mapping
    required = []
    if not has_separate_name and not has_full_name:
        required.extend(["nombre/apellido o nombre completo"])
    if person_type == "student":
        required.append("document")

    missing = [name for name in required if name not in mapping]
    if missing:
        available = ", ".join(str(column) for column in frame.columns)
        raise ValueError(
            f"Faltan columnas requeridas: {', '.join(missing)}. "
            f"Columnas encontradas: {available}"
        )

    rows: list[ImportedRow] = []
    for index, raw in frame.iterrows():
        if has_separate_name:
            first_name = title_name(raw.get(mapping["first_name"], ""))
            last_name = title_name(raw.get(mapping["last_name"], ""))
        else:
            full_column = mapping["full_name"]
            first_name, last_name = _split_full_name(raw.get(full_column, ""), full_column)
        full_name = build_full_name(first_name, last_name)
        document = clean_document(raw.get(mapping.get("document", ""), ""))
        email = str(raw.get(mapping.get("email", ""), "")).strip().lower()

        if not first_name and not last_name and not document:
            continue

        try:
            if person_type == "student":
                upn = build_student_upn(document, domain)
            else:
                upn = build_teacher_upn(first_name, last_name, domain)
        except ValueError as exc:
            raise ValueError(f"Fila {index + 2}: {exc}") from exc

        rows.append(
            ImportedRow(
                document=document,
                first_name=first_name,
                last_name=last_name,
                full_name=full_name,
                email=email,
                upn=upn,
            )
        )

    return rows
