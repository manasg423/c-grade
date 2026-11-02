"""SQLite persistence for the student grading portal."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PORTAL_DB_PATH", ROOT / "portal.db"))
DEFAULT_PASSWORD_HASH = hashlib.sha256(b"123").hexdigest()


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def _consolidate_duplicate_problem_slugs(connection: sqlite3.Connection) -> None:
    duplicate_slugs = connection.execute(
        "SELECT problem_id FROM assignments WHERE problem_id <> '' "
        "GROUP BY problem_id HAVING COUNT(*) > 1"
    ).fetchall()
    for duplicate in duplicate_slugs:
        problem_id = duplicate["problem_id"]
        assignment_rows = connection.execute(
            """
            SELECT a.id, COUNT(s.id) AS submission_count
            FROM assignments a
            LEFT JOIN submissions s ON s.assignment_id = a.id
            WHERE a.problem_id = ?
            GROUP BY a.id
            ORDER BY submission_count DESC, a.id ASC
            """,
            (problem_id,),
        ).fetchall()
        assignment_ids = [row["id"] for row in assignment_rows]
        canonical_id = assignment_ids[0]
        placeholders = ",".join("?" for _ in assignment_ids)
        submissions = connection.execute(
            f"SELECT id, assignment_id, student_id, submitted_at FROM submissions "
            f"WHERE assignment_id IN ({placeholders}) "
            "ORDER BY submitted_at DESC, id DESC",
            assignment_ids,
        ).fetchall()

        latest_by_student: dict[int, sqlite3.Row] = {}
        for submission in submissions:
            latest_by_student.setdefault(submission["student_id"], submission)
        kept_ids = {row["id"] for row in latest_by_student.values()}
        removed_ids = [row["id"] for row in submissions if row["id"] not in kept_ids]
        if removed_ids:
            marks = ",".join("?" for _ in removed_ids)
            connection.execute(
                f"DELETE FROM submissions WHERE id IN ({marks})", removed_ids
            )
        for submission in latest_by_student.values():
            if submission["assignment_id"] != canonical_id:
                connection.execute(
                    "UPDATE submissions SET assignment_id = ? WHERE id = ?",
                    (canonical_id, submission["id"]),
                )

        redundant_ids = assignment_ids[1:]
        if redundant_ids:
            marks = ",".join("?" for _ in redundant_ids)
            connection.execute(
                f"DELETE FROM assignments WHERE id IN ({marks})", redundant_ids
            )


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.executescript(
            """
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
                status TEXT DEFAULT 'submitted' CHECK (status IN ('submitted', 'graded', 'failed')),
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(assignment_id, student_id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER UNIQUE NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                score REAL CHECK (score >= 0.0 AND score <= 1.0),
                io_score REAL,
                quality_score REAL,
                sanitizer_score REAL,
                feedback_summary TEXT,
                raw_evidence_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(assignments)")
        }
        if "problem_id" not in columns:
            connection.execute(
                "ALTER TABLE assignments ADD COLUMN problem_id TEXT NOT NULL DEFAULT ''"
            )
        _consolidate_duplicate_problem_slugs(connection)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_problem_id "
            "ON assignments(problem_id) WHERE problem_id <> ''"
        )


def import_students_csv(csv_text: str) -> dict[str, int]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames or not {"roll_no", "name"}.issubset(
        {field.strip() for field in reader.fieldnames if field}
    ):
        raise ValueError("CSV must include roll_no and name columns.")

    inserted = duplicates = invalid = 0
    with get_connection() as connection:
        for row in reader:
            normalized = {key.strip(): (value or "").strip() for key, value in row.items() if key}
            roll_no = normalized.get("roll_no", "")
            name = normalized.get("name", "")
            if not roll_no or not name:
                invalid += 1
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO students (roll_no, name, password_hash) VALUES (?, ?, ?)",
                (roll_no, name, DEFAULT_PASSWORD_HASH),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
    return {"inserted": inserted, "duplicates": duplicates, "invalid": invalid}


def list_students() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            "SELECT id, roll_no, name, created_at FROM students ORDER BY roll_no"
        ).fetchall()


def get_student_by_roll(roll_no: str) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM students WHERE roll_no = ?", (roll_no.strip(),)
        ).fetchone()


def get_student_by_id(student_id: int) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM students WHERE id = ?", (student_id,)
        ).fetchone()


