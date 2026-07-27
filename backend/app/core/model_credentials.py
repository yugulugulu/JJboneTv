from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings


def encrypt_api_key(db: Session, api_key: str) -> bytes:
    encrypted = db.scalar(
        select(
            func.pgp_sym_encrypt(
                api_key,
                settings.user_model_credentials_secret,
                "cipher-algo=aes256",
            )
        )
    )
    if not isinstance(encrypted, bytes):
        raise RuntimeError("Model credential encryption failed")
    return encrypted


def decrypt_api_key(db: Session, encrypted_api_key: bytes) -> str:
    decrypted = db.scalar(
        select(
            func.pgp_sym_decrypt(
                encrypted_api_key,
                settings.user_model_credentials_secret,
            )
        )
    )
    if not isinstance(decrypted, str):
        raise RuntimeError("Stored model credential cannot be decrypted")
    return decrypted
