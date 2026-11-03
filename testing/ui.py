import os
import sys
sys.dont_write_bytecode = True
import glob
import json
import inspect
import tempfile
import importlib
import subprocess
from datetime import datetime
from pathlib import Path
import streamlit as st

# Keep this flow's data and temporary files under testing/.
ROOT = Path(__file__).resolve().parent.parent
TESTING_DIR = Path(__file__).resolve().parent
TEMP_DIR = TESTING_DIR / ".tmp"
sys.path.insert(0, str(ROOT))
os.chdir(TESTING_DIR)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
for temp_var in ("TMP", "TEMP", "TMPDIR"):
    os.environ[temp_var] = str(TEMP_DIR)
tempfile.tempdir = str(TEMP_DIR)

from core.first_pass import run_first_pass
from core.reporter import generate_report
from test_case_generator import generate_all_test_cases

st.set_page_config(
    page_title="C-Grade: Automated C Assessment",
    page_icon="⚡",
    layout="wide"
)

# Ensure base problems directory exists
os.makedirs("problems", exist_ok=True)


def list_available_problems() -> list[str]:
    rule_files = glob.glob("problems/*/*_rules.json")
    problems = []
    for f in rule_files:
        base = os.path.basename(f)
        prob = base.replace("_rules.json", "")
        problems.append(prob)
    return sorted(problems)


def sync_problem_directories():
    """
    Ensures every existing problem has its own dedicated 
    submissions, tests, and reports subfolders.
    """
    problems = set(list_available_problems()) | {"add", "str"}
    for prob in problems:
        prob_base = os.path.join("problems", prob)
        for sub in ("submissions", "tests", "reports"):
            os.makedirs(os.path.join(prob_base, sub), exist_ok=True)


sync_problem_directories()


