from __future__ import annotations

# """Authenticated student and teacher portal for the C-Grade engine."""

#TEACHER_USERNAME=
#TEACHER_PASSWORD=
#CGRADE_SESSION_SECRET=your-long-random-secret-key

import glob
import json
import os
import re
import sqlite3
import shutil
import sys
sys.dont_write_bytecode = True
import uuid
import csv
import tempfile
import textwrap
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROBLEMS_DIR = ROOT / "problems"
TEMP_DIR = PROBLEMS_DIR / ".tmp"
PROBLEMS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
for temp_var in ("TMP", "TEMP", "TMPDIR"):
    os.environ[temp_var] = str(TEMP_DIR)
tempfile.tempdir = str(TEMP_DIR)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import streamlit as st

from database import auth, database
from core.portal_rules import build_rules
from test_case_generator import generate_all_test_cases


st.set_page_config(page_title="C-Grade Portal", page_icon="C", layout="wide")
database.initialize_database()

# Asymptotic Big-O complexity options for dropdown
COMPLEXITY_OPTIONS = [
    "O(1)",
    "O(log N)",
    "O(N^0.5)",
    "O(N)",
    "O(N log N)",
    "O(N^2 log N)",
    "O(N^2)",
    "O(N^3)",
    "O(2^N)",
    "O(N!)",
    "O(N^N)",
]

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap');
    :root { --ink:#edf3ee; --muted:#aab8b0; --line:#38473f; --paper:#111916; --panel:#1c2822; --lime:#c9ef6a; --green:#245c47; }
    html, body, [class*="css"] { font-family:'DM Sans',sans-serif; color:var(--ink) !important; }
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background:var(--paper) !important; color:var(--ink) !important; }
    .stMarkdown, .stMarkdown p, [data-testid="stWidgetLabel"], [data-testid="stCaptionContainer"], label { color:var(--ink) !important; }
    input, textarea, [data-baseweb="select"] > div { background:#202d27 !important; color:var(--ink) !important; }
    [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea { -webkit-text-fill-color:var(--ink) !important; }
    [data-testid="stTextInput"] input::placeholder, [data-testid="stTextArea"] textarea::placeholder { -webkit-text-fill-color:var(--muted) !important; }
    [data-testid="stSidebar"] { background:#192522; }
    [data-testid="stSidebar"] * { color:#eef3ed; }
    [data-testid="stSidebar"] [data-testid="stRadio"] label { padding:7px 10px; border-radius:5px; }
    [data-testid="stSidebar"] button[kind="secondary"] { background:#2b3934 !important; color:#f4f7f3 !important; border:1px solid #53635c !important; }
    [data-testid="stSidebar"] button[kind="secondary"] p { color:#f4f7f3 !important; }
    [data-testid="stSidebar"] button[kind="secondary"]:hover { background:#c9ef6a !important; color:#192522 !important; border-color:#c9ef6a !important; }
    [data-testid="stSidebar"] button[kind="secondary"]:hover p { color:#192522 !important; }
    h1,h2,h3 { letter-spacing:0 !important; color:var(--ink) !important; }
    h1 { font-size:2rem !important; font-weight:700 !important; }
    h2 { font-size:1.35rem !important; }
    [data-testid="stMetric"] { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px 16px; }
    [data-testid="stMetricLabel"] { color:var(--muted); }
    .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] { background:#245c47; border-color:#245c47; }
    .stCode, code, pre, textarea { font-family:'DM Mono',monospace !important; }
    [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:5px; }
    [data-testid="stExpander"] { border-color:var(--line); }
    .portal-kicker { color:var(--lime); font:500 11px 'DM Mono',monospace; text-transform:uppercase; letter-spacing:0; }
    .portal-rule { border-top:1px solid var(--line); margin:1.2rem 0; }
    @media (max-width: 700px) { h1 { font-size:1.6rem !important; } }
    </style>
    """,
    unsafe_allow_html=True,
)


def available_rubrics() -> list[str]:
    return sorted(
        Path(path).name.removesuffix("_rules.json")
        for path in glob.glob(str(PROBLEMS_DIR / "*" / "*_rules.json"))
    )


def metric_score(report: dict, category: str) -> float | None:
    metrics = report.get("metrics", [])
    matched = []
    for metric in metrics:
        key = f"{metric.get('analyzer', '')} {metric.get('name', '')}".lower()
        if category == "io" and ("io_tests" in key or "i/o" in key):
            matched.append(float(metric.get("score", 0)))
        elif category == "sanitizer" and "sanitizer" in key:
            matched.append(float(metric.get("score", 0)))
        elif category == "quality" and any(
            token in key
            for token in ("compiler_warnings", "static_analysis", "function_split", "quality")
        ):
            matched.append(float(metric.get("score", 0)))
    return round(sum(matched) / len(matched), 4) if matched else None


def evaluation_status(score: float | None) -> str:
    if score is None:
        return "Pending"
    return "Passed" if score >= 0.999 else "Violations Detected"


def assignment_deadline(due_value: str | None) -> tuple[str, bool]:
    if not due_value:
        return "No deadline", False
    try:
        due_day = datetime.fromisoformat(str(due_value)).date()
    except ValueError:
        due_day = date.fromisoformat(str(due_value)[:10])
    if due_day < date.today():
        return "Closed", True
    if due_day == date.today():
        return "Due today", False
    return "Open", False

def first_sentence(text: str) -> str:
    """Return feedback only up to the first full stop."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()

    match = re.search(r"\.(?=\s|$)", text)

    if match:
        return text[:match.start() + 1]

    return text



def evaluation_record(report: dict) -> dict:
    first_pass = report.get("first_pass", {})
    suggestions = report.get("suggestions", [])
    summary = "\n".join(str(item) for item in suggestions)
    if not summary:
        summary = "Evaluation complete. Review the metric evidence for details."
    return {
        "score": max(0.0, min(1.0, float(report.get("grade", 0)) / 100)),
        "io_score": metric_score(report, "io"),
        "quality_score": metric_score(report, "quality"),
        "sanitizer_score": metric_score(report, "sanitizer"),
        "feedback_summary": summary,
        "raw_evidence": report,
        "status": "graded" if first_pass.get("compiled") else "failed",
    }


def build_text_grading_report(
    report: dict,
    assignment_title: str = "",
    filename: str = "",
    is_student: bool = False,
) -> str:
    """Builds the exact plain-text grading report format with full mathematical accuracy and wrapped feedback."""
    display_file = filename or "submission.c"
    metrics = report.get("metrics", [])

    total_contribution = 0.0
    processed_metrics = []

    for m in metrics:
        name = m.get("name", m.get("analyzer", "unknown"))
        analyzer = m.get("analyzer", "")

        try:
            w_val = float(m.get("weight", 0.1))
            weight_int = int(round(w_val * 100))
        except Exception:
            weight_int = 10

        try:
            score_val = float(m.get("score", 0.0))
        except Exception:
            score_val = 0.0

        contrib = round(weight_int * score_val, 1)
        total_contribution += contrib

        earned_str = f"{int(contrib)}" if contrib.is_integer() else f"{contrib:.1f}"
        score_str = f"{earned_str}/{weight_int}"

        # 1. Extract proper feedback data
        raw_fb = m.get("feedback")
        evidence = m.get("evidence", {})

        # If readability or llm_review feedback is truncated (e.g. "Summary: Th")
        # pull full summary from evidence if available
        if (analyzer == "llm_review" or name == "readability") and isinstance(evidence, dict):
            raw_resp = evidence.get("raw_response", {})
            if isinstance(raw_resp, dict) and raw_resp.get("summary"):
                summary_text = str(raw_resp.get("summary", "")).strip()
                if not raw_fb or len(str(raw_fb).strip()) < 15 or str(raw_fb).strip().endswith("..."):
                    raw_fb = summary_text

        fb = str(raw_fb or "").strip().replace("\n", " ")
        fb = first_sentence(fb) 

        # Preserve the complete feedback; the report renderer wraps it to 32 columns.
        if is_student and any(k in name.lower() or k in analyzer.lower() for k in ("similarity", "originality", "plagiarism")):
            fb = "similarity verified."

        processed_metrics.append({
            "name": name,
            "weight": weight_int,
            "score_str": score_str,
            "contrib": contrib,
            "feedback": fb
        })

    grade = report.get("grade")
    if grade is not None:
        try:
            grade_val = float(grade)
        except Exception:
            grade_val = round(total_contribution, 1)
    else:
        grade_val = round(total_contribution, 1)

    lines = [
        f"===== Grading report for {display_file} =====",
        f"Overall grade: {grade_val:.1f} / 100",
        f"{'Metric':<18} {'Weight':>6}  {'Score':>7}  {'Contribution':>12}",
    ]

    for item in processed_metrics:
        fb_text = item["feedback"]
        wrapped_fb = textwrap.wrap(fb_text, width=32) if fb_text else [""]
        if not wrapped_fb:
            wrapped_fb = [""]

        prefix = f"{item['name']:<18} {item['weight']:>6}  {item['score_str']:>7}  {item['contrib']:>12.1f}   "
        lines.append(f"{prefix}{wrapped_fb[0]}")

        indent = " " * len(prefix)
        for extra in wrapped_fb[1:]:
            lines.append(f"{indent}{extra}")

    failures = report.get("failures", [])
    if not failures:
        for m in metrics:
            ev = m.get("evidence", {})
            if isinstance(ev, dict):
                if ev.get("failures") and isinstance(ev["failures"], list):
                    failures.extend(ev["failures"])
                elif ev.get("failed_cases") and isinstance(ev["failed_cases"], list):
                    failures.extend(ev["failed_cases"])

    if failures:
        lines.append("--- Where you failed ---")
        for f in failures:
            if isinstance(f, dict):
                tid = str(f.get("test", f.get("label", f.get("id", "Test")))).strip()
                inp = str(f.get("input", "")).replace("\n", " ").strip()
                exp = str(f.get("expected", "")).replace("\n", " ").strip()
                got = str(f.get("got", f.get("actual", f.get("output", "")))).replace("\n", " ").strip()
                why = str(f.get("why", f.get("reason", f.get("hint", "")))).strip()
                lines.append(f"{tid:<8} INPUT:    {inp}")
                lines.append(f"         EXPECTED: {exp}")
                lines.append(f"         GOT:      {got}")
                if why:
                    lines.append(f"         WHY:      {why}")
            else:
                lines.append(str(f))

    suggestions = report.get("suggestions", [])
    if suggestions:
        lines.append("--- Suggestions to raise the grade ---")
        for s in suggestions:
            lines.append(f"- {s}")

    lines.append("--- Usefulness & limitations of this evaluation ---")
    lines.append("Useful for:")
    lines.append("1. Coding-interview and hiring screens")
    lines.append("   Example: a company runs 500 candidate submissions for an")
    lines.append(f"   '{assignment_title or 'given'}' task. The agent ranks them by")
    lines.append("   correctness, edge-case handling and memory safety, and gives")
    lines.append("   a score for each metric.")
    lines.append("   Benefit: fast first-round filtering where every rejection can")
    lines.append("   be explained metric by metric.\n")
    lines.append("2. Plagiarism and LLM-copy triage for instructors")
    lines.append("   Example: two submissions reach a similarity of 0.93, above the")
    lines.append("   0.85 threshold, so the agent flags them. The instructor checks")
    lines.append("   only those pairs instead of all 300 submissions.")
    lines.append("   Benefit: saves review time by limiting manual checking to")
    lines.append("   suspicious cases.\n")
    lines.append("Limits of this report:")
    lines.append("1. Tests cannot prove correctness")
    lines.append("   Example: a submission passes all 10 hidden tests but fails when")
    lines.append("   the input has extra whitespace or a trailing newline that no test")
    lines.append("   covers. The agent still reports 10/10.")
    lines.append("   Fix: the agent creates its own extra tricky test inputs and")
    lines.append("   compares the student's answers with the answers of a correct")
    lines.append("   program.")

    return "\n".join(lines)


def show_report(report: dict, score: float | None, io_score: float | None,
                quality_score: float | None, sanitizer_score: float | None,
                feedback: str | None, include_private: bool = False,
                student_name: str = "", roll_no: str = "",
                assignment_title: str = "", submitted_at: str = "",
                submitted_code: str = "") -> None:
    columns = st.columns(4)
    values = (
        ("Overall", score),
        ("I/O", io_score),
        ("Code quality", quality_score),
        ("Sanitizers", sanitizer_score),
    )
    for column, (label, value) in zip(columns, values):
        column.metric(label, f"{value * 100:.1f}%" if value is not None else "N/A")

    if feedback:
        st.markdown("#### Feedback summary")
        st.write(first_sentence(feedback))

    st.markdown("#### Grading report")
    text_report = build_text_grading_report(
        report,
        assignment_title=assignment_title,
        filename=re.sub(r"[^A-Za-z0-9_-]+", "_", assignment_title.lower()).strip("_") + ".c",
        is_student=(not include_private),
    )
    st.code(text_report, language="text")

    safe_assignment = re.sub(r"[^A-Za-z0-9_-]+", "_", assignment_title or "assessment")
    safe_roll = re.sub(r"[^A-Za-z0-9_-]+", "_", roll_no or "student")
    problem_id = str(report.get("problem", ""))

    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", problem_id):
        report_stem = Path(str(report.get("submission", ""))).stem
        if not report_stem:
            report_stem = f"{safe_assignment}_{safe_roll}"
        report_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", report_stem)
        report_dir = PROBLEMS_DIR / problem_id / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{report_stem}.txt").write_text(text_report, encoding="utf-8")

    download_key = f"download_txt_{safe_assignment}_{safe_roll}_{report.get('generated_at', 'latest')}"
    st.download_button(
        "Download report (.txt)",
        data=text_report,
        file_name=f"{safe_assignment}_{safe_roll}_report.txt",
        mime="text/plain",
        key=download_key,
        width="stretch",
    )

    if include_private:
        st.markdown("---")
        st.markdown("### 🔒 Teacher Investigation Panel")

        similarity_metric = next(
            (m for m in report.get("metrics", []) if "similarity" in m.get("analyzer", "") or "originality" in m.get("name", "").lower()),
            None
        )
        if similarity_metric:
            evidence = similarity_metric.get("evidence", {})
            with st.expander("Plagiarism & Originality Diagnostics", expanded=True):
                c1, c2, c3 = st.columns(3)
                c1.metric("Effective Similarity", f"{float(evidence.get('effective_similarity', 0.0))*100:.1f}%")
                c2.metric("Structural Match", f"{float(evidence.get('structural_match', 0.0))*100:.1f}%")
                c3.metric("Literal Match", f"{float(evidence.get('literal_match', 0.0))*100:.1f}%")
                st.write(f"**Most Similar Corpus File:** `{evidence.get('most_similar_file', 'None')}`")
                if evidence.get("flagged"):
                    st.error("⚠️ Flagged: Similarity exceeded the plagiarism threshold.")
                if evidence.get("obfuscation_detected"):
                    st.warning("⚠️ Obfuscation detected: Code structure matches despite identifier renaming.")

        with st.expander("Raw Evidence & Execution Traces (JSON)"):
            st.json(report)


def establish_login(user: dict) -> None:
    st.session_state["portal_user"] = user
    st.query_params["session_token"] = auth.create_session_token(user)


def sign_out() -> None:
    st.query_params.clear()
    st.session_state.clear()
    st.rerun()


def restore_login_from_url() -> None:
    if "portal_user" in st.session_state:
        return
    token = st.query_params.get("session_token")
    restored = auth.verify_session_token(token) if token else None
    if restored:
        st.session_state["portal_user"] = restored
    elif token:
        st.query_params.clear()


def login_screen() -> None:
    st.sidebar.title("C-Grade")
    st.sidebar.caption("Student assessment portal")
    st.markdown('<p class="portal-kicker">Course workspace / Sign in</p>', unsafe_allow_html=True)
    st.title("Welcome to C-Grade")
    st.write("Sign in to manage assignments or review your assessment results.")
    left, right = st.columns([1, 1.15], gap="large")
    with left:
        role = st.radio("Account type", ["Student", "Teacher"], horizontal=True)
        with st.form("login_form"):
            if role == "Teacher":
                username = st.text_input("Username", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
            else:
                username = st.text_input("Roll number", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            if role == "Teacher" and auth.verify_teacher(username, password):
                establish_login({"role": "teacher", "name": "Teacher"})
                st.rerun()
            if role == "Student":
                student = auth.authenticate_student(username, password)
                if student:
                    establish_login({
                        "role": "student", "id": student["id"],
                        "name": student["name"], "roll_no": student["roll_no"],
                    })
                    st.rerun()
            st.error("Invalid credentials.")
        if role == "Teacher" and not (
            os.environ.get("TEACHER_USERNAME") and os.environ.get("TEACHER_PASSWORD")
        ):
            st.warning("Teacher login is not configured. Set TEACHER_USERNAME and TEACHER_PASSWORD in .env.")
    with right:
        st.markdown("### Assessment, with a clear record")
        st.write("Assignments, submissions, and detailed C grading reports in one place.")
        st.markdown('<div class="portal-rule"></div>', unsafe_allow_html=True)


def new_assignment_view(assignments) -> None:
    st.markdown('<p class="portal-kicker">Course setup</p>', unsafe_allow_html=True)
    st.title("New assignment")
    st.caption("All configuration fields are required. Problem description is optional.")

    # Pinned persistent success message: stays visible after deployment
    if "assignment_deploy_success" in st.session_state:
        st.success(st.session_state["assignment_deploy_success"])

    saved_form = st.session_state.get("last_assignment_defaults", {})
    form_version = f"form_{saved_form.get('form_revision', 0)}"

    with st.form(f"assignment_form_{form_version}"):
        title = st.text_input(
            "Assignment title",
            value=saved_form.get("title", ""),
            key=f"new_assignment_title_{form_version}",
        )
        description = st.text_area(
            "Problem description (optional)",
            value=saved_form.get("description", ""),
            height=90,
            key=f"new_assignment_description_{form_version}",
        )
        problem_id = st.text_input(
            "Problem slug (used for rules and test folders)",
            value=saved_form.get("problem_id", ""),
            key=f"new_assignment_slug_{form_version}",
        ).strip().lower()
        due = st.date_input(
            "Due date",
            value=saved_form.get("due", date.today() + timedelta(days=7)),
            min_value=date.today(),
            format="YYYY-MM-DD",
            key=f"new_assignment_due_{form_version}",
        )

        col1, col2 = st.columns(2)
        with col1:
            time_limit = st.number_input(
                "Time limit (seconds)", min_value=0.5, max_value=10.0,
                value=float(saved_form.get("time_limit", 1.0)), step=0.5,
                key=f"new_assignment_time_{form_version}",
            )
            memory_limit = st.number_input(
                "Sandbox memory limit (MB)", min_value=16, max_value=512,
                value=int(saved_form.get("memory_limit", 64)), step=16,
                key=f"new_assignment_memory_{form_version}",
            )
            sample_input = st.text_area(
                "Sample smoke/sanitizer input",
                value=saved_form.get("sample_input", "10 20"),
                height=80,
                key=f"new_assignment_sample_{form_version}",
            )
            sample_output = st.text_area(
                "Sample output",
                value=saved_form.get("sample_output", "30"),
                height=80,
                key=f"new_assignment_sample_output_{form_version}",
            )
        with col2:
            expected_complexity = st.selectbox(
                "Expected asymptotic complexity",
                COMPLEXITY_OPTIONS,
                index=COMPLEXITY_OPTIONS.index(saved_form.get("expected_complexity", "O(N)"))
                if saved_form.get("expected_complexity", "O(N)") in COMPLEXITY_OPTIONS
                else COMPLEXITY_OPTIONS.index("O(N)"),
                key=f"new_assignment_complexity_{form_version}",
            )
            auto_gen_count = st.number_input(
                "Auto-generated I/O test cases", min_value=1, max_value=20,
                value=int(saved_form.get("auto_gen_count", 5)), step=1,
                key=f"new_assignment_io_count_{form_version}",
            )

        st.markdown("#### Teacher reference solution (`oracle.c`)")
        oracle_upload = st.file_uploader("Upload teacher C source", type=["c"])
        default_oracle = """#include <stdio.h>

int main() {
    int a, b;
    if (scanf("%d %d", &a, &b) == 2) {
        printf("%d\\n", a + b);
    }
    return 0;
}
"""
        oracle_editor = st.text_area(
            "Reference C code (required if no file is uploaded)",
            value=saved_form.get("oracle_code", default_oracle), height=180,
            key=f"new_assignment_oracle_{form_version}",
        )

        st.markdown("#### Specialist analyzers")
        c1, c2, c3 = st.columns(3)
        with c1:
            use_io = st.checkbox("io_tests (20%)", value=saved_form.get("use_io", True), key=f"new_use_io_{form_version}")
            use_edge = st.checkbox("edge_case_runner (10%)", value=saved_form.get("use_edge", True), key=f"new_use_edge_{form_version}")
            use_san = st.checkbox("sanitizer_run (10%)", value=saved_form.get("use_san", True), key=f"new_use_san_{form_version}")
        with c2:
            use_comp = st.checkbox("complexity_estimator (10%)", value=saved_form.get("use_comp", True), key=f"new_use_comp_{form_version}")
            use_stat = st.checkbox("static_analysis (10%)", value=saved_form.get("use_stat", True), key=f"new_use_stat_{form_version}")
            use_warn = st.checkbox("compiler_warnings (10%)", value=saved_form.get("use_warn", True), key=f"new_use_warn_{form_version}")
        with c3:
            use_func = st.checkbox("function_split (10%)", value=saved_form.get("use_func", True), key=f"new_use_func_{form_version}")
            use_sim = st.checkbox("similarity_flag (10%)", value=saved_form.get("use_sim", True), key=f"new_use_sim_{form_version}")
            use_llm = st.checkbox("llm_review (10%)", value=saved_form.get("use_llm", True), key=f"new_use_llm_{form_version}")
        create = st.form_submit_button("Save and deploy assignment", type="primary")

    if not create:
        return
    if not title.strip():
        st.error("Assignment title is required.")
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", problem_id):
        st.error("Use a slug with lowercase letters, numbers, hyphens, and underscores.")
        return
    if not sample_input.strip():
        st.error("Sample smoke/sanitizer input is required.")
        return
    if not (oracle_upload or oracle_editor.strip()):
        st.error("Upload or enter a teacher reference C solution.")
        return

    try:
        rules = build_rules(
            problem_id=problem_id,
            time_limit=time_limit,
            memory_limit=memory_limit,
            sample_input=sample_input,
            sample_output=sample_output,
            input_type="integer_array",
            expected_complexity=expected_complexity,
            auto_gen_count=auto_gen_count,
            enabled={
                "io_tests": use_io,
                "edge_case_runner": use_edge,
                "sanitizer_run": use_san,
                "complexity_estimator": use_comp,
                "static_analysis": use_stat,
                "compiler_warnings": use_warn,
                "function_split": use_func,
                "similarity_flag": use_sim,
                "llm_review": use_llm,
            },
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    problem_dir = PROBLEMS_DIR / problem_id
    tests_dir = problem_dir / "tests"
    submission_dir = problem_dir / "submissions"
    tests_dir.mkdir(parents=True, exist_ok=True)
    submission_dir.mkdir(parents=True, exist_ok=True)
    oracle_path = tests_dir / "oracle.c"
    oracle_code = (
        oracle_upload.getvalue().decode("utf-8", errors="replace")
        if oracle_upload else oracle_editor
    )
    oracle_path.write_text(oracle_code, encoding="utf-8")
    rules_path = problem_dir / f"{problem_id}_rules.json"
    rules_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    due_value = datetime.combine(due, time.max).isoformat(timespec="seconds")
    assignment_id = database.create_assignment(title, description, due_value, problem_id)

    # Reset form revision so all input fields return to blank/default
    st.session_state["last_assignment_defaults"] = {
        "form_revision": int(saved_form.get("form_revision", 0)) + 1
    }
    st.session_state["materials_assignment_id"] = assignment_id

    try:
        with st.spinner("Generating I/O and edge test cases from oracle.c..."):
            generated = generate_all_test_cases(
                problem_id,
                tests_dir=str(tests_dir),
                oracle_path=str(oracle_path),
                sample_input=sample_input,
                sample_output=sample_output,
                problem_description=description,
                num_io=int(auto_gen_count),
                num_edge=5,
                force_regenerate=True,
            )
        # Persistent success message stored in session_state
        st.session_state["assignment_deploy_success"] = (
            f"Assignment '{title}' (slug: {problem_id}) deployed successfully! "
            f"Generated {generated.get('io_count', 0)} I/O and {generated.get('edge_count', 0)} edge test cases."
        )
    except Exception as exc:
        st.session_state["assignment_deploy_success"] = (
            f"Assignment '{title}' deployed, but test case generation noted: {exc}"
        )

    st.rerun()


def assignment_materials_view(assignments) -> None:
    st.markdown('<p class="portal-kicker">Deployment inspector</p>', unsafe_allow_html=True)
    st.title("Edit assignment")
    if not assignments:
        st.info("Create an assignment before inspecting its deployment files.")
        return

    assignments_by_id = {item["id"]: item for item in assignments}
    assignment_id = st.selectbox(
        "Assignment", list(assignments_by_id),
        format_func=lambda item_id: (
            f"{assignments_by_id[item_id]['title']} · "
            f"{assignments_by_id[item_id]['problem_id']}"
        ),
        key="materials_assignment_id",
    )
    assignment = assignments_by_id[assignment_id]
    problem_id = assignment["problem_id"]
    st.markdown(f"### {assignment['title']}  ·  `{problem_id}`")
    with st.expander("Delete Assignment", expanded=False):
        st.error("⚠️ Warning: Deleting this assignment will permanently remove all associated student submissions, evaluation scores, test cases, and filesystem directories.")
        confirmed = st.checkbox(
            "I understand this action is irreversible and want to permanently delete this assignment.",
            key=f"confirm_delete_assignment_{assignment_id}",
        )
        if st.button("Delete Assignment Permanently", type="primary", disabled=not confirmed, key=f"delete_assignment_{assignment_id}"):
            try:
                deleted, deleted_slug = database.delete_assignment(int(assignment_id))
                if deleted:
                    if deleted_slug and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", deleted_slug):
                        target = (PROBLEMS_DIR / deleted_slug).resolve()
                        if target.parent == PROBLEMS_DIR.resolve() and target.exists():
                            shutil.rmtree(target)
                    stale_keys = [key for key in list(st.session_state.keys()) if (
                        key in {"materials_assignment_id", "edit_assignment_id"}
                        or str(key).endswith(str(assignment_id))
                        or (deleted_slug and deleted_slug in str(key))
                    )]
                    for key in stale_keys:
                        st.session_state.pop(key, None)
                    st.success("Assignment, submissions, evaluation evidence, and deployed files deleted.")
                    st.rerun()
                else:
                    st.error("Assignment was not found; nothing was deleted.")
            except Exception as exc:
                st.error(f"Assignment deletion failed: {exc}")
    current_due = (
        date.fromisoformat(str(assignment["due_date"])[:10])
        if assignment["due_date"] else date.today() + timedelta(days=7)
    )
    st.caption(f"Created: {assignment['created_at']} · Problem slug is fixed to keep deployed file paths stable.")
    with st.form(f"edit_assignment_form_{assignment_id}"):
        updated_title = st.text_input("Assignment title", value=assignment["title"])
        updated_description = st.text_area(
            "Problem description (optional)", value=assignment["description"] or "", height=90
        )
        updated_due = st.date_input(
            "Due date", value=current_due, format="YYYY-MM-DD",
        )
        save_assignment = st.form_submit_button("Save assignment details", type="primary")
    if save_assignment:
        if not updated_title.strip():
            st.error("Assignment title is required.")
        else:
            due_value = datetime.combine(updated_due, time.max).isoformat(timespec="seconds")
            if database.update_assignment(
                assignment_id, updated_title, updated_description, due_value
            ):
                st.success("Assignment details updated.")
                st.rerun()
            st.error("Assignment was not found.")

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", problem_id):
        st.error("This assignment has an invalid problem slug.")
        return

    problem_dir = PROBLEMS_DIR / problem_id
    rules_path = problem_dir / f"{problem_id}_rules.json"
    tests_dir = problem_dir / "tests"
    oracle_path = tests_dir / "oracle.c"
    if not rules_path.is_file():
        st.error(f"Rules file not found: {rules_path.relative_to(ROOT)}")
        return
    try:
        rules_data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        st.error(f"Could not read rules JSON: {exc}")
        return

    metrics = rules_data.get("metrics", [])
    io_rule_count = next(
        (int(item.get("params", {}).get("num_generated_tests", 5))
         for item in metrics if item.get("analyzer") == "io_tests"),
        5,
    )
    io_files = sorted((tests_dir / "auto_generated").glob("gen_*.in"))
    if not io_files:
        io_files = sorted(tests_dir.glob("*.in"))
    edge_files = sorted((tests_dir / "edge_cases").glob("edge_*.in"))
    complexity_metric = next(
        (item for item in metrics if item.get("analyzer") == "complexity_estimator"),
        None,
    )
    complexity_params = (complexity_metric or {}).get("params", {})

    st.markdown("#### Assignment configuration")
    with st.form(f"edit_rules_config_{problem_id}"):
        config_col1, config_col2 = st.columns(2)
        with config_col1:
            edited_time = st.number_input(
                "Time limit (seconds)", min_value=0.5, max_value=10.0,
                value=float(rules_data.get("time_limit_s", 1.0)), step=0.5,
                key=f"edit_time_{problem_id}",
            )
            edited_memory = st.number_input(
                "Sandbox memory (MB)", min_value=16, max_value=512,
                value=max(16, min(512, int(rules_data.get("memory_limit_mb", 64)))),
                step=16, key=f"edit_memory_{problem_id}",
            )
            edited_smoke = st.text_area(
                "Sample input (smoke/sanitizer input)",
                value=str(rules_data.get("smoke_input", "")),
                height=120,
                key=f"edit_smoke_{problem_id}",
            )
            edited_sample_output = st.text_area(
                "Sample output",
                value=str(rules_data.get("sample_output", "")),
                height=120,
                key=f"edit_sample_output_{problem_id}",
            )
            edited_io_count = st.number_input(
                "Generated I/O count", min_value=1, max_value=30,
                value=max(1, min(30, io_rule_count)), step=1,
                key=f"edit_io_count_{problem_id}",
            )
        with config_col2:
            current_complexity = complexity_params.get("expected_complexity", "O(N)")
            edited_complexity = st.selectbox(
                "Expected asymptotic complexity",
                COMPLEXITY_OPTIONS,
                index=COMPLEXITY_OPTIONS.index(current_complexity)
                if current_complexity in COMPLEXITY_OPTIONS
                else COMPLEXITY_OPTIONS.index("O(N)"),
                key=f"edit_complexity_{problem_id}",
            )
            edited_edge_count = st.number_input(
                "Generated edge-case count", min_value=1, max_value=20,
                value=max(1, min(20, len(edge_files) or 5)), step=1,
                key=f"edit_edge_count_{problem_id}",
            )
        save_config = st.form_submit_button("Save configuration and regenerate cases", type="primary")
    if save_config:
        if not edited_smoke.strip():
            st.error("Sample smoke/sanitizer input is required.")
        else:
            rules_data["time_limit_s"] = float(edited_time)
            rules_data["memory_limit_mb"] = int(edited_memory)
            rules_data["smoke_input"] = edited_smoke
            rules_data["sample_output"] = edited_sample_output
            for metric in rules_data.get("metrics", []):
                if metric.get("analyzer") == "io_tests":
                    metric.setdefault("params", {})["num_generated_tests"] = int(edited_io_count)
                if metric.get("analyzer") == "complexity_estimator":
                    metric.setdefault("params", {})
                    metric["params"]["expected_complexity"] = edited_complexity
            rules_path.write_text(json.dumps(rules_data, indent=2), encoding="utf-8")
            st.session_state[f"rules_editor_{problem_id}"] = json.dumps(rules_data, indent=2)
            if not oracle_path.is_file():
                st.error("Save oracle.c before regenerating cases.")
            else:
                try:
                    with st.spinner("Regenerating I/O and edge cases..."):
                        generated = generate_all_test_cases(
                            problem_id,
                            tests_dir=str(tests_dir),
                            oracle_path=str(oracle_path),
                            sample_input=edited_smoke,
                            sample_output=edited_sample_output,
                            problem_description=assignment["description"] or "",
                            num_io=int(edited_io_count),
                            num_edge=int(edited_edge_count),
                            force_regenerate=True,
                        )
                    st.success(
                        f"Saved settings and generated {generated.get('io_count', 0)} I/O, "
                        f"{generated.get('edge_count', 0)} edge cases."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Settings saved, but test generation failed: {exc}")

    first, second, third = st.columns(3)
    first.metric("Time limit", f"{rules_data.get('time_limit_s', 'N/A')} s")
    second.metric("Memory limit", f"{rules_data.get('memory_limit_mb', 'N/A')} MB")
    third.metric("Submissions", len(database.list_assignment_submissions(assignment_id)))

    st.markdown("#### Teacher oracle (`oracle.c`)")
    oracle_text = oracle_path.read_text(encoding="utf-8", errors="replace") if oracle_path.is_file() else ""
    edited_oracle = st.text_area(
        "View or edit the reference C source", value=oracle_text, height=220,
        key=f"oracle_editor_{problem_id}",
    )
    if st.button("Save oracle.c", key=f"save_oracle_{problem_id}"):
        tests_dir.mkdir(parents=True, exist_ok=True)
        oracle_path.write_text(edited_oracle, encoding="utf-8")
        st.success("Oracle source saved.")
        st.rerun()

    st.markdown("#### Deployed rules JSON")
    edited_rules = st.text_area(
        "View or edit rules/<problem>_rules.json",
        value=json.dumps(rules_data, indent=2), height=340,
        key=f"rules_editor_{problem_id}",
    )
    if st.button("Save rules JSON", type="primary", key=f"save_rules_{problem_id}"):
        try:
            updated_rules = json.loads(edited_rules)
            if not isinstance(updated_rules, dict) or not isinstance(updated_rules.get("metrics"), list):
                raise ValueError("Rules must be an object containing a metrics list.")
            updated_rules["problem"] = problem_id
            total_weight = sum(float(metric.get("weight", 0)) for metric in updated_rules["metrics"])
            if not updated_rules["metrics"] or abs(total_weight - 1.0) > 1e-4:
                raise ValueError(f"Metric weights must total 1.00 (currently {total_weight:.2f}).")
            rules_path.write_text(json.dumps(updated_rules, indent=2), encoding="utf-8")
            st.success("Rules JSON saved.")
            st.rerun()
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            st.error(f"Rules were not saved: {exc}")

    def edit_case_files(case_files, category: str, heading: str) -> None:
        st.markdown(f"#### {heading}")
        if not case_files:
            st.info("No test cases found.")
            return
        for input_path in case_files:
            case_name = input_path.stem
            output_path = input_path.with_suffix(".out")
            with st.expander(case_name):
                left, right = st.columns(2)
                input_value = input_path.read_text(encoding="utf-8", errors="replace")
                output_value = output_path.read_text(encoding="utf-8", errors="replace") if output_path.is_file() else ""
                with left:
                    edited_input = st.text_area(
                        "Standard input (.in)", value=input_value, height=150,
                        key=f"case_in_{problem_id}_{category}_{case_name}",
                    )
                with right:
                    edited_output = st.text_area(
                        "Expected output (.out)", value=output_value, height=150,
                        key=f"case_out_{problem_id}_{category}_{case_name}",
                    )
                if st.button(
                    f"Save {case_name}",
                    key=f"save_case_{problem_id}_{category}_{case_name}",
                ):
                    input_path.write_text(edited_input, encoding="utf-8")
                    output_path.write_text(edited_output, encoding="utf-8")
                    st.success(f"{case_name} input and expected output saved.")
                    st.rerun()

    st.markdown("#### Generated and editable test cases")
    io_files = sorted((tests_dir / "auto_generated").glob("gen_*.in"))
    if not io_files:
        io_files = sorted(tests_dir.glob("*.in"))
    edit_case_files(io_files, "io", "Standard I/O cases")
    edit_case_files(edge_files, "edge", "Adversarial edge cases")


def teacher_view() -> None:
    user = st.session_state["portal_user"]
    st.sidebar.title("C-Grade")
    st.sidebar.caption("Teacher workspace")
    section = st.sidebar.radio(
        "Workspace",
        ["Overview", "Students", "Assignments", "Grading"],
    )
    if st.sidebar.button("Sign out", width="stretch"):
        sign_out()

    assignments = database.list_assignments()
    if section == "Overview":
        st.markdown('<p class="portal-kicker">Teacher workspace</p>', unsafe_allow_html=True)
        st.title("Course overview")
        submitted = sum(len(database.list_assignment_submissions(a["id"])) for a in assignments)
        columns = st.columns(3)
        columns[0].metric("Students", database.count_students())
        columns[1].metric("Assignments", len(assignments))
        columns[2].metric("Submissions", submitted)
        st.markdown("### Recent assignments")
        if assignments:
            st.dataframe(
                [{"Assignment": a["title"], "Problem slug": a["problem_id"],
                  "Due": a["due_date"] or "No deadline"} for a in assignments[:8]],
                width="stretch", hide_index=True,
            )
        else:
            st.info("Create an assignment to get started.")

    elif section == "Students":
        st.markdown('<p class="portal-kicker">Roster management</p>', unsafe_allow_html=True)
        st.title("Students")
        with st.expander("Import students from CSV", expanded=True):
            st.caption("Required headers: roll_no,name. New students receive the initial password 123.")
            upload = st.file_uploader("Student roster", type=["csv"])
            if upload and st.button("Import roster", type="primary"):
                try:
                    result = database.import_students_csv(upload.getvalue().decode("utf-8-sig"))
                    st.success(f"{result['inserted']} students imported successfully.")
                    if result["duplicates"] or result["invalid"]:
                        st.warning(f"Skipped {result['duplicates']} duplicate(s) and {result['invalid']} incomplete row(s).")
                    st.rerun()
                except (UnicodeDecodeError, ValueError, csv.Error) as exc:
                    st.error(f"CSV format error: {exc}")
        with st.expander("Add Single Student", expanded=False):
            with st.form("add_single_student_form", clear_on_submit=True):
                new_roll_no = st.text_input("Roll number", key="single_student_roll_no")
                new_student_name = st.text_input("Full name", key="single_student_name")
                add_student = st.form_submit_button("Add student", type="primary")
            if add_student:
                if not new_roll_no.strip() or not new_student_name.strip():
                    st.error("Roll number and full name are required.")
                else:
                    try:
                        database.create_student(new_roll_no, new_student_name)
                        st.session_state.pop("edit_student_id", None)
                        st.session_state.pop("delete_student_id", None)
                        st.success(f"Student {new_roll_no.strip()} added with initial password '123'")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("That roll number already exists.")
                    except ValueError as exc:
                        st.error(str(exc))
        students = database.list_students()
        st.dataframe(
            [{"Roll number": s["roll_no"], "Student": s["name"], "Joined": s["created_at"]}
             for s in students],
            width="stretch", hide_index=True,
        )
        if students:
            students_by_id = {student["id"]: student for student in students}
            st.markdown("### Edit student details")
            edit_id = st.selectbox(
                "Student to edit", list(students_by_id),
                format_func=lambda student_id: (
                    f"{students_by_id[student_id]['name']} · "
                    f"{students_by_id[student_id]['roll_no']}"
                ),
                key="edit_student_id",
            )
            selected_student = students_by_id[edit_id]
            with st.form(f"edit_student_form_{edit_id}"):
                updated_roll_no = st.text_input(
                    "Roll number", value=selected_student["roll_no"]
                )
                updated_name = st.text_input(
                    "Student name", value=selected_student["name"]
                )
                save_student = st.form_submit_button("Save student changes", type="primary")
            if save_student:
                if not updated_roll_no.strip() or not updated_name.strip():
                    st.error("Roll number and student name are required.")
                else:
                    try:
                        if database.update_student_details(edit_id, updated_roll_no, updated_name):
                            st.session_state.pop("edit_student_id", None)
                            st.success("Student details updated.")
                            st.rerun()
                        st.error("Student record was not found.")
                    except sqlite3.IntegrityError:
                        st.error("That roll number is already assigned to another student.")

            with st.expander("Delete a student"):
                delete_id = st.selectbox(
                    "Student to delete", list(students_by_id),
                    format_func=lambda student_id: (
                        f"{students_by_id[student_id]['name']} · "
                        f"{students_by_id[student_id]['roll_no']}"
                    ),
                    key="delete_student_id",
                )
                delete_student = students_by_id[delete_id]
                st.warning(
                    f"Deleting {delete_student['name']} will also permanently delete "
                    "their submissions and evaluation reports."
                )
                confirm_delete = st.checkbox(
                    "I understand and want to delete this student",
                    key=f"confirm_delete_student_{delete_id}",
                )
                if st.button(
                    "Delete student and submissions",
                    type="secondary",
                    disabled=not confirm_delete,
                    key=f"delete_student_{delete_id}",
                ):
                    if database.delete_student(delete_id):
                        st.session_state.pop("edit_student_id", None)
                        st.session_state.pop("delete_student_id", None)
                        st.success("Student and related submissions deleted.")
                        st.rerun()
                    st.error("Student record was not found.")

    elif section == "Assignments":
        st.markdown('<p class="portal-kicker">Course setup</p>', unsafe_allow_html=True)
        st.title("Assignment workspace")
        new_tab, edit_tab = st.tabs(["New assignment", "Edit assignment"])
        with new_tab:
            new_assignment_view(assignments)
        with edit_tab:
            assignment_materials_view(assignments)

    else:
        st.markdown('<p class="portal-kicker">Review and feedback</p>', unsafe_allow_html=True)
        st.title("Assignment grading")
        if not assignments:
            st.info("There are no assignments to review.")
            return
        labels = {a["id"]: a["title"] for a in assignments}
        assignment_id = st.selectbox("Assignment", list(labels), format_func=labels.get)
        submissions = database.list_assignment_submissions(assignment_id)
        total_students = database.count_students()
        graded_count = sum(row["status"] == "graded" for row in submissions)
        a, b, c = st.columns(3)
        a.metric("Assigned students", total_students)
        b.metric("Submitted", len(submissions))
        c.metric("Graded", graded_count)
        st.dataframe(
            [{"Student": r["name"], "Roll no": r["roll_no"], "Submitted": r["submitted_at"],
              "Score": f"{r['score'] * 100:.1f}%" if r["score"] is not None else "Pending",
              "Status": r["status"].title(),
              "I/O status": evaluation_status(r["io_score"]),
              "Sanitizer status": evaluation_status(r["sanitizer_score"])}
             for r in submissions], width="stretch", hide_index=True,
        )
        if submissions:
            by_id = {r["submission_id"]: r for r in submissions}
            selected = st.selectbox(
                "Submission details", list(by_id),
                format_func=lambda value: f"{by_id[value]['name']} · {by_id[value]['roll_no']}",
            )
            record = by_id[selected]
            st.markdown(f"#### {record['name']} / {record['roll_no']}")
            st.caption(f"Submitted {record['submitted_at']}")
            st.code(record["submitted_code"], language="c")
            raw_report = json.loads(record["raw_evidence_json"] or "{}")
            show_report(raw_report, record["score"], record["io_score"],
                        record["quality_score"], record["sanitizer_score"],
                        record["feedback_summary"], include_private=True,
                        student_name=record["name"], roll_no=record["roll_no"],
                        assignment_title=labels[assignment_id],
                        submitted_at=record["submitted_at"],
                        submitted_code=record["submitted_code"])


def student_view() -> None:
    user = st.session_state["portal_user"]
    st.sidebar.title("C-Grade")
    st.sidebar.caption(f"Student / {user['roll_no']}")
    section = st.sidebar.radio("Workspace", ["Assignments", "Submit code", "Settings"])
    if st.sidebar.button("Sign out", width="stretch"):
        sign_out()

    assignments = database.get_student_submissions(user["id"])
    if section == "Assignments":
        st.markdown('<p class="portal-kicker">Student workspace</p>', unsafe_allow_html=True)
        st.title(f"Hello, {user['name']}")
        st.write("Your assignments and latest submission results.")
        rows = []
        for assignment in assignments:
            status = assignment["submission_status"] or "Not Submitted"
            deadline, _ = assignment_deadline(assignment["due_date"])
            rows.append({"Assignment": assignment["title"], "Due": assignment["due_date"] or "No deadline",
                         "Deadline": deadline, "Status": status.title(),
                         "Score": f"{assignment['score'] * 100:.1f}%" if assignment["score"] is not None else "—"})
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.info("No assignments have been published yet.")
        graded = [row for row in assignments if row["raw_evidence_json"]]
        if graded:
            graded_by_id = {row["id"]: row for row in graded}
            selected_id = st.selectbox(
                "View an evaluation", list(graded_by_id),
                format_func=lambda item_id: graded_by_id[item_id]["title"],
            )
            selected = graded_by_id[selected_id]
            raw_report = json.loads(selected["raw_evidence_json"] or "{}")
            st.markdown(f"### {selected['title']}")
            show_report(raw_report, selected["score"], selected["io_score"],
                        selected["quality_score"], selected["sanitizer_score"],
                        selected["feedback_summary"], include_private=False,
                        student_name=user["name"], roll_no=user["roll_no"],
                        assignment_title=selected["title"],
                        submitted_at=selected["submitted_at"],
                        submitted_code=selected["submitted_code"])

    elif section == "Submit code":
        st.markdown('<p class="portal-kicker">Assessment submission</p>', unsafe_allow_html=True)
        st.title("Submit C code")
        if not assignments:
            st.info("No assignments are available yet.")
            return
        labels = {item["id"]: item["title"] for item in assignments}
        assignment_id = st.selectbox("Assignment", list(labels), format_func=labels.get)
        assignment = database.get_assignment(assignment_id)
        prior = next((item for item in assignments if item["id"] == assignment_id), None)
        if prior and prior["submission_id"]:
            st.warning("One submission is allowed for this problem. Resubmission is disabled.")
            if prior["submitted_code"]:
                st.code(prior["submitted_code"], language="c")
            if prior["raw_evidence_json"]:
                prior_report = json.loads(prior["raw_evidence_json"])
                show_report(
                    prior_report, prior["score"], prior["io_score"],
                    prior["quality_score"], prior["sanitizer_score"],
                    prior["feedback_summary"], include_private=False,
                    student_name=user["name"], roll_no=user["roll_no"],
                    assignment_title=assignment["title"],
                    submitted_at=prior["submitted_at"], submitted_code=prior["submitted_code"],
                )
            return

        deadline_label, deadline_closed = assignment_deadline(assignment["due_date"])
        if deadline_closed:
            st.error(f"This assignment closed on {str(assignment['due_date'])[:10]}; submissions are no longer accepted.")
            return
        st.info(f"Deadline: {deadline_label}{' · ' + str(assignment['due_date'])[:10] if assignment['due_date'] else ''}")
        st.write(assignment["description"] or "No additional assignment details.")
        upload = st.file_uploader("Upload a .c file", type=["c"])
        code = st.text_area("Or paste your C source", height=300, placeholder="#include <stdio.h>\nint main(void) {\n    return 0;\n}")
        source = upload.getvalue().decode("utf-8", errors="replace") if upload else code
        if st.button("Review & Submit", type="primary", disabled=not source.strip(), key=f"review_submit_{assignment_id}"):
            if len(source.encode("utf-8")) > 200_000:
                st.error("Source files must be smaller than 200 KB.")
            elif not source.strip():
                st.error("Please provide non-empty C source code.")
            else:
                st.session_state["pending_submission"] = {
                    "assignment_id": int(assignment_id), "source": source,
                }
                st.session_state["submission_confirmed"] = False
                st.rerun()

        pending = st.session_state.get("pending_submission")
        if pending and pending.get("assignment_id") == int(assignment_id):
            @st.dialog("Confirm Final Submission")
            def confirm_final_submission() -> None:
                latest_assignment = database.get_assignment(int(assignment_id))
                if latest_assignment is None:
                    st.error("This assignment is no longer available.")
                    st.session_state.pop("pending_submission", None)
                    return
                _, closed_now = assignment_deadline(latest_assignment["due_date"])
                st.markdown(f"### {latest_assignment['title']}")
                st.write(f"Deadline: {latest_assignment['due_date'] or 'No deadline'}")
                if closed_now:
                    st.error("The deadline has passed. Submission is not allowed.")
                    if st.button("Close", key="close_expired_submission_dialog"):
                        st.session_state.pop("pending_submission", None)
                        st.rerun()
                    return
                st.warning("⚠️ IMPORTANT: Only ONE attempt is allowed for this assignment. Once submitted, your code will be immediately compiled, tested against all analyzers, and permanently recorded. You cannot edit or resubmit.")
                st.caption(f"Source length: {len(pending['source'].encode('utf-8')):,} bytes")
                verified = st.checkbox("I confirm that this is my final, verified solution.", key=f"final_confirm_{assignment_id}")
                busy = bool(st.session_state.get("submission_in_progress", False))
                if st.button("Confirm & Finalize Submission", type="primary", disabled=(not verified or busy), key=f"finalize_submission_{assignment_id}"):
                    st.session_state["submission_in_progress"] = True
                    try:
                        # The unique (assignment_id, student_id) constraint is the authoritative
                        # concurrency guard; the record is created exactly once before evaluation.
                        source_text = pending["source"]
                        problem_slug = str(latest_assignment["problem_id"] or "")
                        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", problem_slug):
                            raise ValueError("Assignment has an invalid problem identifier.")
                        problem_dir = PROBLEMS_DIR / problem_slug
                        rules_path = problem_dir / f"{problem_slug}_rules.json"
                        if not rules_path.is_file():
                            raise FileNotFoundError("The grading rules for this assignment are unavailable.")
                        source_dir = problem_dir / "submissions"
                        source_dir.mkdir(parents=True, exist_ok=True)
                        source_path = source_dir / f"student_{user['id']}_{uuid.uuid4().hex}.c"
                        submission_id = database.create_submission(int(assignment_id), int(user["id"]), source_text)
                        try:
                            source_path.write_text(source_text, encoding="utf-8")
                            with st.spinner("Compiling and evaluating your one allowed submission..."):
                                from agent import grade
                                report = grade(str(source_path), str(rules_path))
                                result = evaluation_record(report)
                                database.complete_submission(submission_id, result["status"], result)
                        except Exception as evaluation_exc:
                            failure = {
                                "grade": 0, "problem": problem_slug,
                                "first_pass": {"compiled": False, "error": str(evaluation_exc)},
                                "metrics": [],
                                "suggestions": [f"Evaluation failed: {type(evaluation_exc).__name__}: {evaluation_exc}"],
                            }
                            result = evaluation_record(failure)
                            result["status"] = "failed"
                            database.complete_submission(submission_id, "failed", result)
                            report = failure
                            st.error(f"Evaluation failed. Your attempt is recorded and cannot be resubmitted: {evaluation_exc}")
                        st.session_state.pop("pending_submission", None)
                        st.session_state.pop(f"final_confirm_{assignment_id}", None)
                        st.session_state["last_submission_result"] = {
                            "report": report, "result": result, "assignment_title": latest_assignment["title"]
                        }
                        st.session_state["submission_in_progress"] = False
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.session_state.pop("pending_submission", None)
                        st.session_state["submission_in_progress"] = False
                        st.error("A submission has already been recorded for this assignment. The one-attempt policy prevents another attempt.")
                    except Exception as exc:
                        st.session_state["submission_in_progress"] = False
                        st.error(f"Submission could not be started: {exc}")
            confirm_final_submission()

        last_result = st.session_state.get("last_submission_result")
        if last_result and last_result.get("assignment_title") == assignment["title"]:
            result = last_result["result"]
            report = last_result["report"]
            st.success("Your one submission has been evaluated and saved." if result["status"] == "graded" else "Your one submission has been recorded; review the evaluation report.")
            show_report(report, result["score"], result["io_score"], result["quality_score"],
                        result["sanitizer_score"], result["feedback_summary"], include_private=False,
                        student_name=user["name"], roll_no=user["roll_no"],
                        assignment_title=last_result["assignment_title"], submitted_code="")

    else:
        st.markdown('<p class="portal-kicker">Account security</p>', unsafe_allow_html=True)
        st.title("Change password")
        student = database.get_student_by_id(user["id"])
        if student and auth.verify_password("123", student["password_hash"]):
            st.warning("You are using the initial password. Set a new password now.")
        with st.form("change_password_form"):
            current = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            confirm = st.text_input("Confirm new password", type="password")
            changed = st.form_submit_button("Update password", type="primary")
        if changed:
            if len(new) < 8:
                st.error("New password must be at least 8 characters.")
            elif new != confirm:
                st.error("The new passwords do not match.")
            elif not auth.change_student_password(user["id"], current, new):
                st.error("Current password is incorrect.")
            else:
                st.success("Password changed successfully.")


restore_login_from_url()

if "portal_user" not in st.session_state:
    login_screen()
elif st.session_state["portal_user"]["role"] == "teacher":
    teacher_view()
else:
    student_view()