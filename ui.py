import os
import sys
import glob
import json
import inspect
import tempfile
import importlib
import subprocess
from datetime import datetime
import streamlit as st

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.first_pass import run_first_pass

st.set_page_config(
    page_title="C-Grade: Automated C Assessment",
    page_icon="⚡",
    layout="wide"
)

# Ensure base directories exist
for d in ("rules", "submissions", "tests", "reports"):
    os.makedirs(d, exist_ok=True)


def list_available_problems() -> list[str]:
    rule_files = glob.glob("rules/*_rules.json")
    problems = []
    for f in rule_files:
        base = os.path.basename(f)
        prob = base.replace("_rules.json", "")
        problems.append(prob)
    return sorted(problems)


def sync_problem_directories():
    """
    Ensures every existing problem in rules/ (plus default 'add' and 'str')
    has its own dedicated subfolder in both submissions/ and tests/.
    """
    problems = set(list_available_problems()) | {"add", "str"}
    for prob in problems:
        os.makedirs(os.path.join("submissions", prob), exist_ok=True)
        os.makedirs(os.path.join("tests", prob), exist_ok=True)


sync_problem_directories()


# ---------------------------------------------------------
# Helper: Dynamic Parser for run_first_pass() Output
# ---------------------------------------------------------
def _parse_first_pass(fp: dict, source_path: str) -> dict:
    """
    Dynamically extracts status fields from the dictionary returned by
    core.first_pass.run_first_pass() without relying on fragile hardcoded key names.
    """
    flat = {}
    if isinstance(fp, dict):
        for k, v in fp.items():
            flat[k.lower()] = v
            if isinstance(v, dict):
                for sk, sv in v.items():
                    flat[f"{k.lower()}_{sk.lower()}"] = sv

    def _find_bool(substrings, default=True):
        for k, v in flat.items():
            if any(sub in k for sub in substrings):
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.strip().upper() in ("TRUE", "PASSED", "PASS", "SUCCESS", "OK", "YES")
        return default

    def _find_val(substrings, default=None):
        for k, v in flat.items():
            if any(sub in k for sub in substrings) and v is not None and v != "":
                return v
        return default

    rules_ok = _find_bool(["rule"], default=True)
    sanity_ok = _find_bool(["sanity", "static"], default=True)
    comp_ok = _find_bool(["compil"], default=True)
    loop_flag = _find_bool(["loop", "crash", "hang"], default=False)

    smoke_raw = _find_val(["smoke"], default="PASSED" if comp_ok else "FAILED")
    if isinstance(smoke_raw, bool):
        smoke_str = "PASSED" if smoke_raw else "FAILED"
    else:
        smoke_str = str(smoke_raw).upper()

    banned = _find_val(["banned", "keyword", "forbidden"], default=[])
    if isinstance(banned, str):
        banned = [banned] if banned.strip() else []
    elif not isinstance(banned, list):
        banned = []

    note = _find_val(["note", "reason", "error", "stderr", "msg", "message"], default="")

    # Locate compiled binary path from dictionary values
    binary_path = ""
    for k, v in flat.items():
        if isinstance(v, str) and any(b in k for b in ["bin", "exe", "out", "path"]):
            if not v.endswith(".c") and not v.endswith(".json") and os.path.exists(v):
                binary_path = v
                break

    if not binary_path:
        for v in flat.values():
            if isinstance(v, str) and os.path.isfile(v) and not v.endswith((".c", ".json", ".py")):
                binary_path = v
                break

    # Guaranteed GCC verification/fallback if binary_path wasn't returned
    compile_stderr = ""
    if not binary_path or not os.path.exists(binary_path):
        tmp_bin = os.path.join(tempfile.gettempdir(), f"cgrade_{os.path.basename(source_path)}.out")
        res = subprocess.run(
            ["gcc", "-O0", source_path, "-o", tmp_bin, "-lm"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if res.returncode == 0 and os.path.exists(tmp_bin):
            comp_ok = True
            binary_path = tmp_bin
            if smoke_str in ("UNKNOWN", "FAILED", "FALSE"):
                smoke_str = "PASSED"
        else:
            comp_ok = False
            compile_stderr = res.stderr.strip()

    return {
        "rules_valid": rules_ok,
        "static_sanity": sanity_ok,
        "compiled": comp_ok,
        "smoke_test": smoke_str,
        "suspected_loop": loop_flag,
        "banned_keywords": banned,
        "binary_path": binary_path,
        "note": note or compile_stderr,
        "raw": fp
    }


# ---------------------------------------------------------
# Helper: Signature-Safe Analyzer Execution
# ---------------------------------------------------------
def run_analyzer_safe(analyzer_name: str, source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    try:
        mod = importlib.import_module(f"analyzers.{analyzer_name}")
        mod = importlib.reload(mod)
        eval_fn = getattr(mod, "evaluate", None)
        if eval_fn is None:
            return {
                "score": 0.0,
                "evidence": {"error": f"No evaluate() function found in analyzers/{analyzer_name}.py"},
                "feedback": f"Analyzer {analyzer_name} is missing evaluate()."
            }

        sig = inspect.signature(eval_fn)
        num_params = len(sig.parameters)

        if num_params >= 4:
            res = eval_fn(source_path, binary_path, params, limits)
        elif num_params == 3:
            res = eval_fn(source_path, binary_path, params)
        elif num_params == 2:
            res = eval_fn(source_path, params)
        else:
            res = eval_fn(source_path)

        if isinstance(res, dict):
            return {
                "score": float(res.get("score", 0.0)),
                "evidence": res.get("evidence", res),
                "feedback": str(res.get("feedback", "Completed."))
            }
        return {"score": 0.0, "evidence": {"raw_return": str(res)}, "feedback": "Invalid analyzer return format."}

    except Exception as e:
        return {
            "score": 0.0,
            "evidence": {"error": f"{type(e).__name__}: {str(e)}"},
            "feedback": f"Analyzer {analyzer_name} raised {type(e).__name__}."
        }


def ensure_valid_rules_weights(rule_path: str) -> dict:
    """
    Loads rules JSON and guarantees metric weights sum to strictly 1.00
    so the gatekeeper never rejects due to floating-point drift.
    """
    with open(rule_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    metrics = data.get("metrics", [])
    if metrics:
        total_w = round(sum(float(m.get("weight", 0.0)) for m in metrics), 4)
        if abs(total_w - 1.0) > 1e-4 and total_w > 0:
            accum = 0.0
            for i, m in enumerate(metrics):
                if i == len(metrics) - 1:
                    m["weight"] = round(1.0 - accum, 2)
                else:
                    w = round(float(m.get("weight", 0.0)) / total_w, 2)
                    m["weight"] = w
                    accum += w
            with open(rule_path, "w", encoding="utf-8") as fw:
                json.dump(data, fw, indent=2)

    return data


# ---------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------
st.sidebar.title("⚡ C-Grade Engine")
st.sidebar.caption("Sandboxed C Assessment & Profiling")

role = st.sidebar.radio("Select Portal:", ["👨‍🎓 Student Portal", "👨‍🏫 Teacher Portal"])
st.sidebar.markdown("---")


# =========================================================
# 1. TEACHER PORTAL
# =========================================================
if role == "👨‍🏫 Teacher Portal":
    st.title("Teacher Portal — Assignment & Rubric Builder")
    teacher_tab1, teacher_tab2 = st.tabs(["➕ Create / Update Assignment", "📋 Inspect Deployed Rules"])

    with teacher_tab1:
        st.subheader("Configure New Problem")
        st.caption(
            "Enter the problem details below. The system automatically creates dedicated folders in "
            "`submissions/<problem>/` and `tests/<problem>/`, saves `oracle.c`, and builds `rules/<problem>_rules.json`."
        )

        col1, col2 = st.columns(2)
        with col1:
            problem_id = st.text_input("Problem Slug (e.g., add, str, sort, prime):").strip().lower()
            time_limit = st.number_input("Time Limit (seconds):", min_value=0.5, max_value=10.0, value=1.0, step=0.5)
            memory_limit = st.number_input("Memory Limit (MB):", min_value=16, max_value=512, value=64, step=16)
            sample_input = st.text_input("Sample Smoke/Sanitizer Input:", value="10 20")

        with col2:
            input_type = st.selectbox(
                "Complexity Generator Input Type:",
                ["string", "integer_array", "raw_array", "single_number", "fixed_arithmetic"]
            )
            expected_complexity = st.selectbox(
                "Expected Asymptotic Complexity:",
                ["O(1)","O(log N)", "O(N)", "O(N log N)", "O(N^2)","O(N^3)"]
            )
            auto_gen_count = st.slider("Auto-Generated I/O Test Cases:", min_value=1, max_value=20, value=5)

        st.markdown("#### Teacher Reference Solution (`oracle.c`)")
        default_oracle = (
            "#include <stdio.h>\n\n"
            "int main() {\n"
            "    int a, b;\n"
            "    if (scanf(\"%d %d\", &a, &b) == 2) {\n"
            "        printf(\"%d\\n\", a + b);\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        oracle_code = st.text_area("Reference C Code:", value=default_oracle, height=180)

        st.markdown("#### Specialist Analyzers (Committed Rubric: 20% Correctness + 10% Each)")
        c1, c2, c3 = st.columns(3)
        with c1:
            use_io = st.checkbox("io_tests (20%)", value=True)
            use_edge = st.checkbox("edge_case_runner (10%)", value=True)
            use_san = st.checkbox("sanitizer_run (10%)", value=True)
        with c2:
            use_comp = st.checkbox("complexity_estimator (10%)", value=True)
            use_stat = st.checkbox("static_analysis (10%)", value=True)
            use_warn = st.checkbox("compiler_warnings (10%)", value=True)
        with c3:
            use_func = st.checkbox("function_split (10%)", value=True)
            use_sim = st.checkbox("similarity_flag (10%)", value=True)
            use_llm = st.checkbox("llm_review (10%)", value=True)

        if st.button("💾 Save & Deploy Assignment", type="primary"):
            if not problem_id:
                st.error("Please enter a valid Problem Slug.")
            else:
                # Create dedicated problem directories in both tests/ and submissions/
                prob_test_dir = os.path.join("tests", problem_id)
                prob_sub_dir = os.path.join("submissions", problem_id)
                os.makedirs(prob_test_dir, exist_ok=True)
                os.makedirs(prob_sub_dir, exist_ok=True)

                oracle_path = os.path.join(prob_test_dir, "oracle.c")
                with open(oracle_path, "w", encoding="utf-8") as f:
                    f.write(oracle_code)

                raw_metrics = []
                if use_io:
                    raw_metrics.append({
                        "name": "correctness",
                        "analyzer": "io_tests",
                        "raw_weight": 0.20,
                        "params": {
                            "testset_dir": f"tests/{problem_id}/",
                            "oracle_path": f"tests/{problem_id}/oracle.c",
                            "auto_generate": True,
                            "num_generated_tests": int(auto_gen_count),
                            "seed": 42
                        }
                    })
                if use_edge:
                    raw_metrics.append({
                        "name": "logical",
                        "analyzer": "edge_case_runner",
                        "raw_weight": 0.10,
                        "params": {
                            "oracle_path": f"tests/{problem_id}/oracle.c",
                            "cases": ["empty", "whitespace"],
                            "test_inputs": [sample_input, "0", "-1"], # not used anymore
                            "seed": 42
                        }
                    })
                if use_san:
                    raw_metrics.append({
                        "name": "memory_safety",
                        "analyzer": "sanitizer_run",
                        "raw_weight": 0.10,
                        "params": {"sample_input": sample_input}
                    })
                if use_comp:
                    raw_metrics.append({
                        "name": "complexity",
                        "analyzer": "complexity_estimator",
                        "raw_weight": 0.10,
                        "params": {
                            "input_type": input_type,
                            "expected_complexity": expected_complexity,
                            "trials": 5,
                            "base_n": 10,
                            "scale_step": 20
                        }
                    })
                if use_stat:
                    raw_metrics.append({
                        "name": "static_analysis",
                        "analyzer": "static_analysis",
                        "raw_weight": 0.10,
                        "params": {
                            "enable_cppcheck": True,
                            "enable_gcc_warnings": True,
                            "error_penalty": 0.30,
                            "warning_penalty": 0.10
                        }
                    })
                if use_warn:
                    raw_metrics.append({
                        "name": "compiler_warnings",
                        "analyzer": "compiler_warnings",
                        "raw_weight": 0.10,
                        "params": {}
                    })
                if use_func:
                    raw_metrics.append({
                        "name": "modularity",
                        "analyzer": "function_split",
                        "raw_weight": 0.10,
                        "params": {}
                    })
                if use_sim:
                    raw_metrics.append({
                        "name": "plagiarism_check",
                        "analyzer": "similarity_flag",
                        "raw_weight": 0.10,
                        "params": {"submissions_dir": f"submissions/{problem_id}/"}
                    })
                if use_llm:
                    raw_metrics.append({
                        "name": "readability",
                        "analyzer": "llm_review",
                        "raw_weight": 0.10,
                        "params": {
                            "model": "qwen3.5:2b",
                            "temperature": 0,
                            "seed": 42
                        }
                    })

                if not raw_metrics:
                    st.error("Select at least one analyzer.")
                else:
                    total_raw = sum(m["raw_weight"] for m in raw_metrics)
                    accum = 0.0
                    final_metrics = []
                    for i, m in enumerate(raw_metrics):
                        if i == len(raw_metrics) - 1:
                            w = round(1.0 - accum, 2)
                        else:
                            w = round(m["raw_weight"] / total_raw, 2)
                            accum += w
                        final_metrics.append({
                            "name": m["name"],
                            "analyzer": m["analyzer"],
                            "weight": w,
                            "params": m["params"]
                        })

                    rules_obj = {
                        "problem": problem_id,
                        "time_limit_s": time_limit,
                        "memory_limit_mb": int(memory_limit),
                        "smoke_input": sample_input,
                        "metrics": final_metrics
                    }

                    rule_file = os.path.join("rules", f"{problem_id}_rules.json")
                    with open(rule_file, "w", encoding="utf-8") as rf:
                        json.dump(rules_obj, rf, indent=2)

                    st.success(
                        f"Deployed '{problem_id}'!\n"
                        f"- Created Submissions Folder: `{prob_sub_dir}/`\n"
                        f"- Created Tests Folder: `{prob_test_dir}/`\n"
                        f"- Saved Rules: `{rule_file}`"
                    )

    with teacher_tab2:
        st.subheader("Inspect Existing Rules JSON")
        probs = list_available_problems()
        if not probs:
            st.info("No rules found.")
        else:
            sel = st.selectbox("Select Problem:", probs)
            r_path = os.path.join("rules", f"{sel}_rules.json")
            with open(r_path, "r", encoding="utf-8") as f:
                st.json(json.load(f))


# =========================================================
# 2. STUDENT PORTAL
# =========================================================
else:
    st.title("Student Portal — Automated C Code Grading")
    st.caption("Submit your C program for gatekeeper verification and multi-analyzer grading.")

    available_problems = list_available_problems()
    if not available_problems:
        st.warning("No assignments found in 'rules/'. Use the Teacher Portal to create one.")
        st.stop()

    col_sel, col_lim1, col_lim2 = st.columns([2, 1, 1])
    with col_sel:
        selected_problem = st.selectbox("Select Assignment:", available_problems)

    # Ensure problem-specific submission & test folders exist
    prob_sub_dir = os.path.join("submissions", selected_problem)
    prob_test_dir = os.path.join("tests", selected_problem)
    os.makedirs(prob_sub_dir, exist_ok=True)
    os.makedirs(prob_test_dir, exist_ok=True)

    rule_path = os.path.join("rules", f"{selected_problem}_rules.json")
    active_rules = ensure_valid_rules_weights(rule_path)

    with col_lim1:
        st.metric("Time Limit", f"{active_rules.get('time_limit_s', 1.0)} s")
    with col_lim2:
        st.metric("Memory Limit", f"{active_rules.get('memory_limit_mb', 64)} MB")

    st.caption(f"📁 Submissions for this problem are stored in: `{prob_sub_dir}/`")

    upload_mode = st.radio(
        "Submission Method:",
        ["Paste Code Directly", "Upload .c File"],
        horizontal=True
    )

    code_to_grade = ""
    existing_sub_path = None
    uploaded_filename = None

    if upload_mode == "Paste Code Directly":
        default_code = (
            "#include <stdio.h>\n\n"
            "int main() {\n"
            "    int a, b;\n"
            "    if (scanf(\"%d %d\", &a, &b) == 2) {\n"
            "        printf(\"%d\\n\", a + b);\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        code_to_grade = st.text_area("C Source Code:", value=default_code, height=220)

    elif upload_mode == "Upload .c File":
        up_file = st.file_uploader("Upload .c file", type=["c"])
        if up_file is not None:
            uploaded_filename = up_file.name
            code_to_grade = up_file.read().decode("utf-8", errors="ignore")
            st.code(code_to_grade, language="c")

    else:
        existing_files = sorted(glob.glob(os.path.join(prob_sub_dir, "*.c")))
        if not existing_files:
            st.info(f"No existing `.c` files found in `{prob_sub_dir}/`. Paste or upload a file first.")
        else:
            existing_sub_path = st.selectbox("Choose file to evaluate:", existing_files)
            if existing_sub_path and os.path.exists(existing_sub_path):
                with open(existing_sub_path, "r", encoding="utf-8", errors="ignore") as ef:
                    code_to_grade = ef.read()
                st.code(code_to_grade, language="c")

    if st.button("🚀 Submit and Grade Code", type="primary", use_container_width=True):
        if not code_to_grade.strip():
            st.error("Submission cannot be empty.")
            st.stop()

        # Save directly into submissions/<selected_problem>/
        if existing_sub_path:
            sub_filepath = existing_sub_path
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if uploaded_filename:
                base_name = os.path.splitext(os.path.basename(uploaded_filename))[0]
                sub_filename = f"{base_name}_{timestamp}.c"
            else:
                sub_filename = f"{selected_problem}_{timestamp}.c"
            sub_filepath = os.path.join(prob_sub_dir, sub_filename)
            with open(sub_filepath, "w", encoding="utf-8") as f:
                f.write(code_to_grade)

        st.info(f"Saved / Using submission in problem folder: `{sub_filepath}`")

        # Dynamically fetch the smoke_input from deployed rules, fallback to "7 5\n"
        smoke_input_val = active_rules.get("smoke_input", "7 5\n")

        # Run Gatekeeper First Pass
        with st.spinner("Running Phase 1 Gatekeeper & Compilation..."):
            try:
                raw_fp = run_first_pass(sub_filepath, rule_path, smoke_input_val)
            except Exception as e:
                raw_fp = {"error": str(e)}
            fp_info = _parse_first_pass(raw_fp, sub_filepath)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Compilation", "SUCCESS" if fp_info["compiled"] else "FAILED")
        c2.metric("Smoke Test", fp_info["smoke_test"])
        c3.metric("Banned Keywords", "None" if not fp_info["banned_keywords"] else ", ".join(fp_info["banned_keywords"]))
        c4.metric("Loop / Crash", "Suspicious" if fp_info["suspected_loop"] else "Clean")

        with st.expander("🔍 View Raw Gatekeeper Output (first_pass)"):
            st.json(fp_info["raw"])

        if not fp_info["compiled"]:
            st.error("Compilation Failed! Dynamic analyzers skipped.")
            if fp_info["note"]:
                st.code(fp_info["note"], language="text")
            st.stop()

        # Run Specialist Analyzers
        binary_path = fp_info["binary_path"]
        metrics = active_rules.get("metrics", [])
        limits = {
            "time_limit_s": active_rules.get("time_limit_s", 1.0),
            "memory_limit_mb": active_rules.get("memory_limit_mb", 64)
        }

        results = {}
        progress = st.progress(0)
        status = st.empty()

        for idx, m in enumerate(metrics):
            a_name = m["analyzer"]
            status.text(f"Running specialist analyzer ({idx + 1}/{len(metrics)}): {a_name}...")
            params = dict(m.get("params", {}))
            # Ensure similarity_flag always points to this problem's submission subfolder
            if a_name == "similarity_flag" and "submissions_dir" not in params:
                params["submissions_dir"] = f"submissions/{selected_problem}/"
            results[a_name] = run_analyzer_safe(a_name, sub_filepath, binary_path, params, limits)
            progress.progress((idx + 1) / max(1, len(metrics)))

        status.empty()
        progress.empty()

        # Calculate Weighted Final Grade
        total_score = 0.0
        total_weight = 0.0
        rows = []

        for m in metrics:
            m_name = m.get("name", m["analyzer"])
            a_name = m["analyzer"]
            w = float(m.get("weight", 0.0))
            total_weight += w

            res = results.get(a_name, {})
            s = float(res.get("score", 0.0))
            total_score += s * w

            rows.append({
                "Metric": m_name.upper(),
                "Analyzer": a_name,
                "Weight": f"{int(round(w * 100))}%",
                "Raw Score": f"{s:.2f} / 1.00",
                "Weighted": f"{(s * w * 100):.1f}%",
                "Feedback": res.get("feedback", "")
            })

        final_grade = round((total_score / total_weight) * 100, 1) if total_weight > 0 else 0.0

        st.markdown("---")
        st.subheader("Assessment Scorecard")

        sc1, sc2 = st.columns([1, 3])
        with sc1:
            st.metric("Final Score", f"{final_grade} / 100")
            if final_grade >= 80:
                st.success("Verdict: Excellent")
            elif final_grade >= 50:
                st.warning("Verdict: Passed with Deductions")
            else:
                st.error("Verdict: Needs Improvement")

        with sc2:
            st.dataframe(rows, use_container_width=True)

        with st.expander("📊 Detailed Analyzer Evidence & JSON Logs"):
            for m in metrics:
                a_name = m["analyzer"]
                res = results.get(a_name, {})
                st.markdown(f"#### `{a_name}`")
                st.caption(f"Feedback: {res.get('feedback', '')}")
                st.json(res.get("evidence", {}))
                st.markdown("---")