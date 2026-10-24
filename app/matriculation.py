from __future__ import annotations

from dataclasses import dataclass
import csv
import io
import re

from app.account_rules import build_student_upn, clean_document


@dataclass(frozen=True)
class CourseRule:
    actual: str
    old: str = ""
    category: str = ""


@dataclass
class DesiredMatriculation:
    document: str
    upn: str
    first_name: str
    last_name: str
    email: str
    source_code: str
    source_course: str
    source_category: str
    logical_course_code: str
    course_shortname: str
    group_name: str


@dataclass
class ConversionIssue:
    document: str
    source_code: str
    source_course: str
    message: str


@dataclass(frozen=True)
class CourseLoadAlert:
    document: str
    first_name: str
    last_name: str
    phone: str
    email: str
    modality: str
    course_count: int
    courses: tuple[tuple[str, str], ...]


COURSE_RULES: list[CourseRule] = [
    CourseRule("7"), CourseRule("10"), CourseRule("13"), CourseRule("73"), CourseRule("86"),
    CourseRule("100", "1"), CourseRule("101", "2"), CourseRule("102", "3", "2"),
    CourseRule("103", "8", "X*"), CourseRule("104", "4"), CourseRule("105", "9", "X"),
    CourseRule("106", "14"), CourseRule("107", "5"), CourseRule("108", "11"),
    CourseRule("108", "15"), CourseRule("109", "6"), CourseRule("200"), CourseRule("201", "12"),
    CourseRule("202"), CourseRule("203"), CourseRule("204", "57"), CourseRule("205", "210"),
    CourseRule("205", "59"), CourseRule("205", "61"), CourseRule("205", "81"),
    CourseRule("206", "60"), CourseRule("207", "80"), CourseRule("208", "58"),
    CourseRule("209", "65"), CourseRule("211"), CourseRule("212"), CourseRule("212", "69"),
    CourseRule("213", "62"), CourseRule("214", "72"), CourseRule("215", "68"),
    CourseRule("216", "63"), CourseRule("217", "66"), CourseRule("218", "67"),
    CourseRule("219"), CourseRule("220", "64"), CourseRule("221", "70"),
    CourseRule("222", "76"), CourseRule("222"), CourseRule("223", "71"),
    CourseRule("223", "74"), CourseRule("224", "75"), CourseRule("225"),
    CourseRule("227"), CourseRule("228"), CourseRule("229"), CourseRule("230"),
    CourseRule("231"), CourseRule("232"), CourseRule("233"), CourseRule("235"),
    CourseRule("300", "39"), CourseRule("301", "16", "X"), CourseRule("302", "23"),
    CourseRule("303", "21"), CourseRule("304", "40", "X"), CourseRule("305", "17"),
    CourseRule("306", "26"), CourseRule("307", "19"), CourseRule("308", "41"),
    CourseRule("309", "26"), CourseRule("310", "47"), CourseRule("311", "42"),
    CourseRule("312", "43"), CourseRule("313", "412"), CourseRule("313", "27"),
    CourseRule("314", "44"), CourseRule("315"), CourseRule("316", "46"),
    CourseRule("317", "45"), CourseRule("318", "45"), CourseRule("318", "49"),
    CourseRule("319", "48"), CourseRule("320", "50"), CourseRule("321", "51"),
    CourseRule("322", "52"), CourseRule("324"), CourseRule("327"), CourseRule("328"),
    CourseRule("330", "54", "X"), CourseRule("332", "37"), CourseRule("332", "56"),
    CourseRule("339", "2000"), CourseRule("340", "423"), CourseRule("340", "343"),
    CourseRule("340", "33"), CourseRule("341"), CourseRule("342"), CourseRule("344", "323"),
    CourseRule("344", "55"), CourseRule("345", "425"), CourseRule("346"),
    CourseRule("347", "337"), CourseRule("400", "18"), CourseRule("401", "24"),
    CourseRule("402", "85"), CourseRule("403"), CourseRule("404", "30"),
    CourseRule("405", "20"), CourseRule("406", "28"), CourseRule("407", "29"),
    CourseRule("408"), CourseRule("409", "25"), CourseRule("410", "32"),
    CourseRule("411", "22"), CourseRule("413", "47"), CourseRule("414"),
    CourseRule("415", "31"), CourseRule("416"), CourseRule("417", "35"),
    CourseRule("418", "34"), CourseRule("419", "36"), CourseRule("420", "89"),
    CourseRule("422"), CourseRule("424"), CourseRule("650", "701"), CourseRule("651", "702"),
    CourseRule("652", "703"), CourseRule("653", "708"), CourseRule("654"), CourseRule("655"),
    CourseRule("656"), CourseRule("657"), CourseRule("658", "711"), CourseRule("659", "709"),
    CourseRule("660", "710"), CourseRule("661", "716"), CourseRule("662", "712"),
    CourseRule("663", "713"), CourseRule("664", "714"), CourseRule("665", "715"),
    CourseRule("666", "704"), CourseRule("667", "717"), CourseRule("668", "718"),
    CourseRule("669", "719"), CourseRule("670", "720"), CourseRule("671", "721"),
    CourseRule("801"), CourseRule("802"), CourseRule("803"), CourseRule("804"),
    CourseRule("805"), CourseRule("806"), CourseRule("807"), CourseRule("808"),
    CourseRule("809"), CourseRule("810"), CourseRule("811"), CourseRule("812"),
    CourseRule("813"), CourseRule("814"), CourseRule("815"), CourseRule("816"),
    CourseRule("817"), CourseRule("818"), CourseRule("1013"), CourseRule("1014"),
    CourseRule("1015"), CourseRule("1016"), CourseRule("1017"), CourseRule("1018"),
    CourseRule("1019"), CourseRule("1020"), CourseRule("1021"), CourseRule("1022"),
    CourseRule("1023"), CourseRule("1024"), CourseRule("1025"), CourseRule("1026"),
    CourseRule("1027"),
]


