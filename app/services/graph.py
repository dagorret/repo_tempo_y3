from dataclasses import dataclass
import re
import time

import httpx
from azure.identity import ClientSecretCredential

from app.config import Settings


@dataclass
class ServiceResult:
    status: str
    message: str = ""
    data: dict | None = None


class GraphClient:
    base_url = "https://graph.microsoft.com/v1.0"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = all(
            [
                settings.azure_tenant_id,
                settings.azure_client_id,
                settings.azure_client_secret,
            ]
        )

    def _token(self) -> str:
        credential = ClientSecretCredential(
            tenant_id=self.settings.azure_tenant_id,
            client_id=self.settings.azure_client_id,
            client_secret=self.settings.azure_client_secret,
        )
        token = credential.get_token("https://graph.microsoft.com/.default")
        return token.token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def get_user(self, upn: str) -> ServiceResult:
        if not self.enabled:
            return ServiceResult("skipped", "Azure no configurado.")
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{self.base_url}/users/{upn}", headers=self._headers())
        if response.status_code == 404:
            return ServiceResult("missing", "Usuario no existe.")
        if response.is_success:
            return ServiceResult("exists", "Usuario existente.", response.json())
        return ServiceResult("error", response.text)

    def create_user(self, upn: str, first_name: str, last_name: str, password: str) -> ServiceResult:
        if not self.enabled:
            return ServiceResult("skipped", "Azure no configurado.")

        mail_nickname = upn.split("@", 1)[0]
        payload = {
            "accountEnabled": True,
            "displayName": f"{first_name} {last_name}".strip(),
            "givenName": first_name,
            "surname": last_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": upn,
            "usageLocation": self.settings.azure_usage_location,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": password,
            },
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(f"{self.base_url}/users", headers=self._headers(), json=payload)
        if response.status_code == 409:
            return ServiceResult("exists", "Usuario existente.", response.json())
        if response.is_success:
            return ServiceResult("created", "Usuario creado.", response.json())
        return ServiceResult("error", response.text)

    def assign_license(self, user_ref: str, person_type: str) -> ServiceResult:
        if not self.enabled:
            return ServiceResult("skipped", "Azure no configurado.")

        configured_sku = (
            self.settings.azure_student_sku_id
            if person_type == "student"
            else self.settings.azure_teacher_sku_id
        )
        if not configured_sku:
            return ServiceResult("skipped", "SKU de licencia no configurado.")

        resolved = self.resolve_sku_id(configured_sku)
        if resolved.status != "ok":
            return ServiceResult("error", resolved.message)
        sku_id = resolved.data.get("skuId", "")

        payload = {"addLicenses": [{"skuId": sku_id, "disabledPlans": []}], "removeLicenses": []}
        # Retry corto por consistencia eventual luego de crear usuario.
        wait_seconds = [0, 2, 4]
        last_error = ""
        for wait in wait_seconds:
            if wait:
                time.sleep(wait)
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    f"{self.base_url}/users/{user_ref}/assignLicense",
                    headers=self._headers(),
                    json=payload,
                )
            if response.is_success:
                return ServiceResult("licensed", "Licencia asignada.", response.json())
            last_error = response.text
        return ServiceResult("error", last_error)

    def list_subscribed_skus(self) -> ServiceResult:
        if not self.enabled:
            return ServiceResult("skipped", "Azure no configurado.")
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{self.base_url}/subscribedSkus", headers=self._headers())
        if response.is_success:
            return ServiceResult("ok", "SKUs obtenidos.", response.json())
        return ServiceResult("error", response.text)

    def delete_user(self, user_ref: str) -> ServiceResult:
        if not self.enabled:
            return ServiceResult("skipped", "Azure no configurado.")
        if not user_ref:
            return ServiceResult("error", "Falta id o UPN de usuario.")
        with httpx.Client(timeout=30) as client:
            response = client.delete(f"{self.base_url}/users/{user_ref}", headers=self._headers())
        if response.status_code == 204:
            return ServiceResult("deleted", "Usuario eliminado en Microsoft 365.")
        if response.status_code == 404:
            return ServiceResult("missing", "Usuario no existe en Microsoft 365.")
        return ServiceResult("error", response.text)

    def resolve_sku_id(self, configured_value: str) -> ServiceResult:
        value = configured_value.strip()
        if not value:
            return ServiceResult("error", "SKU vacio.")

        # Acepta GUID directo en el .env.
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        ):
            return ServiceResult("ok", "SKU GUID valido.", {"skuId": value})

        # Si cargan SKU part number (por ejemplo STANDARDWOFFPACK_STUDENT),
        # lo resolvemos consultando subscribedSkus.
        listed = self.list_subscribed_skus()
        if listed.status != "ok":
            return ServiceResult("error", "No se pudo validar la licencia configurada en Azure.")
        rows = listed.data.get("value", []) if isinstance(listed.data, dict) else []
        for row in rows:
            if str(row.get("skuPartNumber", "")).strip().lower() == value.lower():
                return ServiceResult("ok", "SKU resuelto por codigo.", {"skuId": row.get("skuId", "")})
        return ServiceResult(
            "error",
            f"No se encontro la licencia '{value}'. Usa skuId GUID o skuPartNumber existente.",
        )
