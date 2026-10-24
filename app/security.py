import secrets
import string

from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalUser


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def seed_local_users(db: Session, settings: Settings) -> None:
    for username in ("ivan", "marianela", "matias", "guillermo"):
        exists = db.query(LocalUser).filter(LocalUser.username == username).first()
        if exists:
            continue
        db.add(
            LocalUser(
                username=username,
                password_hash=hash_password(settings.crm_default_password),
                role="operator",
            )
        )
    db.commit()


def generate_temporary_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!#$%*-_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.islower() for ch in password)
            and any(ch.isupper() for ch in password)
            and any(ch.isdigit() for ch in password)
            and any(ch in "!#$%*-_" for ch in password)
        ):
            return password
