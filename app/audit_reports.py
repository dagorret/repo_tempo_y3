"""Enrich a saved ingreso audit with read-only Microsoft 365 usage reports."""

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

import httpx
from openpyxl import Workbook

from app.audit_sources import GraphReadOnly, add_sheet, local_date, meta, now, progress, retry_get


REPORTS = {
    "Microsoft365": "getOffice365ActiveUserDetail",
    "Teams": "getTeamsUserActivityUserDetail",
}
ACTIVITY_FIELDS = [
    "Exchange Last Activity Date", "OneDrive Last Activity Date", "SharePoint Last Activity Date",
    "Skype For Business Last Activity Date", "Yammer Last Activity Date", "Teams Last Activity Date",
]
TEAM_METRICS = [
    "Team Chat Message Count", "Private Chat Message Count", "Call Count", "Meeting Count",
    "Meetings Organized Count", "Meetings Attended Count", "Urgent Messages", "Post Messages",
    "Reply Messages", "Audio Duration In Seconds", "Video Duration In Seconds", "Screen Share Duration In Seconds",
]


def boolean(value):
    v = str(value).strip().lower()
    if v in ("true", "yes", "1"):
        return True
    if v in ("false", "no", "0"):
        return False
    return None


def number(value):
    try:
        result = Decimal(str(value))
        if result.is_finite() and result >= 0:
            return float(result)
    except InvalidOperation:
        pass
    return None


def parse_date(value):
    if not value or not str(value).strip():
        return None
    return date.fromisoformat(str(value).strip())


def classify(person, office_rows, team_rows):
    if person["microsoft_estado"] == "sin_usuario":
        return "Sin cuenta Microsoft", None, "No existe en la consulta de identidad del corte local."
    dates = []
    issues = []
    metrics = []
    positive_metric = False
    for label, rows in (("Microsoft365", office_rows), ("Teams", team_rows)):
        if len(rows) != 1:
            issues.append(f"{label}: {'sin fila identificable' if not rows else 'multiples filas'}")
            continue
        row = rows[0]
        if boolean(row.get("Is Deleted")) is not False:
            issues.append(f"{label}: estado de eliminacion no confirmado como falso")
        try:
            refreshed = parse_date(row.get("Report Refresh Date"))
            if not refreshed:
                issues.append(f"{label}: sin fecha de actualizacion")
            for field in ACTIVITY_FIELDS if label == "Microsoft365" else ["Last Activity Date"]:
                if field not in row:
                    issues.append(f"{label}: falta {field}")
                    continue
                when = parse_date(row[field])
                if when:
                    if refreshed and when > refreshed:
                        issues.append(f"{label}: fecha de actividad posterior al corte")
                    dates.append((when, refreshed))
        except ValueError:
            issues.append(f"{label}: fecha invalida")
        if label == "Teams":
            if number(row.get("Report Period")) != 180:
                issues.append("Teams: periodo distinto de 180 dias")
            metrics = [number(row.get(k)) for k in TEAM_METRICS]
            if any(v is None for v in metrics):
                issues.append("Teams: contadores incompletos")
            other = boolean(row.get("Has Other Action"))
            if other is None:
                issues.append("Teams: otras acciones desconocidas")
            positive_metric = any(v is not None and v > 0 for v in metrics) or other is True
    latest = max((d for d, _ in dates), default=None)
    recent = any(refreshed and d >= refreshed - timedelta(days=179) for d, refreshed in dates)
    if recent or positive_metric:
        return "Actividad registrada (180 dias)", latest, "; ".join(issues) or "Actividad de servicio o acciones de Teams informadas."
    if issues:
        return "Datos incompletos", latest, "; ".join(issues)
    if dates:
        return "Actividad historica fuera de ventana", latest, "El informe devuelve fechas anteriores a sus 180 dias."
    return "Sin actividad informada (180 dias)", None, "Ambos informes identificaron la cuenta; sin fechas de actividad ni acciones Teams positivas."


def review(person, activity):
    if person["moodle_cursos_cantidad"] and person["moodle_cursos_cantidad"] > 0:
        return "Con cursos Moodle"
    if activity == "Actividad registrada (180 dias)":
        return "Con actividad Microsoft"
    if person["teams_cantidad"] and person["teams_cantidad"] > 0:
        return "Revisar equipos existentes"
    if person["moodle_estado"] == "sin_usuario" and person["microsoft_estado"] == "sin_usuario":
        return "Sin cuentas en ambos sistemas"
    if person["moodle_cursos_estado"] != "consultado" or person["teams_estado"] != "consultado" or activity == "Datos incompletos":
        return "Completar datos"
    if "ingresantes" in person["estados_sial"]:
        return "Revisar condicion academica"
    if activity != "Sin actividad informada (180 dias)":
        return "Revisar historial"
    if person["moodle_ultimo_acceso"] is None or person["moodle_primer_acceso"] is None:
        return "Completar datos"
    if person["moodle_ultimo_acceso"] > 0 or person["moodle_primer_acceso"] > 0:
        return "Revisar accesos historicos Moodle"
    return "Candidato a revision; no autoriza baja"