# ---------------------------------------------------------
# Helper: Dynamic Parser for run_first_pass() Output
# ---------------------------------------------------------
def _parse_first_pass(fp: dict, source_path: str) -> dict:
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
    teacher_tab1, teacher_tab2, teacher_tab3 = st.tabs(
        ["➕ Create / Update Assignment", "📋 Inspect Deployed Rules", "⚡ Batch Grade"]
    )

    with teacher_tab1:
        st.subheader("Configure New Problem")
        st.caption(
            "Enter the problem details below. The system automatically creates dedicated folders in "
            "`problems/<problem>/{submissions, tests, reports}` and builds `problems/<problem>/<problem>_rules.json`."
        )

        col1, col2 = st.columns(2)
        with col1:
            problem_id = st.text_input("Problem Slug (e.g., add, str, sort, prime):").strip().lower()
            time_limit = st.number_input("Time Limit (seconds):", min_value=0.5, max_value=10.0, value=1.0, step=0.5)
            memory_limit = st.number_input("Memory Limit (MB):", min_value=16, max_value=512, value=64, step=16)
            sample_input = st.text_area(
                "Sample Smoke/Sanitizer Input:",
                value="10 20",
                height=150,
                placeholder="Enter sample input here...\nExample:\n5\n10 20 30 40 50",
            )

        with col2:
            input_type = st.selectbox(
                "Complexity Generator Input Type:",
                ["string", "integer_array", "raw_array", "single_number", "fixed_arithmetic"]
            )
            expected_complexity = st.selectbox(
                "Expected Asymptotic Complexity:",
                ["O(1)", "O(log N)", "O(N)", "O(N log N)", "O(N^2)", "O(N^3)"]
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
                prob_base_dir = os.path.join("problems", problem_id)
                prob_test_dir = os.path.join(prob_base_dir, "tests")
                prob_sub_dir = os.path.join(prob_base_dir, "submissions")
                prob_rep_dir = os.path.join(prob_base_dir, "reports")
                
                for d in (prob_test_dir, prob_sub_dir, prob_rep_dir):
                    os.makedirs(d, exist_ok=True)

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
                            "testset_dir": f"problems/{problem_id}/tests/",
                            "oracle_path": f"problems/{problem_id}/tests/oracle.c",
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
                            "oracle_path": f"problems/{problem_id}/tests/oracle.c",
                            "cases": ["empty", "whitespace"],
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
                        "params": {"submissions_dir": f"problems/{problem_id}/submissions/"}
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

                    rule_file = os.path.join(prob_base_dir, f"{problem_id}_rules.json")
                    with open(rule_file, "w", encoding="utf-8") as rf:
                        json.dump(rules_obj, rf, indent=2)

                    with st.spinner("Generating I/O and Edge Test Cases using Oracle & LLM..."):
                        try:
                            gen_res = generate_all_test_cases(
                                problem_id,
                                tests_dir=prob_test_dir,
                                oracle_path=oracle_path,
                                num_io=int(auto_gen_count),
                                num_edge=5,
                                force_regenerate=True
                            )
                            test_summary = f"- Generated {gen_res['io_count']} I/O test cases and {gen_res['edge_count']} Edge cases (.in / .out)"
                        except Exception as e:
                            test_summary = f"- Test generation note: {e}"

                    st.success(
                        f"Deployed '{problem_id}'!\n"
                        f"- Created Directory Structure: `{prob_base_dir}/`\n"
                        f"{test_summary}\n"
                        f"- Saved Rules: `{rule_file}`"
                    )

    with teacher_tab2:
        st.subheader("Inspect Rules & Test Cases")
        probs = list_available_problems()
        if not probs:
            st.info("No rules found.")
        else:
            sel = st.selectbox("Select Problem:", probs, key="inspect_prob")
            r_path = os.path.join("problems", sel, f"{sel}_rules.json")
            prob_tests_dir = os.path.join("problems", sel, "tests")
            auto_dir = os.path.join(prob_tests_dir, "auto_generated")
            edge_dir = os.path.join(prob_tests_dir, "edge_cases")

            try:
                with open(r_path, "r", encoding="utf-8") as f:
                    rules_data = json.load(f)
            except Exception:
                rules_data = {}

            cur_io_in_files = sorted(glob.glob(os.path.join(auto_dir, "gen_*.in")))
            if not cur_io_in_files:
                cur_io_in_files = sorted(glob.glob(os.path.join(prob_tests_dir, "*.in")))
            cur_edge_in_files = sorted(glob.glob(os.path.join(edge_dir, "edge_*.in")))

            rule_io_count = 5
            for m in rules_data.get("metrics", []):
                if m.get("analyzer") == "io_tests":
                    rule_io_count = m.get("params", {}).get("num_generated_tests", len(cur_io_in_files) or 5)
                    break
            rule_edge_count = len(cur_edge_in_files) or 5

            st.markdown("### ⚙️ Edit Problem Rules & Test Configuration")
            st.caption(
                "Modify parameters, test case counts, or metrics below. "
                "Saving updates `rules.json` and immediately synchronizes the test suite."
            )

            er_col1, er_col2 = st.columns(2)
            with er_col1:
                e_time = st.number_input(
                    "Time Limit (seconds):",
                    min_value=0.2, max_value=10.0,
                    value=float(rules_data.get("time_limit_s", 1.0)),
                    step=0.5,
                    key=f"e_time_{sel}"
                )
                e_mem = st.number_input(
                    "Memory Limit (MB):",
                    min_value=16, max_value=512,
                    value=int(rules_data.get("memory_limit_mb", 64)),
                    step=16,
                    key=f"e_mem_{sel}"
                )
                e_smoke = st.text_area(
                    "Sample Smoke Input:",
                    value=str(rules_data.get("smoke_input", "10 20")),
                    height=150,
                    key=f"e_smoke_{sel}",
                    placeholder="Enter multiple lines of sample input..."
                )

            with er_col2:
                e_io_count = st.number_input(
                    "Number of I/O Test Cases:",
                    min_value=1, max_value=30,
                    value=int(rule_io_count),
                    step=1,
                    key=f"e_io_count_{sel}"
                )
                e_edge_count = st.number_input(
                    "Number of Edge Cases:",
                    min_value=1, max_value=20,
                    value=int(rule_edge_count),
                    step=1,
                    key=f"e_edge_count_{sel}"
                )

            with st.expander("📝 Advanced Rules JSON Editor", expanded=False):
                st.caption("Directly edit the raw rules configuration JSON. Total weights must sum to 1.00.")
                raw_json_edit = st.text_area(
                    "Rules JSON Content:",
                    value=json.dumps(rules_data, indent=2),
                    height=280,
                    key=f"raw_json_area_{sel}"
                )

            if st.button("💾 Save Rules & Update Test Cases", type="primary", key=f"save_rules_btn_{sel}"):
                try:
                    try:
                        parsed_rules = json.loads(raw_json_edit)
                    except json.JSONDecodeError as jde:
                        st.error(f"Invalid JSON in advanced editor: {jde}")
                        parsed_rules = rules_data

                    parsed_rules["time_limit_s"] = float(e_time)
                    parsed_rules["memory_limit_mb"] = int(e_mem)
                    parsed_rules["smoke_input"] = str(e_smoke)

                    for m in parsed_rules.get("metrics", []):
                        if m.get("analyzer") == "io_tests":
                            if "params" not in m:
                                m["params"] = {}
                            m["params"]["num_generated_tests"] = int(e_io_count)

                    tot_w = sum(m.get("weight", 0.0) for m in parsed_rules.get("metrics", []))
                    if abs(tot_w - 1.0) > 1e-4:
                        st.warning(f"Note: Metric weights sum to {tot_w:.2f} (expected 1.00). Normalizing...")
                        for m in parsed_rules.get("metrics", []):
                            m["weight"] = round(m.get("weight", 0.0) / tot_w, 2)

                    with open(r_path, "w", encoding="utf-8") as rf:
                        json.dump(parsed_rules, rf, indent=2)

                    oracle_file = os.path.join(prob_tests_dir, "oracle.c")
                    with st.spinner(f"Regenerating {e_io_count} I/O tests & {e_edge_count} edge cases for {sel}..."):
                        res = generate_all_test_cases(
                            sel,
                            tests_dir=prob_tests_dir,
                            oracle_path=oracle_file,
                            sample_input=str(e_smoke),
                            num_io=int(e_io_count),
                            num_edge=int(e_edge_count),
                            force_regenerate=True
                        )

                    st.success(f"Rules updated in `{r_path}`!\n- Regenerated {res['io_count']} I/O test cases and {res['edge_count']} edge cases with matching .out files.")
                    st.rerun()

                except Exception as exc:
                    st.error(f"Failed to update rules: {exc}")

            st.markdown("---")
            st.subheader(f"🧪 Test Cases for `{sel}`")

            tc_col1, tc_col2 = st.columns(2)

            with tc_col1:
                st.markdown("#### 📥 Standard I/O Test Cases")
                io_in_files = sorted(glob.glob(os.path.join(auto_dir, "gen_*.in")))
                if not io_in_files:
                    io_in_files = sorted(glob.glob(os.path.join(prob_tests_dir, "*.in")))

                if not io_in_files:
                    st.warning("No I/O test cases generated yet.")
                else:
                    st.caption(f"Found {len(io_in_files)} I/O test case(s).")
                    for in_f in io_in_files:
                        base = os.path.splitext(os.path.basename(in_f))[0]
                        out_f = os.path.join(os.path.dirname(in_f), f"{base}.out")
                        with st.expander(f"📄 {base}", expanded=False):
                            c_in, c_out = st.columns(2)
                            with open(in_f, "r", encoding="utf-8", errors="ignore") as fi:
                                in_txt = fi.read()
                            with c_in:
                                st.caption("Standard Input (`.in`)")
                                st.code(in_txt if in_txt.strip() else "(empty)", language="text")
                            with c_out:
                                st.caption("Expected Output (`.out`)")
                                if os.path.isfile(out_f):
                                    with open(out_f, "r", encoding="utf-8", errors="ignore") as fo:
                                        out_txt = fo.read()
                                    st.code(out_txt if out_txt.strip() else "(empty)", language="text")
                                else:
                                    st.caption("*(No .out file)*")

            with tc_col2:
                st.markdown("#### ⚡ Adversarial Edge Cases")
                edge_in_files = sorted(glob.glob(os.path.join(edge_dir, "edge_*.in")))
                if not edge_in_files:
                    st.warning("No edge cases generated yet.")
                else:
                    st.caption(f"Found {len(edge_in_files)} edge case(s).")
                    for in_f in edge_in_files:
                        base = os.path.splitext(os.path.basename(in_f))[0]
                        out_f = os.path.join(edge_dir, f"{base}.out")
                        with st.expander(f"⚠️ {base}", expanded=False):
                            c_in, c_out = st.columns(2)
                            with open(in_f, "r", encoding="utf-8", errors="ignore") as fi:
                                in_txt = fi.read()
                            with c_in:
                                st.caption("Boundary Input (`.in`)")
                                st.code(in_txt if in_txt.strip() else "(empty)", language="text")
                            with c_out:
                                st.caption("Expected Output (`.out`)")
                                if os.path.isfile(out_f):
                                    with open(out_f, "r", encoding="utf-8", errors="ignore") as fo:
                                        out_txt = fo.read()
                                    st.code(out_txt if out_txt.strip() else "(empty)", language="text")
                                else:
                                    st.caption("*(No .out file)*")

    with teacher_tab3:
        st.subheader("Batch Grade \u2014 Grade All Submissions for a Problem")
        st.caption(
            "Select a problem and click **Run Batch Grade** to evaluate every `.c` file "
            "in `problems/<problem>/submissions/` automatically. Reports are saved to `problems/<problem>/reports/`."
        )

        probs_batch = list_available_problems()
        if not probs_batch:
            st.info("No assignments found. Create one in the 'Create / Update Assignment' tab.")
        else:
            batch_problem    = st.selectbox("Problem to batch-grade:", probs_batch, key="batch_prob")
            batch_rules_path = os.path.join("problems", batch_problem, f"{batch_problem}_rules.json")
            batch_sub_dir    = os.path.join("problems", batch_problem, "submissions")
            batch_files      = sorted(glob.glob(os.path.join(batch_sub_dir, "*.c")))

            if batch_files:
                st.info(f"Found **{len(batch_files)}** submission(s) in `{batch_sub_dir}/`.")
            else:
                st.warning(f"No `.c` files found in `{batch_sub_dir}/`.")

            if st.button("\u26a1 Run Batch Grade", type="primary", key="batch_btn"):
                if not batch_files:
                    st.error("No submissions to grade.")
                else:
                    batch_rules        = ensure_valid_rules_weights(batch_rules_path)
                    batch_results_rows = []
                    batch_progress     = st.progress(0)
                    batch_status       = st.empty()

                    for b_idx, src in enumerate(batch_files):
                        fname = os.path.basename(src)
                        batch_status.text(f"Grading {fname} ({b_idx + 1}/{len(batch_files)}) \u2026")

                        smoke_val = batch_rules.get("smoke_input", "7 5\n")
                        try:
                            raw_fp = run_first_pass(src, batch_rules_path, smoke_val)
                        except Exception as exc:
                            raw_fp = {"error": str(exc)}
                        fp_info = _parse_first_pass(raw_fp, src)

                        bin_path  = fp_info["binary_path"]
                        b_metrics = batch_rules.get("metrics", [])
                        b_limits  = {
                            "time_limit_s":    batch_rules.get("time_limit_s",    1.0),
                            "memory_limit_mb": batch_rules.get("memory_limit_mb", 64),
                        }

                        b_results = {}
                        if fp_info["compiled"]:
                            for m in b_metrics:
                                a_name = m["analyzer"]
                                params = dict(m.get("params", {}))
                                if a_name == "similarity_flag" and "submissions_dir" not in params:
                                    params["submissions_dir"] = f"problems/{batch_problem}/submissions/"
                                b_results[a_name] = run_analyzer_safe(a_name, src, bin_path, params, b_limits)

                        rpt = generate_report(src, batch_problem, batch_rules, raw_fp, b_results)
                        batch_results_rows.append({
                            "File":     fname,
                            "Grade":    f"{rpt['grade']:.1f} / 100",
                            "Letter":   rpt["letter_grade"],
                            "Compiled": "\u2713" if fp_info["compiled"] else "\u2717",
                            "Flagged":  "\u2691 YES" if rpt.get("flags") else "\u2014",
                        })
                        batch_progress.progress((b_idx + 1) / len(batch_files))

                    batch_status.empty()
                    batch_progress.empty()
                    st.success(f"Batch grading complete! {len(batch_files)} submission(s) graded.")
                    st.dataframe(batch_results_rows, use_container_width=True)
                    st.caption(f"Full reports saved to `problems/{batch_problem}/reports/`.")


# =========================================================
# 2. STUDENT PORTAL
# =========================================================
else:
    st.title("Student Portal — Automated C Code Grading")
    st.caption("Submit your C program for gatekeeper verification and multi-analyzer grading.")

    available_problems = list_available_problems()
    if not available_problems:
        st.warning("No assignments found in 'problems/'. Use the Teacher Portal to create one.")
        st.stop()

    col_sel, col_lim1, col_lim2 = st.columns([2, 1, 1])
    with col_sel:
        selected_problem = st.selectbox("Select Assignment:", available_problems)

    prob_base_dir = os.path.join("problems", selected_problem)
    prob_sub_dir = os.path.join(prob_base_dir, "submissions")
    prob_test_dir = os.path.join(prob_base_dir, "tests")
    os.makedirs(prob_sub_dir, exist_ok=True)
    os.makedirs(prob_test_dir, exist_ok=True)

    rule_path = os.path.join(prob_base_dir, f"{selected_problem}_rules.json")
    active_rules = ensure_valid_rules_weights(rule_path)

    with col_lim1:
        st.metric("Time Limit", f"{active_rules.get('time_limit_s', 1.0)} s")
    with col_lim2:
        st.metric("Memory Limit", f"{active_rules.get('memory_limit_mb', 64)} MB")

    st.caption(f"📁 Submissions for this problem are stored in: `{prob_sub_dir}/`")

    upload_mode = st.radio("Submission Method:", ["Paste Code Directly", "Upload .c File"], horizontal=True)

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

        smoke_input_val = active_rules.get("smoke_input", "7 5\n")

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
            if a_name == "similarity_flag" and "submissions_dir" not in params:
                params["submissions_dir"] = f"problems/{selected_problem}/submissions/"
            results[a_name] = run_analyzer_safe(a_name, sub_filepath, binary_path, params, limits)
            progress.progress((idx + 1) / max(1, len(metrics)))

        status.empty()
        progress.empty()

        reporter_report = generate_report(
            sub_filepath,
            selected_problem,
            active_rules,
            raw_fp,
            results,
        )

        rows = []
        for m in metrics:
            m_name = m.get("name", m["analyzer"])
            a_name = m["analyzer"]
            w = float(m.get("weight", 0.0))

            if a_name == "similarity_flag" or "plagiarism" in m_name.lower() or "originality" in m_name.lower():
                continue

            res = results.get(a_name, {})
            s = float(res.get("score", 0.0))
            fb = res.get("feedback", "")
            if "Analyzer failures:" in fb:
                fb = fb.split("Analyzer failures:")[0].strip()

            rows.append({
                "Metric": m_name.upper(),
                "Weight": f"{int(round(w * 100))}%",
                "Score": f"{s * 10:.1f} / 10",
                "Contribution": f"{(s * w * 100):.1f} pts",
                "Summary": fb,
            })

        final_grade = reporter_report.get("grade", 0.0)

        st.markdown("---")
        st.subheader("Assessment Scorecard")

        sc1, sc2 = st.columns([1, 3])
        with sc1:
            st.metric("Final Score", f"{final_grade:.1f} / 100")
            if final_grade >= 80:
                st.success("Verdict: Excellent")
            elif final_grade >= 50:
                st.warning("Verdict: Passed with Deductions")
            else:
                st.error("Verdict: Needs Improvement")

        with sc2:
            st.dataframe(rows, use_container_width=True)

        suggestions = reporter_report.get("suggestions", [])
        if suggestions:
            st.markdown("### 💡 Suggestions to Improve Your Grade")
            for tip in suggestions:
                st.markdown(f"- {tip}")

        failing_cases = []
        for m in metrics:
            a_name = m["analyzer"]
            if a_name == "similarity_flag":
                continue
            ev = results.get(a_name, {}).get("evidence", {})
            for fail in ev.get("failing", []):
                failing_cases.append({"metric": m.get("name", a_name).upper(), **fail})

        if failing_cases:
            st.markdown("### ⚠️ Counter-Examples: Where Your Code Failed")
            for i, fc in enumerate(failing_cases[:4], 1):
                inp = str(fc.get("input", "")).strip()
                exp = str(fc.get("expected", "")).strip()
                got = str(fc.get("got", "")).strip()
                why = fc.get("note", "")
                with st.expander(f"Failed Case {i:02d} [{fc['metric']}]", expanded=True):
                    c1, c2, c3 = st.columns(3)
                    c1.text_area(f"Input #{i}", value=inp or "(empty)", height=70, disabled=True)
                    c2.text_area(f"Expected #{i}", value=exp or "(empty)", height=70, disabled=True)
                    c3.text_area(f"Your Output #{i}", value=got or "(no output / crash)", height=70, disabled=True)
                    if why:
                        st.caption(f"**Why it failed:** {why}")

        with st.expander("🔍 View Technical Diagnostics & Feedback"):
            for m in metrics:
                a_name = m["analyzer"]
                if a_name == "similarity_flag" or "plagiarism" in m.get("name", "").lower() or "originality" in m.get("name", "").lower():
                    continue
                res = results.get(a_name, {})
                st.markdown(f"#### `{m.get('name', a_name).upper()}`")
                fb_clean = res.get('feedback', '')
                if "Analyzer failures:" in fb_clean:
                    fb_clean = fb_clean.split("Analyzer failures:")[0].strip()
                st.caption(f"Feedback: {fb_clean}")
                st.json(res.get("evidence", {}))
                st.markdown("---")

        st.markdown("---")
        st.subheader("Download Student Grading Report")
        txt_path = reporter_report.get("_txt_path", "")
        if txt_path and os.path.exists(txt_path):
            with open(txt_path, "rb") as tf:
                st.download_button(
                    label="⬇ Download Student Feedback Report (.txt)",
                    data=tf.read(),
                    file_name=os.path.basename(txt_path),
                    mime="text/plain",
                    type="primary",
                )