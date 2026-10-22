import os
import glob
import subprocess
import requests
from core.sandbox import run_safe

# Standard C language boundary constants across data types
INT_MAX = "2147483647"
INT_MIN = "-2147483648"

def _generate_generic_boundary_cases(case_types: list) -> list:
    """Generates standard C boundary inputs depending on metric categories in rules.json."""
    generated = []
    if "zero" in case_types:
        generated.extend(["0", "0 0", "0\n0"])
    if "negatives" in case_types:
        generated.extend(["-1", "-1 -1", "-100 50"])
    if "int_overflow" in case_types or "limits" in case_types:
        generated.extend([
            f"{INT_MAX} 1",
            f"{INT_MAX} 0",
            f"{INT_MIN} -1",
            f"{INT_MAX}",
            f"{INT_MIN}"
        ])
    if "empty" in case_types or "whitespace" in case_types:
        generated.extend(["", "\n", "   "])
    return generated

def _get_ollama_universal_cases(source_code: str, oracle_code: str) -> list:
    """
    Asks Ollama to look at the C code and deduce what kind of input it expects
    (e.g., integers, strings, arrays) and provide 3 tricky corner-case inputs.
    """
    reference = oracle_code if oracle_code else source_code
    prompt = (
        "Analyze the following C program to see what format it reads from standard input (stdin) "
        "(e.g., integers, single characters, strings, arrays).\n"
        "Generate 3 tricky edge-case or corner-case inputs (such as minimum/maximum bounds, "
        "empty inputs, or extreme values) that might cause edge bugs or runtime crashes.\n"
        "Return ONLY the raw input lines that should be piped to stdin. Do not write explanations, "
        "do not write markdown, do not write code comments.\n\n"
        f"```c\n{reference[:800]}\n```"
    )
    try:
        res = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "qwen3.5:2b",
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "seed": 42
                }
            },
            timeout=5.0
        )
        if res.status_code == 200:
            lines = res.json().get("response", "").strip().splitlines()
            cleaned = [line.strip() for line in lines if line.strip() and not line.startswith("`")]
            return cleaned[:3]
    except Exception:
        pass
    return []

def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    timeout_s = limits.get("time_limit_s", 1.0)
    
    # 1. Locate and compile reference oracle if available
    # Look in the test directory configured in rules.json (or tests/)
    testset_dir = params.get("testset_dir", os.path.dirname(params.get("oracle_path", "tests/add/")))
    oracle_src = params.get("oracle_path", os.path.join(testset_dir, "oracle.c"))
    
    oracle_bin = "/tmp/c_grade_bin/universal_oracle_bin"
    os.makedirs(os.path.dirname(oracle_bin), exist_ok=True)
    
    oracle_code = ""
    has_oracle = False
    if os.path.exists(oracle_src):
        with open(oracle_src, "r", encoding="utf-8", errors="ignore") as f:
            oracle_code = f.read()
        comp = subprocess.run(["gcc", "-O2", oracle_src, "-o", oracle_bin], capture_output=True, text=True)
        has_oracle = (comp.returncode == 0)

    # 2. Compile student code with UBSan to catch overflows and undefined behavior
    student_san_bin = "/tmp/c_grade_bin/student_edge_san"
    subprocess.run(["gcc", "-O1", "-fsanitize=undefined", source_path, "-o", student_san_bin], capture_output=True)
    target_bin = student_san_bin if os.path.exists(student_san_bin) else binary_path

    # 3. Assemble Edge-Case Inputs dynamically
    test_cases = []

    # A. Explicit inputs provided directly in rules.json
    if "test_inputs" in params:
        test_cases.extend(params["test_inputs"])

    # B. Load inputs from dedicated edge files (e.g. tests/*/edge_*.in) if present
    edge_files = sorted(glob.glob(os.path.join(testset_dir, "edge_*.in")))
    for ef in edge_files:
        try:
            with open(ef, "r") as f:
                test_cases.append(f.read().rstrip("\n"))
        except Exception:
            pass

    # C. Rule-specified case keywords (e.g. "cases": ["negatives", "zero", "int_overflow"])
    rule_cases = params.get("cases", [])
    if rule_cases:
        test_cases.extend(_generate_generic_boundary_cases(rule_cases))

    # D. LLM-derived boundary cases
    try:
        with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
            student_code = f.read()
        llm_cases = _get_ollama_universal_cases(student_code, oracle_code)
        for lc in llm_cases:
            if lc not in test_cases:
                test_cases.append(lc)
    except Exception:
        pass

    # Remove duplicates while preserving order
    unique_cases = []
    for c in test_cases:
        if c not in unique_cases:
            unique_cases.append(c)

    if not unique_cases:
        return {
            "score": 1.0,
            "evidence": {"note": "No edge cases specified or generated."},
            "feedback": "Edge case check passed (no cases applied)."
        }

    # 4. Run student binary vs oracle on all cases
    passed = 0
    total = len(unique_cases)
    counter_examples = []

    for case_input in unique_cases:
        stdin_data = case_input + "\n" if not case_input.endswith("\n") else case_input
        
        # Oracle expected
        expected = None
        if has_oracle:
            o_res = run_safe([oracle_bin], stdin_data=stdin_data, timeout_s=timeout_s, memory_mb=None)
            expected = o_res["stdout"].strip()

        # Student run
        s_res = run_safe([target_bin], stdin_data=stdin_data, timeout_s=timeout_s, memory_mb=None)
        got = s_res["stdout"].strip()
        err = s_res.get("stderr", "")

        has_fault = ("runtime error:" in err) or ("AddressSanitizer" in err)
        
        # Verdict logic
        if has_oracle:
            is_correct = (got == expected) and not has_fault and not s_res["timed_out"]
        else:
            # If no oracle exists, test whether it survives without crashing or timing out
            is_correct = not has_fault and not s_res["timed_out"] and (s_res["returncode"] == 0)

        if is_correct:
            passed += 1
        else:
            why = "Output mismatch against reference oracle."
            if has_fault:
                fault_lines = [l.strip() for l in err.splitlines() if "runtime error:" in l]
                why = fault_lines[0] if fault_lines else "Runtime sanitizer fault detected."
            elif s_res["timed_out"]:
                why = f"Execution timed out after {timeout_s}s on boundary input."
            elif s_res["returncode"] != 0:
                why = f"Process crashed with returncode {s_res['returncode']}."

            counter_examples.append({
                "input": case_input.strip(),
                "expected": expected if expected is not None else "Clean exit without crash",
                "got": got if not has_fault else f"{got} (Runtime fault)",
                "why": why
            })

    score = passed / total if total > 0 else 0.0
    feedback = f"{passed}/{total} edge cases passed."
    if counter_examples:
        first = counter_examples[0]
        feedback += f" Fails on input '{first['input']}': {first['why']}"

    return {
        "score": score,
        "evidence": {
            "cases_evaluated": total,
            "cases_passed": passed,
            "counter_examples": counter_examples
        },
        "feedback": feedback
    }