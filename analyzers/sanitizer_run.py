# import os
# import subprocess
# import glob
# from core.sandbox import run_safe

# def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
#     flags = params.get("flags", "-fsanitize=address,undefined")
#     timeout_s = limits.get("time_limit_s", 1.0)

#     # 1. Compile with AddressSanitizer and UndefinedBehaviorSanitizer
#     san_bin = "/tmp/c_grade_bin/sanitizer_bin"
#     os.makedirs(os.path.dirname(san_bin), exist_ok=True)

#     compile_cmd = [
#         "gcc", "-O1", "-g",
#         "-fno-omit-frame-pointer"
#     ] + flags.split() + [source_path, "-o", san_bin]

#     comp = subprocess.run(compile_cmd, capture_output=True, text=True)
#     if comp.returncode != 0:
#         return {
#             "score": 0.0,
#             "evidence": {"compile_error": comp.stderr.strip()},
#             "feedback": "Failed to compile with sanitizers enabled."
#         }

#     # 2. Gather sample test inputs
#     testset_dir = "tests/add/"
#     in_files = sorted(glob.glob(os.path.join(testset_dir, "*.in")))
#     sample_inputs = []
#     for inf in in_files:
#         try:
#             with open(inf, "r") as f:
#                 sample_inputs.append(f.read())
#         except Exception:
#             pass

#     if not sample_inputs:
#         sample_inputs = ["7 5\n"]

#     diagnostics = []

#     # 3. Run with ASan & UBSan (pass memory_mb=None so ASan can reserve shadow memory)
#     for inp in sample_inputs:
#         run_res = run_safe([san_bin], stdin_data=inp, timeout_s=timeout_s, memory_mb=None)
        
#         err = run_res.get("stderr", "")
#         if "runtime error:" in err or "AddressSanitizer" in err:
#             lines = [line.strip() for line in err.splitlines() if "runtime error:" in line or "SUMMARY:" in line]
#             diagnostics.append({
#                 "type": "Sanitizer Error",
#                 "input": inp.strip(),
#                 "details": lines if lines else err[:200]
#             })

#     # 4. Optional: Run Valgrind leak-check on normal binary (if valgrind is installed)
#     valgrind_ok = True
#     valgrind_msg = "Clean"
#     try:
#         valg = subprocess.run(
#             ["valgrind", "--leak-check=full", "--error-exitcode=42", binary_path],
#             input=sample_inputs[0],
#             text=True,
#             capture_output=True,
#             timeout=2.0
#         )
#         if valg.returncode == 42:
#             valgrind_ok = False
#             valgrind_msg = "Memory leak detected by Valgrind."
#     except Exception:
#         pass

#     # 5. Score calculation
#     if not diagnostics and valgrind_ok:
#         score = 1.0
#         feedback = "ASan/UBSan clean. No memory safety issues detected."
#     else:
#         score = 0.5 if not diagnostics else 0.0
#         feedback = f"Sanitizer diagnostics triggered: {len(diagnostics)} issues found."

#     return {
#         "score": score,
#         "evidence": {
#             "sanitizer_clean": len(diagnostics) == 0,
#             "valgrind_status": valgrind_msg,
#             "issues": diagnostics
#         },
#         "feedback": feedback
#     }


import os
import re
import shlex
import shutil
import subprocess
import tempfile

from core.sandbox import run_safe


def _get_authorized_inputs(params):
    """Accept only inputs explicitly provided by the current problem."""
    tests = []

    sample_input = params.get("sample_input")
    if isinstance(sample_input, str):
        tests.append({
            "name": "sample_input",
            "input": sample_input
        })

    extra = params.get("test_inputs", [])
    if isinstance(extra, (list, tuple)):
        for index, item in enumerate(extra):
            if isinstance(item, str):
                name = f"test_{index + 1}"
                input_text = item
            elif isinstance(item, dict) and isinstance(item.get("input"), str):
                name = str(item.get("name", f"test_{index + 1}"))
                input_text = item["input"]
            else:
                continue

            if not any(t["input"] == input_text for t in tests):
                tests.append({
                    "name": name,
                    "input": input_text
                })

    return tests


def _sanitizer_findings(stderr):
    """Extract concise ASan, UBSan and LeakSanitizer findings."""
    stderr = stderr or ""
    findings = []
    seen = set()

    patterns = [
        (
            "AddressSanitizer",
            r"ERROR:\s*AddressSanitizer:\s*([^\n]+)"
        ),
        (
            "UndefinedBehaviorSanitizer",
            r"([^:\n]+:\d+(?::\d+)?:\s*runtime error:\s*[^\n]+)"
        ),
        (
            "LeakSanitizer",
            r"ERROR:\s*LeakSanitizer:\s*([^\n]+)"
        )
    ]

    for tool, pattern in patterns:
        for match in re.finditer(pattern, stderr):
            message = match.group(1).strip()
            key = (tool, message)

            if key in seen:
                continue

            seen.add(key)
            findings.append({
                "tool": tool,
                "message": message[:250]
            })

    return findings


