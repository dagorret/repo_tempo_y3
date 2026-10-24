"""Read-only Moodle roster validation for Lincu previews."""
from app.services.moodle import MoodleClient


def course_usernames(moodle: MoodleClient, shortname: str) -> set[str]:
    course = moodle.get_course_by_shortname(shortname)
    if course.status != "ok" or not isinstance(course.data, dict):
        raise ValueError(f"Curso {shortname}: {course.message or course.status}")
    if course.data.get("shortname") != shortname:
        raise ValueError(f"Moodle no devolvio el nombre corto exacto {shortname}.")
    course_id = int(course.data.get("id", 0) or 0)
    if course_id <= 0:
        raise ValueError(f"Curso {shortname} sin ID valido.")
    result = moodle.get_enrolled_users(course_id)
    if result.status != "ok" or not isinstance(result.data, list):
        raise ValueError(f"Participantes de {shortname}: {result.message or result.status}")
    usernames = set()
    for user in result.data:
        if not isinstance(user, dict) or not isinstance(user.get("username"), str) or not user["username"].strip():
            raise ValueError(f"Participantes de {shortname} sin username verificable; revisar permisos Moodle.")
        if int(user.get("id", 0) or 0) <= 0:
            raise ValueError(f"Participantes de {shortname} sin ID valido.")
        username = user["username"].strip().lower()
        if username in usernames:
            raise ValueError(f"Participantes duplicados en la respuesta de {shortname}; revisar Moodle.")
        usernames.add(username)
    return usernames
