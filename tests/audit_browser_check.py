"""Optional local browser smoke test. Synthetic data, isolated DB, no external traffic."""
from pathlib import Path
import socket
import sys
import threading

from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_auditing import AuditTest


def main():
    case = AuditTest()
    AuditTest.setUpClass()
    case.setUp()
    for i in range(2, 30):
        p = {**case.p, "dni": str(99888777 + i), "upn": f"a{99888777+i}@example.invalid",
             "nombre": "Alumno de prueba con nombre compuesto", "apellido": "Apellido extenso para control visual"}
        case.create_snapshot([p])
    client = case.client()
    app = client.app
    app.mount("/static", StaticFiles(directory="app/static"))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    output = Path("data/test_artifacts/audit")
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1366, "height": 900})
            context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(f"http://127.0.0.1:{port}/") else r.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            base = f"http://127.0.0.1:{port}"
            page.goto(base + "/test-session")
            page.goto(base + "/auditoria?per_page=25")
            expect(page.get_by_role("heading", name="Auditoría", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "desktop-top.png"))
            preview = page.get_by_role("button", name="Preparar baja de seleccionados")
            expect(preview).to_be_disabled()
            page.get_by_role("checkbox", name="Seleccionar candidatos de esta página").check()
            expect(preview).to_be_enabled()
            assert page.locator('input[name="dni"]:checked').count() == 25
            page.get_by_role("table").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "desktop-table.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            preview.click()
            expect(page.get_by_role("heading", name="Bajas", exact=False)).to_be_visible()
            assert page.locator("tbody tr").count() == 25
            page.screenshot(path=str(output / "preview.png"))
            page.goto(base + "/auditoria?per_page=25&page=2")
            assert page.locator('input[name="dni"]').count() == 4
            page.get_by_role("checkbox", name="Todos los candidatos habilitados del filtro", exact=False).check()
            page.get_by_role("button", name="Preparar baja de seleccionados").click()
            expect(page.get_by_role("heading", name="Bajas", exact=False)).to_be_visible()
            expect(page.locator("tbody tr")).to_have_count(29)
            page.goto(base + "/auditoria?per_page=25")
            with page.expect_download() as download:
                page.get_by_role("link", name="Descargar Excel").click()
            assert download.value.suggested_filename.endswith(".xlsx")
            case.create_snapshot([{**case.p, "dni": "99999001", "upn": "a99999001@example.invalid",
                                   "apellido": "Protegido", "moodle_cursos_cantidad": 5}])
            for width in (1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 844})
                page.reload()
                if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                    page.screenshot(path=str(output / f"overflow-{width}.png"))
                    print(page.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right}))"))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow {width}"
                if width == 390:
                    page.screenshot(path=str(output / "mobile-top.png"))
                    page.get_by_role("heading", name="Todas las personas", exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(output / "mobile-table.png"))
            page.set_viewport_size({"width": 1366, "height": 900})
            page.get_by_role("link", name="Preinscriptos con cursos", exact=False).click()
            expect(page.get_by_role("heading", name="Preinscriptos con cursos", exact=True)).to_be_visible()
            expect(page.locator('input[name="dni"]')).to_have_count(0)
            expect(page.get_by_role("button", name="Preparar baja de seleccionados")).to_be_disabled()
            expect(page.get_by_role("checkbox", name="Todos los candidatos habilitados del filtro", exact=False)).to_be_disabled()
            expect(page.get_by_role("link", name="Ver cursos", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "protected-student.png"), full_page=True)
            page.get_by_role("link", name="Preinscriptos sin cursos/equipos", exact=False).click()
            expect(page.get_by_role("heading", name="Preinscriptos sin cursos/equipos", exact=True)).to_be_visible()
            expect(page.get_by_text("Protegido, Alumno", exact=False)).to_have_count(0)
            page.get_by_role("checkbox", name="Todos los candidatos habilitados del filtro", exact=False).check()
            page.get_by_role("button", name="Preparar baja de seleccionados").click()
            expect(page.get_by_role("heading", name="Bajas", exact=False)).to_be_visible()
            expect(page.locator("tbody tr")).to_have_count(29)
            page.goto(base + "/settings/audit")
            expect(page.get_by_label("Contraseña del servicio")).to_have_value("")
            assert "secret-test" not in page.content()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            print("Browser OK: desktop/mobile, pagination, selection, preview, Excel, secret masking; no external requests.")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        case.doCleanups()


if __name__ == "__main__":
    main()