def _run_instrumented(binary, test, timeout_s):
    """Run one authorized input with ASan/UBSan enabled."""
    try:
        result = run_safe(
            [binary],
            stdin_data=test["input"],
            timeout_s=timeout_s,
            memory_mb=None
        )
    except Exception as exc:
        return {
            "test": test["name"],
            "status": "execution_error",
            "findings": [],
            "message": f"{type(exc).__name__}: {str(exc)[:200]}"
        }

    stderr = result.get("stderr", "") or ""
    returncode = result.get("returncode", result.get("exit_code"))

    timed_out = (
        result.get("timed_out", False)
        or result.get("timeout", False)
        or "timed out" in stderr.lower()
    )

    findings = _sanitizer_findings(stderr)

    if timed_out:
        status = "timeout"
    elif findings:
        status = "issue_detected"
    elif returncode not in (0, None):
        status = "abnormal_exit"
    elif returncode is None:
        status = "inconclusive"
    else:
        status = "clean"

    return {
        "test": test["name"],
        "status": status,
        "return_code": returncode,
        "findings": findings
    }


def _run_valgrind(binary_path, test, timeout_s):
    """Run Valgrind Memcheck for one authorized input."""
    valgrind = shutil.which("valgrind")

    if not valgrind:
        return {
            "test": test["name"],
            "status": "unavailable",
            "message": "Valgrind is not installed."
        }

    if not binary_path or not os.path.isfile(binary_path):
        return {
            "test": test["name"],
            "status": "unavailable",
            "message": "Compiled student binary not found."
        }

    command = [
        valgrind,
        "--tool=memcheck",
        "--leak-check=full",
        "--show-leak-kinds=all",
        "--errors-for-leak-kinds=definite,indirect,possible",
        "--track-origins=yes",
        "--error-exitcode=42",
        binary_path
    ]

    try:
        result = subprocess.run(
            command,
            input=test["input"],
            text=True,
            capture_output=True,
            timeout=max(float(timeout_s) * 3, 3.0),
            check=False
        )
    except subprocess.TimeoutExpired:
        return {
            "test": test["name"],
            "status": "timeout",
            "message": "Valgrind timed out."
        }
    except OSError as exc:
        return {
            "test": test["name"],
            "status": "unavailable",
            "message": f"{type(exc).__name__}: {str(exc)[:200]}"
        }

    log = (result.stderr or "") + "\n" + (result.stdout or "")

    summary_match = re.search(
        r"ERROR SUMMARY:\s*([\d,]+)\s+errors?",
        log,
        re.IGNORECASE
    )

    error_count = (
        int(summary_match.group(1).replace(",", ""))
        if summary_match else None
    )

    leak_summary = {}

    for kind in (
        "definitely lost",
        "indirectly lost",
        "possibly lost",
        "still reachable"
    ):
        match = re.search(
            rf"{re.escape(kind)}:\s*([\d,]+)\s+bytes? in\s+"
            rf"([\d,]+)\s+blocks?",
            log,
            re.IGNORECASE
        )

        if match:
            leak_summary[kind] = {
                "bytes": int(match.group(1).replace(",", "")),
                "blocks": int(match.group(2).replace(",", ""))
            }

    leak_kinds = (
        "definitely lost",
        "indirectly lost",
        "possibly lost"
    )

    leak_detected = any(
        leak_summary.get(kind, {}).get("bytes", 0) > 0
        for kind in leak_kinds
    )

    if leak_detected:
        status = "leak_detected"
    elif error_count is not None and error_count > 0:
        status = "memory_error_detected"
    elif error_count == 0:
        status = "clean"
    else:
        status = "inconclusive"

    # Keep only a small, relevant summary. No raw Valgrind trace.
    return {
        "test": test["name"],
        "status": status,
        "error_count": error_count,
        "leak_summary": leak_summary
    }


