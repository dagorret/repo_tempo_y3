import re
import unicodedata


def clean_document(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(\.0+)?", text):
        text = text.split(".", 1)[0]
    return re.sub(r"\D+", "", text)


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


def title_name(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").strip().split())


def build_student_upn(document: str, domain: str) -> str:
    clean = clean_document(document)
    if not clean:
        raise ValueError("El estudiante no tiene documento valido.")
    return f"a{clean}@{domain}"


def build_teacher_upn(first_name: str, last_name: str, domain: str) -> str:
    first = normalize_name(first_name)
    last = normalize_name(last_name)
    if not first or not last:
        raise ValueError("El docente necesita nombre y apellido validos.")
    return f"{first[0]}{last}@{domain}"
