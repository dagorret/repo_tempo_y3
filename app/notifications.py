from dataclasses import dataclass
import csv
import io
import re

import pandas as pd


@dataclass
class NotificationRecipient:
    dni: str
    last_name: str
    first_name: str
    email: str
    modality_raw: str
    modality_group: str

    @property
    def full_name(self) -> str:
        return " ".join(part for part in [self.first_name.strip(), self.last_name.strip()] if part).strip()


def _decode_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _normalize_modality(value: str) -> str:
    text = str(value or "").strip().upper()
    if "DISTANCIA" in text:
        return "distance"
    if "PRESENCIAL" in text:
        return "presential"
    return "other"


def _valid_email(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) is not None


def parse_notifications_txt(content: bytes) -> list[NotificationRecipient]:
    text = _decode_bytes(content).replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        raise ValueError("El archivo no tiene datos suficientes.")

    header_line = lines[0]
    data_lines = lines[1:]
    normalized = []
    for line in data_lines:
        parts = line.split("+")
        if len(parts) < 5:
            continue
        normalized.append("+".join(parts))

    source = io.StringIO("\n".join([header_line] + normalized))
    reader = csv.reader(source, delimiter="+")
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("No se encontraron filas de inscripcion para procesar.")

    headers = [str(col).strip() for col in rows[0]]
    index = {name: idx for idx, name in enumerate(headers)}
    required = ["NRODOC", "APE.ALUMNO", "NOM.ALUMNO", "MODALIDAD", "MAIL"]
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError(f"Faltan columnas requeridas: {', '.join(missing)}")

    recipients: dict[str, NotificationRecipient] = {}
    for row in rows[1:]:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        email = str(row[index["MAIL"]]).strip().lower()
        if not _valid_email(email):
            continue
        if email in recipients:
            continue
        modality_raw = str(row[index["MODALIDAD"]]).strip()
        recipients[email] = NotificationRecipient(
            dni=str(row[index["NRODOC"]]).strip(),
            last_name=str(row[index["APE.ALUMNO"]]).strip(),
            first_name=str(row[index["NOM.ALUMNO"]]).strip(),
            email=email,
            modality_raw=modality_raw,
            modality_group=_normalize_modality(modality_raw),
        )

    return sorted(recipients.values(), key=lambda r: (r.last_name.lower(), r.first_name.lower(), r.email))


def preview_notifications_excel(content: bytes, limit: int = 8) -> tuple[list[str], list[dict[str, str]]]:
    frame = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
    rows = frame.head(limit).to_dict(orient="records")
    return [str(column) for column in frame.columns], rows


def parse_notifications_excel(content: bytes, mapping: dict[str, str]) -> list[NotificationRecipient]:
    frame = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
    mapping = {k: (v or "").strip() for k, v in mapping.items()}
    email_col = mapping.get("email", "")
    if not email_col:
        raise ValueError("Debes asociar la columna de email.")
    if email_col not in frame.columns:
        raise ValueError("La columna de email seleccionada no existe en el archivo.")

    recipients: dict[str, NotificationRecipient] = {}
    for _, raw in frame.iterrows():
        email = str(raw.get(email_col, "")).strip().lower()
        if not _valid_email(email):
            continue
        if email in recipients:
            continue

        modality_raw = str(raw.get(mapping.get("modality", ""), "")).strip() if mapping.get("modality") else ""
        dni = str(raw.get(mapping.get("dni", ""), "")).strip() if mapping.get("dni") else ""
        last_name = str(raw.get(mapping.get("last_name", ""), "")).strip() if mapping.get("last_name") else ""
        first_name = str(raw.get(mapping.get("first_name", ""), "")).strip() if mapping.get("first_name") else ""

        recipients[email] = NotificationRecipient(
            dni=dni,
            last_name=last_name,
            first_name=first_name,
            email=email,
            modality_raw=modality_raw,
            modality_group=_normalize_modality(modality_raw),
        )

    return sorted(recipients.values(), key=lambda r: (r.last_name.lower(), r.first_name.lower(), r.email))
