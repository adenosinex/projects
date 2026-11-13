"""Shared credential store for Mac services (mac-compute + router status dashboard).

Stores a single username + salted PBKDF2-HMAC-SHA256 password hash in
~/mac-compute/auth.json (mode 600). No plaintext password is ever stored.
First-visit setup: if the file does not exist, the service shows a setup
form; after setup, login is required.
"""
import datetime
import hashlib
import hmac
import json
import os
import secrets

AUTH_FILE = os.path.expanduser("~/mac-compute/auth.json")
ITERATIONS = 600000
DKLEN = 32
MIN_PASSWORD_LEN = 8


def credentials_exist() -> bool:
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("username")) and bool(data.get("password_hash")) and bool(data.get("salt"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def create_credentials(username: str, password: str) -> None:
    if credentials_exist():
        raise RuntimeError("credentials already exist")
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=DKLEN)
    data = {
        "username": username,
        "salt": salt.hex(),
        "password_hash": dk.hex(),
        "kdf": "pbkdf2-sha256",
        "iterations": ITERATIONS,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)


def _load():
    with open(AUTH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_credentials(username: str, password: str) -> bool:
    try:
        data = _load()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if not hmac.compare_digest(username.strip(), data.get("username", "")):
        return False
    return _check_password(data, password)


def verify_password(password: str) -> bool:
    """Check a password without a username (for API header auth)."""
    try:
        data = _load()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return _check_password(data, password)


def _check_password(data, password: str) -> bool:
    try:
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["password_hash"])
    except (KeyError, ValueError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt,
        data.get("iterations", ITERATIONS), dklen=DKLEN,
    )
    return hmac.compare_digest(dk, expected)
