from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PORTAL_DB_PATH", ROOT / "portal.db"))
DEFAULT_PASSWORD_HASH = hashlib.sha256(b"123").hexdigest()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _consolidate_duplicate_problem_slugs(connection: sqlite3.Connection) -> None:
    duplicates = connection.execute(
        "SELECT problem_id FROM assignments WHERE problem_id <> '' GROUP BY problem_id HAVING COUNT(*) > 1"
    ).fetchall()
    for duplicate in duplicates:
        slug = duplicate["problem_id"]
        rows = connection.execute(
            "SELECT a.id, COUNT(s.id) AS n FROM assignments a LEFT JOIN submissions s ON s.assignment_id=a.id "
            "WHERE a.problem_id=? GROUP BY a.id ORDER BY n DESC,a.id ASC", (slug,)
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            continue
        canonical = ids[0]
        marks = ",".join("?" for _ in ids)
        submissions = connection.execute(
            f"SELECT id,assignment_id,student_id,submitted_at FROM submissions WHERE assignment_id IN ({marks}) "
            "ORDER BY submitted_at DESC,id DESC", ids
        ).fetchall()
        latest: dict[int, sqlite3.Row] = {}
        for item in submissions:
            latest.setdefault(int(item["student_id"]), item)
        keep = {int(item["id"]) for item in latest.values()}
        remove = [int(item["id"]) for item in submissions if int(item["id"]) not in keep]
        if remove:
            connection.execute(f"DELETE FROM submissions WHERE id IN ({','.join('?' for _ in remove)})", remove)
        for item in latest.values():
            if int(item["assignment_id"]) != canonical:
                connection.execute("UPDATE submissions SET assignment_id=? WHERE id=?", (canonical, item["id"]))
        if len(ids) > 1:
            redundant = ids[1:]
            connection.execute(f"DELETE FROM assignments WHERE id IN ({','.join('?' for _ in redundant)})", redundant)


def initialize_database() -> None:
    with get_connection() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roll_no TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            due_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            problem_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            submitted_code TEXT NOT NULL,
            status TEXT DEFAULT 'submitted' CHECK(status IN ('submitted','graded','failed')),
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assignment_id,student_id)
        );
        CREATE TABLE IF NOT EXISTS evaluation_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER UNIQUE NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            score REAL CHECK(score >= 0.0 AND score <= 1.0),
            io_score REAL, quality_score REAL, sanitizer_score REAL,
            feedback_summary TEXT, raw_evidence_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(assignments)")}
        if "problem_id" not in columns:
            connection.execute("ALTER TABLE assignments ADD COLUMN problem_id TEXT NOT NULL DEFAULT ''")
        _consolidate_duplicate_problem_slugs(connection)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_problem_id ON assignments(problem_id) WHERE problem_id <> ''")


def create_student(roll_no: str, name: str) -> int:
    roll_no, name = roll_no.strip(), name.strip()
    if not roll_no or not name:
        raise ValueError("Roll number and full name are required.")
    try:
        from . import auth
    except ImportError:
        try:
            from database import auth  # type: ignore
        except ImportError:
            import auth  # type: ignore
    password_hash = auth.hash_password("123")
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO students(roll_no,name,password_hash) VALUES(?,?,?)", (roll_no, name, password_hash)
        )
        return int(cursor.lastrowid)


def import_students_csv(csv_text: str) -> dict[str, int]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    fields = {field.strip() for field in (reader.fieldnames or []) if field}
    if not {"roll_no", "name"}.issubset(fields):
        raise ValueError("CSV must include roll_no and name columns.")
    inserted = duplicates = invalid = 0
    try:
        from . import auth
    except ImportError:
        try:
            from database import auth  # type: ignore
        except ImportError:
            import auth  # type: ignore
    with get_connection() as connection:
        for row in reader:
            normalized = {key.strip(): (value or "").strip() for key, value in row.items() if key}
            roll, name = normalized.get("roll_no", ""), normalized.get("name", "")
            if not roll or not name:
                invalid += 1
                continue
            cursor = connection.execute("INSERT OR IGNORE INTO students(roll_no,name,password_hash) VALUES(?,?,?)", (roll, name, auth.hash_password("123")))
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates, "invalid": invalid}


def list_students() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT id,roll_no,name,created_at FROM students ORDER BY roll_no").fetchall()


def get_student_by_roll(roll_no: str) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM students WHERE roll_no=?", (roll_no.strip(),)).fetchone()


def get_student_by_id(student_id: int) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()


def update_student_details(student_id: int, roll_no: str, name: str) -> bool:
    with get_connection() as connection:
        cursor = connection.execute("UPDATE students SET roll_no=?,name=? WHERE id=?", (roll_no.strip(), name.strip(), student_id))
        return cursor.rowcount == 1


