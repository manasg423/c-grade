"""Authentication helpers for C-Grade teacher and student accounts."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass
from typing import Any

try:
    from . import database
except ImportError:  # Support the project's existing ``from database import auth`` import.
    import database  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
PBKDF2_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def _session_secret() -> bytes:
    """Load a stable signing key; create a private local key if none is configured."""
    configured = os.environ.get("CGRADE_SESSION_SECRET") or os.environ.get("SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    key_file = ROOT / ".cgrade_session_secret"
    try:
        if key_file.exists():
            secret = key_file.read_bytes()
            if len(secret) >= 32:
                return secret
        key_file.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(48)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secret)
        return secret
    except FileExistsError:
        return key_file.read_bytes()


def hash_password(password: str) -> str:
    """Hash passwords with PBKDF2-HMAC-SHA256 and a unique random salt."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must not be empty.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            algorithm, iterations, salt_text, digest_text = stored_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations_int = int(iterations)
            if iterations_int < 100_000 or iterations_int > 2_000_000:
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations_int)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    # Backward compatibility for databases created by the previous SHA-256 implementation.
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)


def verify_teacher(username: str, password: str) -> bool:
    expected_username = os.environ.get("TEACHER_USERNAME", "")
    expected_password = os.environ.get("TEACHER_PASSWORD", "")
    if not expected_username or not expected_password:
        return False
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)


def authenticate_student(roll_no: str, password: str):
    student = database.get_student_by_roll(roll_no)
    if student and verify_password(password, student["password_hash"]):
        # Upgrade legacy SHA-256 hashes after a successful login.
        if not student["password_hash"].startswith("pbkdf2_sha256$"):
            database.update_student_password(int(student["id"]), hash_password(password))
        return student
    return None


def change_student_password(student_id: int, current_password: str, new_password: str) -> bool:
    student = database.get_student_by_id(student_id)
    if not student or not verify_password(current_password, student["password_hash"]):
        return False
    database.update_student_password(student_id, hash_password(new_password))
    return True


def create_session_token(user: dict[str, Any], ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    payload = {
        "uid": int(user["id"]) if user.get("role") == "student" else 0,
        "role": str(user["role"]),
        "name": str(user.get("name", "Teacher")),
        "roll_no": str(user.get("roll_no", "")),
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_seconds),
        "nonce": secrets.token_urlsafe(12),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    sig_text = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{encoded}.{sig_text}"


def verify_session_token(token: str | None, ttl_seconds: int = SESSION_TTL_SECONDS) -> dict[str, Any] | None:
    if not token or len(token) > 4096 or "." not in token:
        return None
    try:
        encoded, supplied_sig = token.split(".", 1)
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(supplied_sig, expected_sig):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        now = int(time.time())
        if payload.get("role") not in {"teacher", "student"}:
            return None
        if not isinstance(payload.get("iat"), int) or not isinstance(payload.get("exp"), int):
            return None
        if payload["iat"] > now + 60 or payload["exp"] < now or payload["exp"] - payload["iat"] > ttl_seconds:
            return None
        if payload["role"] == "student":
            student = database.get_student_by_id(int(payload.get("uid", 0)))
            if not student:
                return None
            return {"role": "student", "id": int(student["id"]), "name": student["name"], "roll_no": student["roll_no"]}
        return {"role": "teacher", "name": "Teacher"}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        return None
