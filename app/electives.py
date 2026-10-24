from dataclasses import dataclass
from io import BytesIO
import re
import unicodedata

import pandas as pd

from app.account_rules import build_student_upn, clean_document, title_name


@dataclass
class ElectiveRow:
    row_number: int
    document: str
    first_name: str
    last_name: str
    email: str
    upn: str
    course_shortname: str
    group_name: str
    source_course: str


@dataclass
class ElectiveIssue:
    row_number: int
    document: str
    first_name: str
    last_name: str
    course_shortname: str
    source_course: str
    message: str


def _normalize_column(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _unique_columns(values: list[object]) -> list[str]:
    used: dict[str, int] = {}
    columns: list[str] = []
    for index, value in enumerate(values, start=1):
        base = _clean_text(value) or f"Columna {index}"
        count = used.get(base, 0)
        used[base] = count + 1
        columns.append(base if count == 0 else f"{base} ({count + 1})")
    return columns


def _looks_like_header(values: list[object]) -> bool:
    normalized = {_normalize_column(value) for value in values if _clean_text(value)}
    has_document = bool(
        normalized
        & {
            "dni",
            "documento",
            "nrodoc",
            "nro_documento",
            "numero_documento",
        }
    )
    has_context = bool(
        normalized
        & {
            "modalidad",
            "codigo",
            "codigo_moodle",
            "shortname",
            "curso",
            "asignatura",
            "asignatura_electiva",
            "asigntura_electiva",
        }
    )
    return has_document and has_context


def _read_elective_frame(content: bytes) -> tuple[pd.DataFrame, int]:
    raw = pd.read_excel(BytesIO(content), header=None, dtype=str).fillna("")
    header_index = 0
    for index in range(min(len(raw.index), 30)):
        if _looks_like_header(raw.iloc[index].tolist()):
            header_index = index
            break

    header_values = raw.iloc[header_index].tolist()
    known_aliases = {
        alias
        for aliases in ELECTIVE_COLUMN_ALIASES.values()
        for alias in aliases
    }
    for column_index, value in enumerate(header_values):
        if _clean_text(value):
            continue
        for previous_index in range(header_index - 1, -1, -1):
            candidate = raw.iat[previous_index, column_index]
            if _normalize_column(candidate) in known_aliases:
                header_values[column_index] = candidate
                break

    columns = _unique_columns(header_values)
    frame = raw.iloc[header_index + 1 :].copy()
    frame.columns = columns
    frame = frame.fillna("")
    return frame, header_index


def preview_elective_excel(content: bytes, limit: int = 5) -> tuple[list[str], list[dict[str, str]]]:
    frame, _ = _read_elective_frame(content)
    return [str(column) for column in frame.columns], frame.head(limit).to_dict(orient="records")


ELECTIVE_COLUMN_ALIASES = {
    "document": (
        "dni",
        "documento",
        "nrodoc",
        "nro_documento",
        "numero_documento",
        "numero_de_documento",
    ),
    "first_name": ("nombre", "nombres", "primer_nombre", "first_name"),
    "last_name": ("apellido", "apellidos", "last_name", "surname"),
    "full_name": (
        "alumno",
        "estudiante",
        "nombre_completo",
        "apellido_y_nombre",
        "apellido_y_nombres",
        "apellidos_y_nombres",
        "apellido_nombre",
        "apellidos_nombres",
    ),
    "email": ("email", "correo", "mail", "e_mail"),
    "modality": ("modalidad", "modalidad_cursado"),
    "course": ("codigo_moodle", "shortname", "nombre_corto", "codigo", "curso_codigo"),
    "subject": (
        "asignatura_electiva",
        "asigntura_electiva",
        "asignatura",
        "asigntura",
        "materia",
        "materia_electiva",
    ),
}


def suggest_elective_mapping(columns: list[str]) -> dict[str, str]:
    normalized = {_normalize_column(column): column for column in columns}
    mapping: dict[str, str] = {}
    for field, aliases in ELECTIVE_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    return mapping


def _subject_key(value: object) -> str:
    text = _clean_text(value)
    text = re.sub(r"\s*\([A-Za-z0-9][A-Za-z0-9._-]*\)\s*$", "", text)
    return _normalize_column(text)


def _subject_label(value: object) -> str:
    text = _clean_text(value)
    return re.sub(r"\s*\([A-Za-z0-9][A-Za-z0-9._-]*\)\s*$", "", text).strip()


def _split_full_name(value: object, column_name: str) -> tuple[str, str]:
    text = _clean_text(value)
    if not text:
        return "", ""
    if "," in text:
        last_name, first_name = [part.strip() for part in text.split(",", 1)]
        return title_name(first_name), title_name(last_name)
    parts = text.split()
    if len(parts) == 1:
        return title_name(parts[0]), ""
    if _normalize_column(column_name).startswith("apellido"):
        return title_name(" ".join(parts[1:])), title_name(parts[0])
    return title_name(parts[0]), title_name(" ".join(parts[1:]))


def _normalize_course_shortname(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    parenthesized = re.search(r"\(([^()]+)\)\s*$", text)
    if parenthesized:
        candidate = _clean_text(parenthesized.group(1))
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate):
            return candidate
    leading_code = re.match(r"^(\d+[A-Za-z0-9._-]*)\s+(?:-|:)\s*.+$", text)
    if leading_code:
        return leading_code.group(1)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text) and re.search(r"\d", text):
        return text
    return ""


