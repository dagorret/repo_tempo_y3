from dataclasses import dataclass
import math
import threading
import time
import unicodedata

import httpx

from app.config import Settings


# Moodle sits behind a rate limiter. Keep CRM requests paced across every
# web request and background batch, while leaving bulk API calls enabled.
MOODLE_REQUEST_INTERVAL_SECONDS = 0.4
MOODLE_READ_RETRY_DELAYS_SECONDS = (1.0, 3.0, 6.0)
_MOODLE_REQUEST_LOCK = threading.Lock()
_MOODLE_NEXT_REQUEST_AT = 0.0


MOODLE_FUNCTION_CATALOG = (
    {"name": "core_webservice_get_site_info", "area": "Servicio", "mode": "Lectura", "purpose": "Auditar funciones habilitadas"},
    {"name": "core_course_get_courses_by_field", "area": "Cursos", "mode": "Lectura", "purpose": "Buscar cursos por nombre corto"},
    {"name": "core_course_search_courses", "area": "Cursos", "mode": "Lectura", "purpose": "Buscar cursos por texto"},
    {"name": "core_enrol_get_enrolled_users", "area": "Matriculas", "mode": "Lectura", "purpose": "Consultar participantes"},
    {"name": "core_enrol_get_users_courses", "area": "Matriculas", "mode": "Lectura", "purpose": "Consultar cursos de un usuario"},
    {"name": "core_user_get_users_by_field", "area": "Usuarios", "mode": "Lectura", "purpose": "Buscar usuarios"},
    {"name": "core_group_get_course_groups", "area": "Grupos", "mode": "Lectura", "purpose": "Consultar grupos de un curso"},
    {"name": "core_group_get_group_members", "area": "Grupos", "mode": "Lectura", "purpose": "Consultar integrantes de un grupo"},
    {"name": "core_group_create_groups", "area": "Grupos", "mode": "Escritura", "purpose": "Crear grupos Moodle"},
    {"name": "core_group_add_group_members", "area": "Grupos", "mode": "Escritura", "purpose": "Agregar usuarios a grupos"},
    {"name": "core_group_delete_group_members", "area": "Grupos", "mode": "Escritura", "purpose": "Quitar usuarios de grupos"},
    {"name": "core_user_create_users", "area": "Usuarios", "mode": "Escritura", "purpose": "Crear usuarios Moodle"},
    {"name": "core_user_delete_users", "area": "Usuarios", "mode": "Escritura", "purpose": "Eliminar usuarios Moodle"},
    {"name": "enrol_manual_enrol_users", "area": "Matriculas", "mode": "Escritura", "purpose": "Matricular usuarios"},
    {"name": "enrol_manual_unenrol_users", "area": "Matriculas", "mode": "Escritura", "purpose": "Desmatricular usuarios"},
)


@dataclass
class MoodleResult:
    status: str
    message: str = ""
    data: dict | list | None = None


class MoodleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.moodle_base_url and settings.moodle_token)
        self.endpoint = settings.moodle_base_url.rstrip("/") + "/webservice/rest/server.php"

    @staticmethod
    def _wait_for_moodle_slot() -> None:
        global _MOODLE_NEXT_REQUEST_AT
        with _MOODLE_REQUEST_LOCK:
            now = time.monotonic()
            wait_seconds = max(0.0, _MOODLE_NEXT_REQUEST_AT - now)
            _MOODLE_NEXT_REQUEST_AT = max(now, _MOODLE_NEXT_REQUEST_AT) + MOODLE_REQUEST_INTERVAL_SECONDS
        if wait_seconds:
            time.sleep(wait_seconds)

    def _call(self, function: str, payload: dict) -> MoodleResult:
        if not self.enabled:
            return MoodleResult("skipped", "Moodle no configurado.")
        data = {
            "wstoken": self.settings.moodle_token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **payload,
        }
        try:
            self._wait_for_moodle_slot()
            with httpx.Client(timeout=30) as client:
                response = client.post(self.endpoint, data=data)
        except httpx.HTTPError as exc:
            return MoodleResult("error", f"No se pudo conectar con Moodle: {exc}")
        if not response.is_success:
            detail = response.text.strip()
            message = f"Moodle respondio HTTP {response.status_code}"
            if detail:
                message += f": {detail}"
            return MoodleResult("error", message)
        try:
            parsed = response.json()
        except ValueError:
            detail = response.text.strip()
            return MoodleResult(
                "error",
                f"Moodle devolvio una respuesta no valida{': ' + detail if detail else ''}",
            )
        if isinstance(parsed, dict) and parsed.get("exception"):
            return MoodleResult("error", parsed.get("message", str(parsed)), parsed)
        return MoodleResult("ok", "Operacion Moodle exitosa.", parsed)

    def _read_call(self, function: str, payload: dict) -> MoodleResult:
        """Retry only read endpoints after transient Moodle rate limiting."""
        result = self._call(function, payload)
        for delay in MOODLE_READ_RETRY_DELAYS_SECONDS:
            if result.status != "error":
                break
            time.sleep(delay)
            result = self._call(function, payload)
        return result

    def get_service_info(self) -> MoodleResult:
        result = self._read_call("core_webservice_get_site_info", {})
        if result.status != "ok":
            return result
        if not isinstance(result.data, dict):
            return MoodleResult("error", "Moodle devolvio informacion de servicio invalida.")
        functions = result.data.get("functions")
        if not isinstance(functions, list):
            return MoodleResult("error", "Moodle no informo las funciones habilitadas para este token.")
        return MoodleResult("ok", "Funciones Moodle obtenidas.", result.data)

    def get_course_by_shortname(self, shortname: str) -> MoodleResult:
        result = self._read_call(
            "core_course_get_courses_by_field",
            {"field": "shortname", "value": shortname},
        )
        if result.status != "ok":
            return result
        courses = result.data.get("courses", []) if isinstance(result.data, dict) else []
        if not courses:
            return MoodleResult("missing", f"No existe curso con shortname {shortname}.")
        return MoodleResult("ok", "Curso encontrado.", courses[0])

    def search_courses_by_text(self, text: str, perpage: int = 100) -> MoodleResult:
        query = str(text or "").strip()
        if not query:
            return MoodleResult("ok", "Busqueda vacia.", [])

        first_page = self._read_call(
            "core_course_search_courses",
            {
                "criterianame": "search",
                "criteriavalue": query,
                "page": 0,
                "perpage": perpage,
            },
        )
        if first_page.status != "ok":
            return first_page
        if not isinstance(first_page.data, dict):
            return MoodleResult("error", "Respuesta invalida al buscar cursos.")

        total = int(first_page.data.get("total", 0) or 0)
        courses = list(first_page.data.get("courses", []) or [])
        if total <= len(courses) or perpage <= 0:
            return MoodleResult("ok", "Cursos obtenidos.", courses)

        total_pages = int(math.ceil(total / perpage))
        for page in range(1, total_pages):
            page_result = self._read_call(
                "core_course_search_courses",
                {
                    "criterianame": "search",
                    "criteriavalue": query,
                    "page": page,
                    "perpage": perpage,
                },
            )
            if page_result.status != "ok":
                return page_result
            if isinstance(page_result.data, dict):
                courses.extend(page_result.data.get("courses", []) or [])

        return MoodleResult("ok", "Cursos obtenidos.", courses)

    def get_enrolled_users(self, course_id: int) -> MoodleResult:
        result = self._read_call("core_enrol_get_enrolled_users", {"courseid": course_id})
        if result.status != "ok":
            return result
        if not isinstance(result.data, list):
            return MoodleResult("error", "Respuesta invalida al consultar participantes Moodle; no se puede confirmar una lista vacia.")
        return MoodleResult("ok", "Participantes del curso obtenidos.", result.data)

    @staticmethod
    def _normalize_group_name(value: str) -> str:
        text = str(value or "").strip().lower()
        normalized = unicodedata.normalize("NFKD", text)
        return normalized.encode("ascii", "ignore").decode("ascii")

    def get_user_by_username(self, username: str) -> MoodleResult:
        payload = {"field": "username", "values[0]": username}
        result = self._read_call("core_user_get_users_by_field", payload)
        if result.status != "ok":
            return result
        if not isinstance(result.data, list):
            return MoodleResult("error", "Respuesta invalida al consultar usuario Moodle.")
        users = result.data
        if not users:
            return MoodleResult("missing", "Usuario Moodle no existe.")
        return MoodleResult("ok", "Usuario Moodle encontrado.", users[0])

    def get_users_by_usernames(self, usernames: list[str]) -> MoodleResult:
        values = [str(username).strip().lower() for username in usernames if str(username).strip()]
        if not values:
            return MoodleResult("ok", "No hay usuarios para consultar.", [])
        payload = {
            "field": "username",
            **{f"values[{index}]": value for index, value in enumerate(values)},
        }
        result = self._read_call("core_user_get_users_by_field", payload)
        if result.status != "ok":
            return result
        users = result.data if isinstance(result.data, list) else []
        return MoodleResult("ok", "Usuarios Moodle obtenidos.", users)

    def get_user_courses(self, user_id: int) -> MoodleResult:
        result = self._read_call("core_enrol_get_users_courses", {"userid": user_id})
        if result.status != "ok":
            return result
        if not isinstance(result.data, list):
            return MoodleResult("error", "Respuesta invalida al consultar cursos Moodle; no equivale a una lista vacia.")
        courses = result.data
        return MoodleResult("ok", "Cursos del usuario obtenidos.", courses)

    def get_course_groups(self, course_id: int) -> MoodleResult:
        result = self._read_call("core_group_get_course_groups", {"courseid": course_id})
        if result.status != "ok":
            return result
        groups = result.data if isinstance(result.data, list) else []
        return MoodleResult("ok", "Grupos del curso obtenidos.", groups)

    def get_group_members(self, group_id: int) -> MoodleResult:
        result = self._read_call("core_group_get_group_members", {"groupids[0]": group_id})
        if result.status != "ok":
            return result
        data = result.data if isinstance(result.data, list) else []
        return MoodleResult("ok", "Miembros del grupo obtenidos.", data)

    def get_group_in_course_by_name(self, course_id: int, group_name: str) -> MoodleResult:
        groups = self.get_course_groups(course_id)
        if groups.status != "ok":
            return groups
        target = self._normalize_group_name(group_name)
        for group in groups.data if isinstance(groups.data, list) else []:
            current_name = self._normalize_group_name(group.get("name", ""))
            if current_name == target:
                return MoodleResult("ok", "Grupo encontrado.", group)
        return MoodleResult("missing", f"No existe el grupo '{group_name}' en el curso.")

    def create_group(self, course_id: int, group_name: str) -> MoodleResult:
        payload = {
            "groups[0][courseid]": course_id,
            "groups[0][name]": group_name,
            "groups[0][description]": "",
            "groups[0][descriptionformat]": 1,
        }
        result = self._call("core_group_create_groups", payload)
        if result.status != "ok":
            return result
        groups = result.data if isinstance(result.data, list) else []
        if not groups:
            return MoodleResult("error", "Moodle no devolvio el grupo creado.")
        return MoodleResult("created", "Grupo creado.", groups[0])

    def add_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        payload = {
            "members[0][groupid]": group_id,
            "members[0][userid]": user_id,
        }
        result = self._call("core_group_add_group_members", payload)
        if result.status == "ok":
            return MoodleResult("added", "Usuario agregado al grupo.", result.data)
        return result

    def add_group_members(self, memberships: list[tuple[int, int]]) -> MoodleResult:
        if not memberships:
            return MoodleResult("ok", "No hay miembros para agregar.")
        payload: dict[str, int] = {}
        for index, (group_id, user_id) in enumerate(memberships):
            payload[f"members[{index}][groupid]"] = group_id
            payload[f"members[{index}][userid]"] = user_id
        result = self._call("core_group_add_group_members", payload)
        if result.status == "ok":
            return MoodleResult("added", "Usuarios agregados al grupo.", result.data)
        return result

    def delete_group_member(self, group_id: int, user_id: int) -> MoodleResult:
        payload = {
            "members[0][groupid]": group_id,
            "members[0][userid]": user_id,
        }
        result = self._call("core_group_delete_group_members", payload)
        if result.status == "ok":
            return MoodleResult("removed", "Usuario removido del grupo.", result.data)
        return result

    def create_user(self, upn: str, first_name: str, last_name: str, email: str) -> MoodleResult:
        if not self.settings.moodle_create_missing_users:
            return MoodleResult("missing", "Usuario Moodle no existe y creacion automatica desactivada.")
        payload = {
            "users[0][username]": upn.lower(),
            "users[0][firstname]": first_name,
            "users[0][lastname]": last_name,
            "users[0][email]": upn.lower(),
            "users[0][auth]": self.settings.moodle_auth_method,
        }
        result = self._call("core_user_create_users", payload)
        if result.status != "ok":
            return result
        users = result.data if isinstance(result.data, list) else []
        if not users:
            return MoodleResult("error", "Moodle no devolvio el usuario creado.")
        return MoodleResult("created", "Usuario Moodle creado.", users[0])

    def delete_user_by_id(self, user_id: int) -> MoodleResult:
        result = self._call("core_user_delete_users", {"userids[0]": user_id})
        if result.status == "ok":
            return MoodleResult("deleted", "Usuario eliminado en Moodle.", result.data)
        return result

    def delete_user_by_username(self, username: str) -> MoodleResult:
        user = self.get_user_by_username(username.lower())
        if user.status == "missing":
            return MoodleResult("missing", "Usuario no existe en Moodle.")
        if user.status != "ok" or not isinstance(user.data, dict):
            return MoodleResult(user.status, user.message, user.data)
        return self.delete_user_by_id(int(user.data["id"]))

    def enrol_user(self, moodle_user_id: int, course_id: int, person_type: str) -> MoodleResult:
        role_id = (
            self.settings.moodle_student_role_id
            if person_type == "student"
            else self.settings.moodle_teacher_role_id
        )
        payload = {
            "enrolments[0][roleid]": role_id,
            "enrolments[0][userid]": moodle_user_id,
            "enrolments[0][courseid]": course_id,
        }
        result = self._call("enrol_manual_enrol_users", payload)
        if result.status == "ok":
            return MoodleResult("enrolled", "Usuario matriculado.", result.data)
        return result

    def enrol_users(self, enrolments: list[tuple[int, int, str]]) -> MoodleResult:
        if not enrolments:
            return MoodleResult("ok", "No hay usuarios para matricular.")
        payload: dict[str, int] = {}
        for index, (moodle_user_id, course_id, person_type) in enumerate(enrolments):
            role_id = (
                self.settings.moodle_student_role_id
                if person_type == "student"
                else self.settings.moodle_teacher_role_id
            )
            payload[f"enrolments[{index}][roleid]"] = role_id
            payload[f"enrolments[{index}][userid]"] = moodle_user_id
            payload[f"enrolments[{index}][courseid]"] = course_id
        result = self._call("enrol_manual_enrol_users", payload)
        if result.status == "ok":
            return MoodleResult("enrolled", "Usuarios matriculados.", result.data)
        return result

    def unenrol_user(self, moodle_user_id: int, course_id: int, person_type: str) -> MoodleResult:
        # Moodle only accepts the user and course for manual unenrolment.
        # The role belongs to enrolment, but is not a valid parameter here.
        payload = {
            "enrolments[0][userid]": moodle_user_id,
            "enrolments[0][courseid]": course_id,
        }
        result = self._call("enrol_manual_unenrol_users", payload)
        if result.status == "ok":
            return MoodleResult("unenrolled", "Usuario desmatriculado.", result.data)
        return result