def evaluate(
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict
) -> dict:
    """
    Runtime memory analyzer for C programs.

    Uses only explicitly supplied sample_input/test_inputs.
    Does not discover test files or run GCC warnings/Cppcheck analysis.
    """
    params = params or {}
    limits = limits or {}

    if not os.path.isfile(source_path):
        return {
            "score": 0.0,
            "evidence": {
                "analysis_status": "not_run",
                "error": "Source file not found."
            },
            "feedback": "Sanitizer analysis could not start."
        }

    tests = _get_authorized_inputs(params)

    if not tests:
        return {
            "score": 0.0,
            "evidence": {
                "analysis_status": "not_run",
                "tests_run": 0,
                "issues": [],
                "error": "No sample_input or test_inputs were provided."
            },
            "feedback": (
                "Runtime memory analysis was not run because no input "
                "was supplied. This is not a student-code failure."
            )
        }

    timeout_s = float(limits.get("time_limit_s", 1.0))

    flags = params.get(
        "flags",
        "-fsanitize=address,undefined"
    )

    if isinstance(flags, str):
        flags = shlex.split(flags)
    elif not isinstance(flags, (list, tuple)):
        flags = ["-fsanitize=address,undefined"]

    compile_command = [
        "gcc",
        "-x", "c",
        "-std=c11",
        "-O1",
        "-g",
        "-fno-omit-frame-pointer",
        "-fno-sanitize-recover=all",
        *flags,
        source_path
    ]

    sanitizer_results = []
    valgrind_results = []

    try:
        with tempfile.TemporaryDirectory(prefix="c_grade_san_") as work_dir:
            sanitized_binary = os.path.join(
                work_dir,
                "sanitized_program"
            )

            try:
                compiled = subprocess.run(
                    compile_command + ["-o", sanitized_binary],
                    capture_output=True,
                    text=True,
                    timeout=max(timeout_s * 5, 10.0),
                    check=False
                )
            except subprocess.TimeoutExpired:
                return {
                    "score": 0.0,
                    "evidence": {
                        "analysis_status": "not_run",
                        "error": "Sanitizer compilation timed out."
                    },
                    "feedback": (
                        "Instrumented compilation timed out; no memory "
                        "verdict was produced."
                    )
                }

            if compiled.returncode != 0:
                return {
                    "score": 0.0,
                    "evidence": {
                        "analysis_status": "not_run",
                        "error": "Instrumented compilation failed."
                    },
                    "feedback": (
                        "Could not build the instrumented program. "
                        "No runtime memory verdict was produced."
                    )
                }

            for test in tests:
                sanitizer_results.append(
                    _run_instrumented(
                        sanitized_binary,
                        test,
                        timeout_s
                    )
                )

                if params.get("enable_valgrind", True):
                    valgrind_results.append(
                        _run_valgrind(
                            binary_path,
                            test,
                            timeout_s
                        )
                    )

    except OSError as exc:
        return {
            "score": 0.0,
            "evidence": {
                "analysis_status": "not_run",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"
            },
            "feedback": "Runtime memory analysis could not be completed."
        }

    sanitizer_issues = [
        item for item in sanitizer_results
        if item["status"] == "issue_detected"
    ]

    sanitizer_unverified = [
        item for item in sanitizer_results
        if item["status"] in (
            "timeout",
            "abnormal_exit",
            "execution_error",
            "inconclusive"
        )
    ]

    valgrind_issues = [
        item for item in valgrind_results
        if item["status"] in (
            "leak_detected",
            "memory_error_detected"
        )
    ]

    valgrind_unverified = [
        item for item in valgrind_results
        if item["status"] in (
            "unavailable",
            "timeout",
            "inconclusive"
        )
    ]

    if sanitizer_issues or valgrind_issues:
        score = 0.0
        analysis_status = "issues_detected"
        feedback = (
            f"Runtime analysis detected {len(sanitizer_issues)} "
            f"ASan/UBSan issue(s) and {len(valgrind_issues)} "
            "Valgrind memory issue(s)."
        )
    elif sanitizer_unverified or valgrind_unverified:
        score = 0.8
        analysis_status = "inconclusive"
        feedback = (
            "No confirmed memory issue was reported, but some checks "
            "were unavailable or inconclusive."
        )
    else:
        score = 1.0
        analysis_status = "clean"
        feedback = (
            f"No ASan/UBSan or Valgrind memory issues were found across "
            f"{len(tests)} explicitly supplied input(s)."
        )

    valgrind_status = (
        "issues_detected" if valgrind_issues
        else "inconclusive" if valgrind_unverified
        else "clean" if valgrind_results
        else "disabled"
    )

    return {
        "score": score,
        "evidence": {
            "analysis_status": analysis_status,
            "tests_run": len(tests),
            "test_names": [test["name"] for test in tests],
            "sanitizer_status": (
                "issues_detected" if sanitizer_issues
                else "inconclusive" if sanitizer_unverified
                else "clean"
            ),
            "sanitizer_issues": sanitizer_issues,
            "valgrind_status": valgrind_status,
            "valgrind_issues": valgrind_issues,
            "issues": sanitizer_issues + valgrind_issues
        },
        "feedback": feedback
    }