def update_student_details(student_id: int, roll_no: str, name: str) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE students SET roll_no = ?, name = ? WHERE id = ?",
            (roll_no.strip(), name.strip(), student_id),
        )
        return cursor.rowcount == 1


def delete_student(student_id: int) -> bool:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
        return cursor.rowcount == 1


def update_student_password(student_id: int, password_hash: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE students SET password_hash = ? WHERE id = ?",
            (password_hash, student_id),
        )


def create_assignment(
    title: str, description: str, due_date: str | None, problem_id: str
) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO assignments (title, description, due_date, problem_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(problem_id) WHERE problem_id <> '' DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                due_date = excluded.due_date
            RETURNING id
            """,
            (title.strip(), description.strip(), due_date, problem_id),
        )
        return int(cursor.fetchone()["id"])


def update_assignment(
    assignment_id: int,
    title: str,
    description: str,
    due_date: str,
) -> bool:
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE assignments SET title = ?, description = ?, due_date = ? WHERE id = ?",
            (title.strip(), description.strip(), due_date, assignment_id),
        )
        return cursor.rowcount == 1


def list_assignments() -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM assignments ORDER BY created_at DESC, id DESC"
        ).fetchall()


def get_assignment(assignment_id: int) -> sqlite3.Row | None:
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()


def get_student_submissions(student_id: int) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            """
                 SELECT a.*, s.id AS submission_id, s.submitted_code,
                     s.status AS submission_status,
                   s.submitted_at, er.score, er.io_score, er.quality_score,
                   er.sanitizer_score, er.feedback_summary, er.raw_evidence_json
            FROM assignments a
            LEFT JOIN submissions s ON s.assignment_id = a.id AND s.student_id = ?
            LEFT JOIN evaluation_reports er ON er.submission_id = s.id
            ORDER BY a.created_at DESC, a.id DESC
            """,
            (student_id,),
        ).fetchall()


def create_submission(assignment_id: int, student_id: int, code: str) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO submissions (assignment_id, student_id, submitted_code, status) "
            "VALUES (?, ?, ?, 'submitted')",
            (assignment_id, student_id, code),
        )
        return int(cursor.lastrowid)


def complete_submission(submission_id: int, status: str, evaluation: dict) -> None:
    import json

    with get_connection() as connection:
        connection.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            (status, submission_id),
        )
        connection.execute(
            """
            INSERT INTO evaluation_reports (
                submission_id, score, io_score, quality_score, sanitizer_score,
                feedback_summary, raw_evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                evaluation["score"],
                evaluation["io_score"],
                evaluation["quality_score"],
                evaluation["sanitizer_score"],
                evaluation["feedback_summary"],
                json.dumps(evaluation["raw_evidence"], ensure_ascii=True, default=str),
            ),
        )


def update_submission_evaluation(
    submission_id: int, status: str, evaluation: dict
) -> None:
    import json

    with get_connection() as connection:
        connection.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            (status, submission_id),
        )
        cursor = connection.execute(
            """
            UPDATE evaluation_reports
            SET score = ?, io_score = ?, quality_score = ?, sanitizer_score = ?,
                feedback_summary = ?, raw_evidence_json = ?, created_at = CURRENT_TIMESTAMP
            WHERE submission_id = ?
            """,
            (
                evaluation["score"],
                evaluation["io_score"],
                evaluation["quality_score"],
                evaluation["sanitizer_score"],
                evaluation["feedback_summary"],
                json.dumps(evaluation["raw_evidence"], ensure_ascii=True, default=str),
                submission_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"Evaluation report not found for submission {submission_id}.")


def save_submission(
    assignment_id: int,
    student_id: int,
    code: str,
    status: str,
    evaluation: dict,
) -> int:
    submission_id = create_submission(assignment_id, student_id, code)
    complete_submission(submission_id, status, evaluation)
    return submission_id


def list_assignment_submissions(assignment_id: int) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT s.id AS submission_id, s.submitted_code, s.status, s.submitted_at,
                   st.name, st.roll_no, er.score, er.io_score, er.quality_score,
                   er.sanitizer_score, er.feedback_summary, er.raw_evidence_json
            FROM submissions s
            JOIN students st ON st.id = s.student_id
            LEFT JOIN evaluation_reports er ON er.submission_id = s.id
            WHERE s.assignment_id = ?
            ORDER BY s.submitted_at DESC, st.roll_no
            """,
            (assignment_id,),
        ).fetchall()


def count_students() -> int:
    with get_connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM students").fetchone()[0])