def delete_student(student_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM students WHERE id=?", (student_id,))
        return cursor.rowcount == 1


def update_student_password(student_id: int, password_hash: str) -> None:
    with get_connection() as connection:
        connection.execute("UPDATE students SET password_hash=? WHERE id=?", (password_hash, student_id))


def create_assignment(title: str, description: str, due_date: str | None, problem_id: str) -> int:
    with get_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO assignments(title,description,due_date,problem_id) VALUES(?,?,?,?)
            ON CONFLICT(problem_id) WHERE problem_id <> '' DO UPDATE SET
                title=excluded.title,description=excluded.description,due_date=excluded.due_date
            RETURNING id
        """, (title.strip(), description.strip(), due_date, problem_id.strip()))
        return int(cursor.fetchone()["id"])


def update_assignment(assignment_id: int, title: str, description: str, due_date: str) -> bool:
    with get_connection() as connection:
        cursor = connection.execute("UPDATE assignments SET title=?,description=?,due_date=? WHERE id=?", (title.strip(), description.strip(), due_date, assignment_id))
        return cursor.rowcount == 1


def delete_assignment(assignment_id: int) -> tuple[bool, str | None]:
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT problem_id FROM assignments WHERE id=?", (int(assignment_id),)).fetchone()
        if row is None:
            connection.rollback()
            return False, None
        problem_id = str(row["problem_id"] or "")
        connection.execute("DELETE FROM assignments WHERE id=?", (int(assignment_id),))
        connection.commit()
        return True, problem_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_assignments() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM assignments ORDER BY created_at DESC,id DESC").fetchall()


def get_assignment(assignment_id: int) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()


def get_student_submissions(student_id: int) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("""
            SELECT a.*,s.id AS submission_id,s.submitted_code,s.status AS submission_status,s.submitted_at,
                   er.score,er.io_score,er.quality_score,er.sanitizer_score,er.feedback_summary,er.raw_evidence_json
            FROM assignments a LEFT JOIN submissions s ON s.assignment_id=a.id AND s.student_id=?
            LEFT JOIN evaluation_reports er ON er.submission_id=s.id ORDER BY a.created_at DESC,a.id DESC
        """, (student_id,)).fetchall()


def create_submission(assignment_id: int, student_id: int, code: str) -> int:
    if not code or not code.strip():
        raise ValueError("Cannot submit empty source code.")
    with get_connection() as connection:
        cursor = connection.execute("INSERT INTO submissions(assignment_id,student_id,submitted_code,status) VALUES(?,?,?,'submitted')", (assignment_id, student_id, code))
        return int(cursor.lastrowid)


def complete_submission(submission_id: int, status: str, evaluation: dict) -> None:
    if status not in {"graded", "failed", "submitted"}:
        raise ValueError("Invalid submission status.")
    evidence = json.dumps(evaluation.get("raw_evidence", {}), ensure_ascii=True, default=str)
    with get_connection() as connection:
        connection.execute("UPDATE submissions SET status=? WHERE id=?", (status, submission_id))
        connection.execute("""
            INSERT INTO evaluation_reports(submission_id,score,io_score,quality_score,sanitizer_score,feedback_summary,raw_evidence_json)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(submission_id) DO UPDATE SET
            score=excluded.score,io_score=excluded.io_score,quality_score=excluded.quality_score,
            sanitizer_score=excluded.sanitizer_score,feedback_summary=excluded.feedback_summary,
            raw_evidence_json=excluded.raw_evidence_json,created_at=CURRENT_TIMESTAMP
        """, (submission_id, evaluation.get("score"), evaluation.get("io_score"), evaluation.get("quality_score"), evaluation.get("sanitizer_score"), evaluation.get("feedback_summary"), evidence))


def update_submission_evaluation(submission_id: int, status: str, evaluation: dict) -> None:
    complete_submission(submission_id, status, evaluation)


def save_submission(assignment_id: int, student_id: int, code: str, status: str, evaluation: dict) -> int:
    with get_connection() as connection:
        cursor = connection.execute("INSERT INTO submissions(assignment_id,student_id,submitted_code,status) VALUES(?,?,?,?)", (assignment_id, student_id, code, status))
        submission_id = int(cursor.lastrowid)
        complete_submission(submission_id, status, evaluation)
        return submission_id


def list_assignment_submissions(assignment_id: int) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute("""
            SELECT s.id AS submission_id,s.submitted_code,s.status,s.submitted_at,st.name,st.roll_no,
                   er.score,er.io_score,er.quality_score,er.sanitizer_score,er.feedback_summary,er.raw_evidence_json
            FROM submissions s JOIN students st ON st.id=s.student_id
            LEFT JOIN evaluation_reports er ON er.submission_id=s.id WHERE s.assignment_id=?
            ORDER BY s.submitted_at DESC,st.roll_no
        """, (assignment_id,)).fetchall()


def count_students() -> int:
    with get_connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM students").fetchone()[0])
