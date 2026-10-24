from __future__ import annotations

import csv
import io
import re


def _decode_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _clean_dni(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _extract_dni_from_username(username: str) -> str:
    text = str(username or "").strip().lower()
    match = re.match(r"^a(\d+)@", text)
    if match:
        return match.group(1)
    return ""


def _valid_email(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) is not None


def parse_lincu_txt(content: bytes) -> tuple[dict[str, dict], int]:
    text = _decode_bytes(content).replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        raise ValueError("El archivo Lincu no tiene datos suficientes.")

    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter="+")
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("No se encontraron filas validas en Lincu.")

    headers = [str(col or "").strip().upper() for col in rows[0]]
    index = {name: idx for idx, name in enumerate(headers)}
    required = ["NRODOC", "APE.ALUMNO", "NOM.ALUMNO", "MAIL", "CODIGO", "MATERIA", "MODALIDAD"]
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError(f"Lincu sin columnas requeridas: {', '.join(missing)}")

    by_dni: dict[str, dict] = {}
    total_rows = 0
    for row in rows[1:]:
        total_rows += 1
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))

        dni = _clean_dni(row[index["NRODOC"]])
        if not dni:
            continue
        last_name = str(row[index["APE.ALUMNO"]]).strip()
        first_name = str(row[index["NOM.ALUMNO"]]).strip()
        email = str(row[index["MAIL"]]).strip().lower()
        code = str(row[index["CODIGO"]]).strip()
        materia = str(row[index["MATERIA"]]).strip()
        modality = str(row[index["MODALIDAD"]]).strip()
        subject_key = f"{code}|{materia}".strip("|")

        current = by_dni.get(dni)
        if not current:
            current = {
                "dni": dni,
                "last_name": last_name,
                "first_name": first_name,
                "email": email if _valid_email(email) else "",
                "subjects": set(),
                "modalities": set(),
            }
            by_dni[dni] = current
        if not current["last_name"] and last_name:
            current["last_name"] = last_name
        if not current["first_name"] and first_name:
            current["first_name"] = first_name
        if not current["email"] and _valid_email(email):
            current["email"] = email
        if subject_key:
            current["subjects"].add(subject_key)
        if modality:
            current["modalities"].add(modality)

    return by_dni, total_rows


def parse_ingresos_csv(content: bytes) -> tuple[dict[str, dict], int]:
    text = _decode_bytes(content)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV de ingresos sin encabezados.")

    raw_headers = [str(h or "").strip() for h in reader.fieldnames]
    headers_map = {h.lower(): h for h in raw_headers}
    username_col = headers_map.get("username")
    ingresos_col = headers_map.get("ingresos")
    shortname_col = headers_map.get("course_shortname")
    fullcourse_col = headers_map.get("course_fullname")
    dni_col = headers_map.get("dni") or headers_map.get("nrodoc")

    if not username_col and not dni_col:
        raise ValueError("CSV de ingresos debe incluir 'username' o 'dni/nrodoc'.")
    if not ingresos_col:
        raise ValueError("CSV de ingresos sin columna 'ingresos'.")

    by_dni: dict[str, dict] = {}
    total_rows = 0
    for row in reader:
        total_rows += 1
        username = str(row.get(username_col, "") if username_col else "").strip().lower()
        dni = _extract_dni_from_username(username)
        if not dni and dni_col:
            dni = _clean_dni(str(row.get(dni_col, "")).strip())
        if not dni:
            continue

        ingresos_raw = str(row.get(ingresos_col, "")).strip()
        try:
            ingresos = int(float(ingresos_raw))
        except ValueError:
            continue

        course_shortname = str(row.get(shortname_col, "")).strip() if shortname_col else ""
        course_name = str(row.get(fullcourse_col, "")).strip() if fullcourse_col else ""
        course_label = course_shortname or course_name

        current = by_dni.get(dni)
        if not current:
            current = {
                "dni": dni,
                "upn": username if username else f"a{dni}@eco.unrc.edu.ar",
                "ingresos_values": set(),
                "courses": set(),
                "rows_count": 0,
            }
            by_dni[dni] = current
        if username and not current["upn"]:
            current["upn"] = username

        current["ingresos_values"].add(ingresos)
        if course_label:
            current["courses"].add(course_label)
        current["rows_count"] += 1

    return by_dni, total_rows


def build_lincu_report(lincu_by_dni: dict[str, dict], ingresos_by_dni: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for dni, ingreso in ingresos_by_dni.items():
        lincu = lincu_by_dni.get(dni, {})
        ingresos_values = sorted(int(v) for v in ingreso.get("ingresos_values", set()))
        if not ingresos_values:
            continue
        ingresos_text = ",".join(str(v) for v in ingresos_values)
        row = {
            "dni": dni,
            "last_name": str(lincu.get("last_name", "") or "").strip() or "-",
            "first_name": str(lincu.get("first_name", "") or "").strip() or "-",
            "upn": str(ingreso.get("upn", "") or "").strip().lower(),
            "ingresos": ingresos_text,
            "cursos_csv": len(ingreso.get("courses", set())),
            "materias_lincu": len(lincu.get("subjects", set())),
            "modality": " / ".join(sorted(str(v).strip() for v in lincu.get("modalities", set()) if str(v).strip())) or "-",
            "email_personal": str(lincu.get("email", "") or "").strip().lower() or "-",
        }
        rows.append(row)

    rows.sort(key=lambda item: (item["last_name"].lower(), item["first_name"].lower(), item["dni"]))
    return rows