def _decode_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _title_like_converter(value: str) -> str:
    text = str(value or "").replace(",", " ").strip()
    words = []
    for part in re.split(r"(\s+)", text):
        if not part.strip():
            words.append(part)
            continue
        lower = part.lower()
        if lower in {"y", "de", "del", "la", "las", "los"}:
            words.append(lower)
        elif part.upper() == "UNRC":
            words.append("UNRC")
        else:
            words.append(part[:1].upper() + part[1:].lower())
    return "".join(words).strip()


def _group_from_modality(modality: str) -> str:
    return "D" if "DIST" in str(modality or "").upper() else "P"


def _find_rule(source_code: str, rules: list[CourseRule]) -> CourseRule | None:
    code = str(int(source_code)) if str(source_code or "").strip().isdigit() else str(source_code or "").strip()
    for rule in rules:
        if rule.actual == code:
            return rule
        if rule.old and rule.old == code:
            return rule
    return None


def _course_shortname(rule: CourseRule, source_category: str, group_name: str) -> str:
    shortname = rule.actual
    category_rule = str(rule.category or "").strip()
    append_modality = category_rule.endswith("*")
    if append_modality:
        category_rule = category_rule[:-1]

    category = str(source_category or "").strip()
    if category_rule:
        if category_rule.upper() != "X" and _digits(category) in {"", "0"}:
            category = category_rule
        shortname = f"{shortname}-{category}"

    if append_modality:
        shortname = f"{shortname}{group_name}"

    if shortname.startswith("105"):
        category_value = int(_digits(category) or "0")
        if category_value == 0 and group_name == "D":
            return "105-1"
        if category_value == 1 and group_name == "P":
            return "105-1"
        if category_value == 2 and group_name == "P":
            return "105-2"
        if category_value == 1 and group_name == "D":
            return "105-3"
        if category_value == 3 and group_name == "P":
            return "105-3"

    return shortname


def _read_lincu_table(content: bytes) -> tuple[list[str], dict[str, int], list[list[str]]]:
    text = _decode_bytes(content).replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        raise ValueError("El archivo Lincu no tiene datos suficientes.")

    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter="+"))
    headers = [str(col or "").strip().upper() for col in rows[0]]
    index = {name: idx for idx, name in enumerate(headers)}
    data_rows: list[list[str]] = []
    for raw in rows[1:]:
        if len(raw) < len(headers):
            raw = raw + [""] * (len(headers) - len(raw))
        data_rows.append(raw)
    return headers, index, data_rows