def copy_snapshot(source, folder):
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / "auditoria.sqlite3"
    if destination.exists():
        raise RuntimeError("La carpeta ya tiene una auditoria; usar otra para conservar las anteriores.")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src:
        db = sqlite3.connect(destination)
        src.backup(db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    init_reports(db)
    meta(db, "actividad_inicio", now())
    meta(db, "actividad_origen", str(source.resolve()))
    return db


def init_reports(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS informes_uso (
            informe TEXT PRIMARY KEY, endpoint TEXT, estado TEXT, consultado TEXT,
            actualizacion TEXT, periodo_dias INTEGER, filas_totales INTEGER,
            filas_coincidentes INTEGER, sha256 TEXT, columnas_json TEXT, detalle TEXT
        );
        CREATE TABLE IF NOT EXISTS uso_filas (
            informe TEXT REFERENCES informes_uso(informe), fila INTEGER, dni TEXT REFERENCES personas(dni),
            identidad TEXT, datos_json TEXT, PRIMARY KEY(informe,fila)
        );
        CREATE TABLE IF NOT EXISTS evaluacion_actividad (
            dni TEXT PRIMARY KEY REFERENCES personas(dni), actividad TEXT,
            ultima_actividad TEXT, motivo TEXT, orientacion TEXT, evaluado TEXT
        );
    """)


def download_report(reader, endpoint):
    r = reader.get(endpoint)
    if r.status_code != 302:
        raise RuntimeError(f"Graph respondio HTTP {r.status_code}; se esperaba descarga CSV")
    link = r.headers.get("Location", "")
    # Download URLs are preauthenticated; never forward the Graph bearer token.
    with httpx.Client(timeout=90, follow_redirects=False) as client:
        for _ in range(5):
            if urlparse(link).scheme != "https" or urlparse(link).username:
                raise RuntimeError("URL de descarga inesperada")
            r = retry_get(client, link)
            if r.status_code == 200:
                return r.content
            if r.status_code in (301, 302, 303, 307, 308):
                link = str(r.url.join(r.headers.get("Location", "")))
                continue
            raise RuntimeError(f"Descarga respondio HTTP {r.status_code}")
    raise RuntimeError("Demasiadas redirecciones de descarga")


def collect(db, settings=None, progress=progress):
    people = db.execute("SELECT * FROM personas").fetchall()
    by_upn = {p["upn"].strip().lower(): p for p in people}
    by_id = {p["microsoft_id"].lower(): p for p in people if p["microsoft_id"]}
    reader = GraphReadOnly(settings)
    try:
        for label, function in REPORTS.items():
            endpoint = f"/reports/{function}(period='D180')"
            meta(db, "actividad_en_curso", label)
            try:
                content = download_report(reader, endpoint)
                csv_reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
                fields = csv_reader.fieldnames or []
                expected = {"User Principal Name", "Report Refresh Date", "Is Deleted"}
                expected.update(ACTIVITY_FIELDS if label == "Microsoft365" else ["User Id", "Last Activity Date", "Report Period", "Has Other Action", *TEAM_METRICS])
                if not expected.issubset(fields):
                    raise RuntimeError("CSV no contiene las columnas esperadas")
                total = 0
                matches = []
                refreshes = set()
                for index, raw in enumerate(csv_reader):
                    total += 1
                    refreshes.add(raw.get("Report Refresh Date", ""))
                    upn = str(raw.get("User Principal Name", "")).strip().lower()
                    user_id = str(raw.get("User Id", "")).strip().lower()
                    person = by_upn.get(upn)
                    identity = "UPN"
                    if user_id and user_id in by_id:
                        id_person = by_id[user_id]
                        if person is not None and person["dni"] != id_person["dni"]:
                            raise RuntimeError("Conflicto entre ID Microsoft y UPN; no clasificar automaticamente")
                        person = id_person
                        identity = "ID Microsoft"
                    elif person is not None and user_id and person["microsoft_id"] and user_id != person["microsoft_id"].lower():
                        raise RuntimeError("UPN coincide pero ID Microsoft difiere; requiere revision")
                    if person is not None:
                        matches.append((label, index, person["dni"], identity, json.dumps(raw, ensure_ascii=False)))
                with db:
                    db.execute("DELETE FROM uso_filas WHERE informe=?", (label,))
                    db.execute("INSERT OR REPLACE INTO informes_uso VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        label, endpoint, "consultado", now(), "; ".join(sorted(refreshes)), 180,
                        total, len(matches), hashlib.sha256(content).hexdigest(), json.dumps(fields), ""))
                    db.executemany("INSERT INTO uso_filas VALUES (?,?,?,?,?)", matches)
                progress("informe_uso", informe=label, filas=total, coincidencias=len(matches), actualizado=sorted(refreshes))
            except (RuntimeError, httpx.HTTPError, ValueError) as exc:
                with db:
                    db.execute("DELETE FROM uso_filas WHERE informe=?", (label,))
                    db.execute("INSERT OR REPLACE INTO informes_uso VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        label, endpoint, "error", now(), None, 180, None, None, None, None,
                        str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
                progress("informe_uso", informe=label, estado="error", tipo=type(exc).__name__)
    finally:
        reader.client.close()
    meta(db, "actividad_fin", now())


def evaluate(db):
    raw = {}
    for r in db.execute("SELECT * FROM uso_filas"):
        raw.setdefault((r["dni"], r["informe"]), []).append(json.loads(r["datos_json"]))
    with db:
        for p in db.execute("SELECT * FROM personas").fetchall():
            activity, latest, reason = classify(p, raw.get((p["dni"], "Microsoft365"), []), raw.get((p["dni"], "Teams"), []))
            db.execute("INSERT OR REPLACE INTO evaluacion_actividad VALUES (?,?,?,?,?,?)", (
                p["dni"], activity, latest.isoformat() if latest else None, reason, review(p, activity), now()))


def export(db, folder):
    wb = Workbook()
    wb.remove(wb.active)
    all_people = db.execute("SELECT p.*,e.actividad,e.ultima_actividad,e.motivo,e.orientacion FROM personas p JOIN evaluacion_actividad e USING(dni) ORDER BY apellido,nombre,dni").fetchall()
    subset = [p for p in all_people if p["moodle_cursos_estado"] == "consultado" and p["moodle_cursos_cantidad"] == 0 and p["teams_estado"] == "consultado" and p["teams_cantidad"] == 0]
    categories = sorted({p["actividad"] for p in all_people})
    summary = []
    for group, rows in (("Todas las personas", all_people), ("Sin cursos Moodle ni equipos directos", subset)):
        for state in ("aspirantes", "preinscriptos"):
            part = [p for p in rows if state in p["estados_sial"].split("; ")]
            summary.append([group, state, len(part), *[sum(p["actividad"] == category for p in part) for category in categories]])
    add_sheet(wb, "Resumen", ["Grupo", "Estado SIAL", "Personas", *categories], summary, {1:47})
    reports = [dict(r) for r in db.execute("SELECT * FROM informes_uso")]
    metadata = dict(db.execute("SELECT clave,valor FROM metadatos"))
    notes = [
        ("Corte SIAL/Moodle/equipos", "Se conserva la auditoria del 10/09/2026; no se volvieron a consultar sus matriculas ni equipos. Fechas originales por persona en SQLite."),
        ("Consulta actividad", local_date(metadata["actividad_fin"])),
        ("Ventana solicitada", "180 dias; el corte de cada informe es su Report Refresh Date, no el momento de descarga."),
        ("Actualizacion informes", "; ".join(f"{r['informe']}: {r['actualizacion'] or r['estado']}" for r in reports)),
        ("Cruce", "UPN institucional exacto; Teams tambien se cruza por ID Microsoft. Filas ausentes o duplicadas quedan como datos incompletos."),
        ("Sin actividad informada", "Requiere una fila identificada en ambos informes, sin estado eliminado, sin fechas de uso ni contadores positivos de Teams y con contadores completos."),
        ("Actividad historica", "Una fecha anterior a la ventana informada se conserva y se senala para revisar."),
        ("Acciones de Teams", "Incluye chats privados, reuniones, llamadas y otras acciones; no se leen mensajes ni archivos."),
        ("Candidato a revision", "Sin cursos Moodle, sin equipos directos, ambos informes sin actividad y sin primer/ultimo acceso Moodle registrado. No es autorizacion para borrar."),
        ("Limitaciones", "Los informes no demuestran ausencia de todo uso historico ni falta de necesidad academica. No se dispone del ultimo inicio de sesion Entra ni de equipos accesibles solo por canales compartidos."),
        ("Decisiones", "Completar manualmente la hoja Personas. No se ejecutan bajas y las anotaciones Excel no se sincronizan automaticamente con SQLite."),
        ("Versiones", "La auditoria y el Excel anteriores se conservan. Esta copia no importa automaticamente sus anotaciones manuales."),
        ("Privacidad", "Solo se guardan las filas de los 895 DNI de la auditoria, no el reporte completo de la organizacion. Sin credenciales ni enlaces temporales de descarga."),
        ("Documentacion", "https://learn.microsoft.com/en-us/graph/api/reportroot-getoffice365activeuserdetail?view=graph-rest-1.0"),
        ("Documentacion Teams", "https://learn.microsoft.com/en-us/graph/api/reportroot-getteamsuseractivityuserdetail?view=graph-rest-1.0"),
    ]
    add_sheet(wb, "Alcance", ["Dato", "Detalle"], notes, {1:34, 2:125})
    headers = ["DNI", "Apellido", "Nombre", "Condicion SIAL", "Usuario", "Cuenta Moodle", "Cursos Moodle", "Ultimo acceso Moodle", "Cuenta Microsoft", "Equipos Teams directos", "Actividad Microsoft", "Ultima actividad informada", "Orientacion", "Detalle", "Decision Moodle", "Decision Microsoft", "Responsable", "Observaciones"]
    def row(p):
        access = "Sin acceso registrado" if p["moodle_ultimo_acceso"] == 0 else local_date(p["moodle_ultimo_acceso"])
        return [p["dni"], p["apellido"], p["nombre"], p["estados_sial"], p["upn"], p["moodle_estado"], p["moodle_cursos_cantidad"], access,
                p["microsoft_estado"], p["teams_cantidad"], p["actividad"], p["ultima_actividad"], p["orientacion"], p["motivo"], "", "", "", ""]
    widths = {1:15, 2:25, 3:28, 5:38, 11:38, 13:48, 14:65, 18:50}
    add_sheet(wb, "Personas", headers, [row(p) for p in all_people], widths)
    add_sheet(wb, "Revision_sin_cursos_ni_equipos", headers, [row(p) for p in subset], widths)
    candidates = [p for p in all_people if p["orientacion"] == "Candidato a revision; no autoriza baja"]
    add_sheet(wb, "Candidatos_a_revision", headers, [row(p) for p in candidates], widths)
    active_subset = [p for p in subset if p["actividad"] == "Actividad registrada (180 dias)"]
    add_sheet(wb, "Con_uso_sin_cursos_ni_equipos", headers, [row(p) for p in active_subset], widths)
    for label in REPORTS:
        report = db.execute("SELECT * FROM informes_uso WHERE informe=?", (label,)).fetchone()
        fields = json.loads(report["columnas_json"]) if report["columnas_json"] else []
        rows = [[r["dni"], r["identidad"], *[json.loads(r["datos_json"]).get(f) for f in fields]] for r in db.execute("SELECT * FROM uso_filas WHERE informe=? ORDER BY dni", (label,))]
        add_sheet(wb, "Uso_" + label, ["DNI", "Cruce por", *fields], rows)
    add_sheet(wb, "Equipos_Teams", ["DNI", "Equipo", "Archivado", "ID Equipo", "Consultado"],
              ([r["dni"],r["nombre"],"Desconocido" if r["archivado"] is None else "Si" if r["archivado"] else "No",r["equipo_id"],local_date(r["consultado"])]
               for r in db.execute("SELECT t.*,e.nombre,e.archivado FROM teams_pertenencias t JOIN equipos e USING(equipo_id) ORDER BY dni,nombre")), {2:70,4:40})
    add_sheet(wb, "Cursos_Moodle", ["DNI", "Codigo", "Curso", "Consulta"],
              ([r["dni"],r["codigo"],r["nombre"],local_date(r["consultado"])] for r in db.execute("SELECT * FROM moodle_cursos ORDER BY dni,codigo")), {3:70})
    add_sheet(wb, "Fuentes", ["Informe", "Estado", "Actualizado", "Consulta UTC", "Dias", "Filas organizacion", "Filas muestra", "Detalle"],
              [[r["informe"],r["estado"],r["actualizacion"],r["consultado"],r["periodo_dias"],r["filas_totales"],r["filas_coincidentes"],r["detalle"]] for r in reports])
    output = folder / "auditoria_con_actividad.xlsx"
    if output.exists():
        raise RuntimeError("El Excel ya existe; no reemplazar revisiones manuales")
    wb.save(output)
    progress("terminado", excel=str(output), total=len(all_people), sin_cursos_ni_equipos=len(subset), candidatos_revision=len(candidates), categorias=categories, resumen=summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = copy_snapshot(args.source, args.output)
    try:
        collect(db)
        evaluate(db)
        export(db, args.output)
    finally:
        db.close()


if __name__ == "__main__":
    main()
