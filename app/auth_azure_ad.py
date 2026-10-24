import logging

import httpx
import msal
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalUser
from app.security import hash_password


logger = logging.getLogger(__name__)


class AzureADAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = all(
            [
                settings.azure_tenant_id,
                settings.azure_client_id,
                settings.azure_client_secret,
            ]
        )
        self.app = None
        if self.enabled:
            self.app = msal.ConfidentialClientApplication(
                client_id=settings.azure_client_id,
                client_credential=settings.azure_client_secret,
                authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
            )

    def init_auth_code_flow(self, redirect_uri: str) -> dict:
        """Start auth flow and return flow payload for session storage."""
        if not self.enabled:
            return {}
        # MSAL adds openid/profile/offline_access automatically.
        return self.app.initiate_auth_code_flow(
            scopes=["User.Read"],
            redirect_uri=redirect_uri,
        )

    def acquire_token_by_auth_flow(self, flow: dict, request_params: dict[str, str]) -> dict | None:
        """Exchange auth code for token using the stored flow."""
        if not self.enabled:
            return None
        if not flow:
            logger.error("No pending auth flow found in session")
            return None
        try:
            result = self.app.acquire_token_by_auth_code_flow(flow, request_params)
            if "access_token" in result:
                return result
            logger.error("Error getting token: %s", result)
            return None
        except Exception as exc:
            logger.error("Error exchanging code for token: %s", exc)
            return None

    def get_user_info(self, access_token: str) -> dict | None:
        """Get user profile from Microsoft Graph."""
        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            with httpx.Client(timeout=10) as client:
                response = client.get(
                    "https://graph.microsoft.com/v1.0/me",
                    headers=headers,
                )
            if response.is_success:
                return response.json()
            logger.error("Error getting user info: %s", response.text)
            return None
        except Exception as exc:
            logger.error("Error getting user info: %s", exc)
            return None

    def validate_user(self, user_info: dict) -> tuple[bool, str]:
        """Validate domain restrictions for CRM access."""
        if not user_info:
            return False, "No se pudo obtener informacion del usuario."

        email = user_info.get("userPrincipalName", "").lower()
        if not email:
            return False, "El usuario no tiene correo principal."

        if not email.endswith(f"@{self.settings.crm_domain}"):
            return False, f"El correo debe terminar en @{self.settings.crm_domain}"

        return True, email

    def sync_user_to_db(self, db: Session, email: str, user_info: dict) -> LocalUser:
        """Create local user if needed and keep it active."""
        username = email.split("@")[0]

        user = db.query(LocalUser).filter(LocalUser.username == username).first()
        if user:
            user.active = True
            db.commit()
            return user

        user = LocalUser(
            username=username,
            password_hash=hash_password(self.settings.crm_default_password),
            role="operator",
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Usuario %s sincronizado desde Azure AD", username)
        return user