def build_course_load_alerts(
    content: bytes,
    threshold: int = 5,
    rules: list[CourseRule] | None = None,
) -> list[CourseLoadAlert]:
    conversion_rules = COURSE_RULES if rules is None else rules
    _, index, rows = _read_lincu_table(content)
    required = ["CODIGO", "MATERIA", "MODALIDAD", "NRODOC", "APE.ALUMNO", "NOM.ALUMNO", "MAIL"]
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError(f"Lincu sin columnas requeridas: {', '.join(missing)}")

    students: dict[str, dict] = {}
    for raw in rows:
        document = clean_document(raw[index["NRODOC"]])
        source_code = _digits(raw[index["CODIGO"]])
        if not document or not source_code:
            continue

        student = students.setdefault(
            document,
            {
                "first_name": "",
                "last_name": "",
                "phone": "",
                "email": "",
                "modalities": set(),
                "courses": {},
            },
        )
        first_name = _title_like_converter(raw[index["NOM.ALUMNO"]])
        last_name = _title_like_converter(raw[index["APE.ALUMNO"]])
        email = str(raw[index["MAIL"]] or "").strip().lower()
        phone = str(raw[index["TELEFONO"]] or "").strip() if "TELEFONO" in index else ""
        modality = str(raw[index["MODALIDAD"]] or "").strip()
        if first_name:
            student["first_name"] = first_name
        if last_name:
            student["last_name"] = last_name
        if email:
            student["email"] = email
        if phone:
            student["phone"] = phone
        if modality:
            student["modalities"].add(modality)

        rule = _find_rule(source_code, conversion_rules)
        logical_code = rule.actual if rule else source_code
        course_name = str(raw[index["MATERIA"]] or "").strip()
        student["courses"][logical_code] = (logical_code, course_name)

    minimum = max(1, int(threshold or 5))
    alerts: list[CourseLoadAlert] = []
    for document, student in students.items():
        courses = tuple(
            sorted(
                student["courses"].values(),
                key=lambda item: (int(item[0]) if item[0].isdigit() else 10**9, item[0]),
            )
        )
        if len(courses) < minimum:
            continue
        alerts.append(
            CourseLoadAlert(
                document=document,
                first_name=student["first_name"],
                last_name=student["last_name"],
                phone=student["phone"],
                email=student["email"],
                modality=" / ".join(sorted(student["modalities"])) or "-",
                course_count=len(courses),
                courses=courses,
            )
        )

    return sorted(alerts, key=lambda item: (item.last_name.lower(), item.first_name.lower(), item.document))


def parse_matriculation_lincu(
    content: bytes,
    domain: str,
    rules: list[CourseRule] | None = None,
) -> tuple[dict[tuple[str, str], DesiredMatriculation], int, list[ConversionIssue]]:
    conversion_rules = COURSE_RULES if rules is None else rules
    _, index, rows = _read_lincu_table(content)
    required = ["CODIGO", "MATERIA", "MODALIDAD", "CATEDRA", "NRODOC", "APE.ALUMNO", "NOM.ALUMNO", "MAIL"]
    missing = [name for name in required if name not in index]
    if missing:
        raise ValueError(f"Lincu sin columnas requeridas: {', '.join(missing)}")

    desired: dict[tuple[str, str], DesiredMatriculation] = {}
    issues: list[ConversionIssue] = []
    total_rows = 0

    for raw in rows:
        total_rows += 1
        document = clean_document(raw[index["NRODOC"]])
        source_code = _digits(raw[index["CODIGO"]])
        source_course = str(raw[index["MATERIA"]]).strip()
        if not document or not source_code:
            issues.append(ConversionIssue(document, source_code, source_course, "Fila sin DNI o codigo."))
            continue

        rule = _find_rule(source_code, conversion_rules)
        if not rule:
            issues.append(ConversionIssue(document, source_code, source_course, "Codigo sin regla de conversion."))
            continue

        group_name = _group_from_modality(raw[index["MODALIDAD"]])
        source_category = str(raw[index["CATEDRA"]]).strip()
        course_shortname = _course_shortname(rule, source_category, group_name)
        upn = build_student_upn(document, domain)
        item = DesiredMatriculation(
            document=document,
            upn=upn,
            first_name=_title_like_converter(raw[index["NOM.ALUMNO"]]),
            last_name=_title_like_converter(raw[index["APE.ALUMNO"]]),
            email=str(raw[index["MAIL"]]).strip().lower(),
            source_code=source_code,
            source_course=source_course,
            source_category=source_category,
            logical_course_code=rule.actual,
            course_shortname=course_shortname,
            group_name=group_name,
        )

        key = (document, rule.actual)
        current = desired.get(key)
        if current and (
            current.course_shortname != item.course_shortname
            or current.group_name != item.group_name
        ):
            issues.append(
                ConversionIssue(
                    document,
                    source_code,
                    source_course,
                    (
                        f"Conflicto para la materia {rule.actual}: "
                        f"{current.course_shortname}/{current.group_name} y "
                        f"{item.course_shortname}/{item.group_name}."
                    ),
                )
            )
            continue
        desired[key] = item

    return desired, total_rows, issues
