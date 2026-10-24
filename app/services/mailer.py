from dataclasses import dataclass
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
import smtplib

from app.config import Settings


@dataclass
class MailResult:
    status: str
    message: str = ""


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = all(
            [
                settings.smtp_host,
                settings.smtp_username,
                settings.smtp_password,
                settings.smtp_from_email,
            ]
        )

    @staticmethod
    def _repair_mojibake(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        candidate = text
        for _ in range(2):
            if any(token in candidate for token in ("Ã", "Â", "ðŸ", "â")):
                try:
                    repaired = candidate.encode("latin-1").decode("utf-8")
                except UnicodeError:
                    break
                if repaired == candidate:
                    break
                candidate = repaired
            else:
                break
        return candidate

    def send_credentials(
        self,
        to_email: str,
        upn: str,
        password: str,
        full_name: str = "",
        custom_subject: str = "",
        custom_message: str = "",
    ) -> MailResult:
        if not self.enabled:
            return MailResult("skipped", "SMTP no configurado.")
        if not to_email:
            return MailResult("skipped", "La fila no tiene correo de destino.")

        subject = self._repair_mojibake((custom_subject or "").strip() or self.settings.smtp_subject)
        template = self._repair_mojibake((custom_message or "").strip() or self.settings.smtp_message_template)
        from_name = self._repair_mojibake(self.settings.smtp_from_name)
        template_data = {
            "upn": upn,
            "password": password,
            "full_name": self._repair_mojibake(full_name),
        }
        body = template
        for key, value in template_data.items():
            body = body.replace("{" + key + "}", value or "")

        message = EmailMessage()
        message["Subject"] = str(Header(subject, "utf-8"))
        message["From"] = formataddr((from_name, self.settings.smtp_from_email), charset="utf-8")
        message["Reply-To"] = getattr(self.settings, "smtp_reply_to", "") or "v.estudiantes@fce.unrc.edu.ar"
        message["To"] = to_email
        message.set_content(body, charset="utf-8")

        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(message)
        except Exception as exc:
            return MailResult("error", str(exc))
        return MailResult("sent", "Correo enviado.")

    def send_message(self, to_email: str, subject: str, body: str) -> MailResult:
        if not self.enabled:
            return MailResult("skipped", "SMTP no configurado.")
        if not to_email:
            return MailResult("skipped", "Falta correo de destino.")

        normalized_subject = self._repair_mojibake((subject or "").strip())
        normalized_body = self._repair_mojibake(body or "")
        from_name = self._repair_mojibake(self.settings.smtp_from_name)

        message = EmailMessage()
        message["Subject"] = str(Header(normalized_subject, "utf-8"))
        message["From"] = formataddr((from_name, self.settings.smtp_from_email), charset="utf-8")
        message["Reply-To"] = getattr(self.settings, "smtp_reply_to", "") or "v.estudiantes@fce.unrc.edu.ar"
        message["To"] = to_email
        message.set_content(normalized_body, charset="utf-8")

        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(message)
        except Exception as exc:
            return MailResult("error", str(exc))
        return MailResult("sent", "Correo enviado.")
