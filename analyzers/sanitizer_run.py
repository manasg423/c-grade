import os
import subprocess
import glob
from core.sandbox import run_safe

def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    flags = params.get("flags", "-fsanitize=address,undefined")
    timeout_s = limits.get("time_limit_s", 1.0)

    # 1. Compile with AddressSanitizer and UndefinedBehaviorSanitizer
    san_bin = "/tmp/c_grade_bin/sanitizer_bin"
    os.makedirs(os.path.dirname(san_bin), exist_ok=True)

    compile_cmd = [
        "gcc", "-O1", "-g",
        "-fno-omit-frame-pointer"
    ] + flags.split() + [source_path, "-o", san_bin]

    comp = subprocess.run(compile_cmd, capture_output=True, text=True)
    if comp.returncode != 0:
        return {
            "score": 0.0,
            "evidence": {"compile_error": comp.stderr.strip()},
            "feedback": "Failed to compile with sanitizers enabled."
        }

    # 2. Gather sample test inputs
    testset_dir = "tests/add/"
    in_files = sorted(glob.glob(os.path.join(testset_dir, "*.in")))
    sample_inputs = []
    for inf in in_files:
        try:
            with open(inf, "r") as f:
                sample_inputs.append(f.read())
        except Exception:
            pass

    if not sample_inputs:
        sample_inputs = ["7 5\n"]

    diagnostics = []

    # 3. Run with ASan & UBSan (pass memory_mb=None so ASan can reserve shadow memory)
    for inp in sample_inputs:
        run_res = run_safe([san_bin], stdin_data=inp, timeout_s=timeout_s, memory_mb=None)
        
        err = run_res.get("stderr", "")
        if "runtime error:" in err or "AddressSanitizer" in err:
            lines = [line.strip() for line in err.splitlines() if "runtime error:" in line or "SUMMARY:" in line]
            diagnostics.append({
                "type": "Sanitizer Error",
                "input": inp.strip(),
                "details": lines if lines else err[:200]
            })

    # 4. Optional: Run Valgrind leak-check on normal binary (if valgrind is installed)
    valgrind_ok = True
    valgrind_msg = "Clean"
    try:
        valg = subprocess.run(
            ["valgrind", "--leak-check=full", "--error-exitcode=42", binary_path],
            input=sample_inputs[0],
            text=True,
            capture_output=True,
            timeout=2.0
        )
        if valg.returncode == 42:
            valgrind_ok = False
            valgrind_msg = "Memory leak detected by Valgrind."
    except Exception:
        pass

    # 5. Score calculation
    if not diagnostics and valgrind_ok:
        score = 1.0
        feedback = "ASan/UBSan clean. No memory safety issues detected."
    else:
        score = 0.5 if not diagnostics else 0.0
        feedback = f"Sanitizer diagnostics triggered: {len(diagnostics)} issues found."

    return {
        "score": score,
        "evidence": {
            "sanitizer_clean": len(diagnostics) == 0,
            "valgrind_status": valgrind_msg,
            "issues": diagnostics
        },
        "feedback": feedback
    }