def analyze_elective_subjects(
    content: bytes,
    subject_column: str = "",
) -> list[dict[str, object]]:
    frame, _ = _read_elective_frame(content)
    if not subject_column:
        subject_column = suggest_elective_mapping(
            [str(column) for column in frame.columns]
        ).get("subject", "")
    if not subject_column or subject_column not in frame.columns:
        return []

    subjects: dict[str, dict[str, object]] = {}
    for value in frame[subject_column].tolist():
        label = _subject_label(value)
        key = _subject_key(value)
        if not key:
            continue
        item = subjects.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "row_count": 0,
                "candidate_courses": set(),
            },
        )
        item["row_count"] = int(item["row_count"]) + 1
        course = _normalize_course_shortname(value)
        if course:
            item["candidate_courses"].add(course)

    result: list[dict[str, object]] = []
    for item in sorted(subjects.values(), key=lambda entry: str(entry["label"]).lower()):
        candidates = sorted(str(value) for value in item["candidate_courses"])
        result.append(
            {
                "key": item["key"],
                "label": item["label"],
                "row_count": item["row_count"],
                "candidate_courses": candidates,
                "suggested_course": candidates[0] if len(candidates) == 1 else "",
            }
        )
    return result


def _group_from_modality(value: object) -> str:
    text = _normalize_column(value)
    if text in {"d", "distancia", "a_distancia"} or "dist" in text:
        return "D"
    if text in {"p", "presencial", "a_presencial"} or "presen" in text:
        return "P"
    return ""


def parse_elective_excel(
    content: bytes,
    domain: str,
    mapping: dict[str, str],
    subject_course_map: dict[str, str] | None = None,
    default_group: str = "",
) -> tuple[list[ElectiveRow], list[ElectiveIssue], int, int]:
    frame, header_index = _read_elective_frame(content)
    mapping = {key: value for key, value in mapping.items() if value}
    subject_course_map = {
        str(key): _normalize_course_shortname(value)
        for key, value in (subject_course_map or {}).items()
        if _normalize_course_shortname(value)
    }
    default_group = default_group.strip().upper()
    required = ["document"]
    if not (mapping.get("first_name") and mapping.get("last_name")) and not mapping.get("full_name"):
        required.append("nombre y apellido, o nombre completo")
    if not mapping.get("course") and not mapping.get("subject"):
        required.append("codigo Moodle o materia")
    if not mapping.get("modality") and default_group not in {"P", "D"}:
        required.append("modalidad o grupo general P/D")
    missing = [field for field in required if field not in mapping]
    missing = [
        field
        for field in missing
        if field not in {"modalidad o grupo general P/D"}
        or default_group not in {"P", "D"}
    ]
    if missing:
        raise ValueError(f"Faltan asociaciones requeridas: {', '.join(missing)}.")

    rows: list[ElectiveRow] = []
    issues: list[ElectiveIssue] = []
    seen: dict[tuple[str, str], ElectiveRow] = {}
    duplicates = 0
    total_rows = 0
    for index, raw in frame.iterrows():
        row_number = int(index) + header_index + 2
        document = clean_document(raw.get(mapping["document"], ""))
        subject = _clean_text(raw.get(mapping.get("subject", ""), ""))
        subject_key = _subject_key(subject)
        course_shortname = subject_course_map.get(subject_key, "")
        if not course_shortname and mapping.get("course"):
            course_shortname = _normalize_course_shortname(raw.get(mapping["course"], ""))
        modality_value = _clean_text(raw.get(mapping.get("modality", ""), ""))
        if mapping.get("first_name") and mapping.get("last_name"):
            first_name = title_name(raw.get(mapping["first_name"], ""))
            last_name = title_name(raw.get(mapping["last_name"], ""))
        else:
            first_name, last_name = _split_full_name(
                raw.get(mapping.get("full_name", ""), ""),
                mapping.get("full_name", ""),
            )
        email = _clean_text(raw.get(mapping.get("email", ""), "")).lower()

        if not any([document, course_shortname, first_name, last_name, modality_value, subject]):
            continue
        total_rows += 1
        group_name = _group_from_modality(modality_value) or default_group
        if not document or not course_shortname or not first_name or not last_name or not group_name:
            missing_fields = []
            if not document:
                missing_fields.append("DNI")
            if not first_name or not last_name:
                missing_fields.append("nombre/apellido")
            if not course_shortname:
                missing_fields.append("codigo Moodle")
            if not group_name:
                missing_fields.append("modalidad P o D")
            issues.append(
                ElectiveIssue(
                    row_number=row_number,
                    document=document,
                    first_name=first_name,
                    last_name=last_name,
                    course_shortname=course_shortname,
                    source_course=subject,
                    message=f"Faltan o no son validos: {', '.join(missing_fields)}.",
                )
            )
            continue
        try:
            upn = build_student_upn(document, domain)
        except ValueError as exc:
            issues.append(
                ElectiveIssue(
                    row_number=row_number,
                    document=document,
                    first_name=first_name,
                    last_name=last_name,
                    course_shortname=course_shortname,
                    source_course=subject,
                    message=str(exc),
                )
            )
            continue

        record = ElectiveRow(
            row_number=row_number,
            document=document,
            first_name=first_name,
            last_name=last_name,
            email=email,
            upn=upn,
            course_shortname=course_shortname,
            group_name=group_name,
            source_course=subject,
        )
        key = (document, course_shortname)
        previous = seen.get(key)
        if previous:
            if previous.group_name != group_name:
                issues.append(
                    ElectiveIssue(
                        row_number=row_number,
                        document=document,
                        first_name=first_name,
                        last_name=last_name,
                        course_shortname=course_shortname,
                        source_course=subject,
                        message=(
                            f"El Excel tiene modalidades distintas para el mismo alumno y curso: "
                            f"{previous.group_name} y {group_name}."
                        ),
                    )
                )
            else:
                duplicates += 1
            continue
        seen[key] = record
        rows.append(record)

    return rows, issues, total_rows, duplicates
