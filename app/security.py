from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

API_KEY_PREFIX = "dbt_"


def _load_or_create_secret() -> bytes:
    path = settings.secret_key_path
    if path.exists():
        return path.read_bytes().strip()

    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


_secret = _load_or_create_secret()
_fernet = Fernet(_secret)
_hmac_key = hashlib.sha256(_secret).digest()


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密数据库密码：secret.key 可能已更换") from exc


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hmac.new(_hmac_key, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def api_key_prefix(raw_key: str) -> str:
    return raw_key[: len(API_KEY_PREFIX) + 8]


def session_secret() -> str:
    return _secret.decode("ascii")


def check_basic_auth(username: str, password: str) -> bool:
    if not settings.admin_password:
        return True
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )
