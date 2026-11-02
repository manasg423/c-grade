"""Authentication helpers for teacher and student portal accounts."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from dotenv import load_dotenv

from . import database


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_teacher(username: str, password: str) -> bool:
    expected_username = os.environ.get("TEACHER_USERNAME", "")
    expected_password = os.environ.get("TEACHER_PASSWORD", "")
    if not expected_username or not expected_password:
        return False
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password, expected_password
    )


def authenticate_student(roll_no: str, password: str):
    student = database.get_student_by_roll(roll_no)
    if student and hmac.compare_digest(
        student["password_hash"], hash_password(password)
    ):
        return student
    return None


def change_student_password(student_id: int, current_password: str, new_password: str) -> bool:
    student = database.get_student_by_id(student_id)
    if not student or not hmac.compare_digest(
        student["password_hash"], hash_password(current_password)
    ):
        return False
    database.update_student_password(student_id, hash_password(new_password))
    return True