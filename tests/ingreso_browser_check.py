"""Local browser smoke test: synthetic identities, isolated DB, remote clients mocked."""
from pathlib import Path
import socket
import sys
import threading

from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_ingreso import IngresoTest, inscription
from app import ingreso as logic
from app.models import IngresoStudent


def main():
    case = IngresoTest()
    case.setUp()
    with case.session() as db:
        for i in range(1, 30):
            db.add(IngresoStudent(campaign_id=case.campaign.id, dni=str(99000000+i), upn=f"a{99000000+i}@example.invalid",
                first_name="Nombre compuesto de prueba", last_name="Apellido de prueba para validar tabla", email="personal@example.invalid",
                personal_status="ok", registrations_json=logic.dump([inscription("8", commission="COMISION TGE")])))
        db.commit()
    microsoft_job = case.job("microsoft")
    case.run_job(microsoft_job)
    moodle_job = case.job("moodle")
    case.run_job(moodle_job)
    client = case.client()
    app = client.app
    app.mount("/static", StaticFiles(directory="app/static"))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    output = Path("data/test_artifacts/ingreso")
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width":1366,"height":900})
            base = f"http://127.0.0.1:{port}"
            context.route("**/*",lambda r:r.continue_() if r.request.url.startswith(base+"/") else r.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror",lambda e:errors.append(str(e)))
            page.goto(base+"/test-session")
            page.goto(base+"/ingreso?per_page=25")
            expect(page.get_by_role("heading",name="Ingreso",exact=True)).to_be_visible()
            expect(page.locator("tbody tr")).to_have_count(25)
            assert page.locator(".nav").evaluate("el => el.getBoundingClientRect().height < 58"), "Desktop menu wrapped"
            page.screenshot(path=str(output/"desktop-sial.png"),full_page=False)
            page.get_by_role("link",name="Siguiente",exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(5)
            for section in ("sial","microsoft","moodle","history"):
                page.goto(base+"/ingreso?section="+section)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),section
            page.goto(base+f"/ingreso/jobs/{moodle_job}?per_page=25")
            expect(page.locator('input[name="task_id"]:enabled')).to_have_count(0)
            expect(page.get_by_role("button",name="Ejecutar matriculaciones Moodle")).to_have_count(0)
            page.screenshot(path=str(output/"desktop-moodle-preview.png"),full_page=False)
            page.goto(base+f"/ingreso/jobs/{microsoft_job}")
            expect(page.get_by_role("button",name="Crear cuentas y enviar correos")).to_have_count(0)
            page.goto(base+f"/settings/ingreso?campaign_id={case.campaign.id}")
            expect(page.get_by_label("COMISION 02 D",exact=True)).to_have_value("I2")
            expect(page.get_by_label("COMISION 2 P",exact=True)).to_have_value("")
            page.get_by_label("COMISION 02 D",exact=True).select_option("I1")
            page.get_by_role("button",name="Agregar carrera",exact=True).click()
            expect(page.get_by_label("Nuevo ID SIAL",exact=True)).to_be_visible()
            page.get_by_role("button",name="Guardar configuración",exact=True).click()
            expect(page.get_by_role("heading",name="Ingreso",exact=True)).to_be_visible()
            page.goto(base+f"/settings/ingreso?campaign_id={case.campaign.id}")
            expect(page.get_by_label("COMISION 02 D",exact=True)).to_have_value("I1")
            page.screenshot(path=str(output/"desktop-settings.png"),full_page=True)
            page.goto(base+"/ingreso")
            with page.expect_download() as download:
                page.get_by_role("link",name="Descargar Excel",exact=True).click()
            assert download.value.suggested_filename.endswith(".xlsx")
            for width in (1024,768,390):
                page.set_viewport_size({"width":width,"height":844})
                for path in ("/ingreso?section=moodle", f"/settings/ingreso?campaign_id={case.campaign.id}", f"/ingreso/jobs/{moodle_job}"):
                    page.goto(base+path)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width,path)
                    if width == 390:
                        label = "settings" if "settings" in path else "job" if "jobs" in path else "moodle"
                        page.screenshot(path=str(output/f"mobile-{label}.png"),full_page=False)
            page.set_viewport_size({"width":1366,"height":900})
            page.goto(base+f"/settings/ingreso?campaign_id={case.campaign.id}")
            page.locator('input[name="start_date"]').fill("2025-11-01")
            page.locator('.ingreso-settings button[type="submit"]').click()
            expect(page.get_by_role("status").filter(has_text="Fechas actualizadas")).to_be_visible()
            expect(page.get_by_role("status").filter(has_text="Datos anteriores pendientes")).to_be_visible()
            case.graph.create_user.assert_not_called()
            case.moodle.create_user.assert_not_called()
            case.moodle.enrol_user.assert_not_called()
            case.mailer.send_credentials.assert_not_called()
            assert not errors,errors
            browser.close()
            print("Browser OK: SIAL, Microsoft/Moodle previews, settings, pagination, Excel, desktop/mobile. No remote writes or emails.")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        case.doCleanups()


if __name__ == "__main__":
    main()
