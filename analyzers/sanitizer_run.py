from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Knowledge Base: Diagnostic Patterns -> Metadata & Remediation Guidance
# ---------------------------------------------------------------------------

DIAGNOSTIC_KB: dict[str, dict[str, str]] = {
    # Heap & Stack Memory Violations (ASan)
    "stack-buffer-overflow": {
        "title": "Stack Buffer Overflow",
        "category": "memory_error",
        "hint": (
            "You wrote or read past the boundary of a local array. Verify "
            "loop bounds (indices run from 0 to N-1) and avoid unsafe functions "
            "like gets() or unchecked strcpy()/scanf('%s')."
        ),
    },
    "heap-buffer-overflow": {
        "title": "Heap Buffer Overflow",
        "category": "memory_error",
        "hint": (
            "Out-of-bounds access on dynamic memory allocated with malloc() or calloc(). "
            "Ensure the allocated buffer size accounts for all elements and includes "
            "room for the null terminator ('\\0') on strings."
        ),
    },
    "global-buffer-overflow": {
        "title": "Global Array Overflow",
        "category": "memory_error",
        "hint": (
            "Array subscript exceeded bounds on a global or static array. "
            "Check constant array bounds and indexing logic."
        ),
    },
    "stack-buffer-underflow": {
        "title": "Buffer Underflow",
        "category": "memory_error",
        "hint": (
            "Accessed a negative index (e.g., arr[-1]). Ensure index calculations "
            "are guarded so index >= 0."
        ),
    },
    # Dynamic Lifecycle & Pointer Errors (ASan / LSan)
    "heap-use-after-free": {
        "title": "Use-After-Free",
        "category": "memory_error",
        "hint": (
            "Accessed a pointer after calling free() on it. Set pointers to NULL "
            "immediately after freeing them (ptr = NULL;) to avoid stale references."
        ),
    },
    "double-free": {
        "title": "Double Free",
        "category": "memory_error",
        "hint": (
            "free() was invoked more than once on the same memory address. "
            "Ensure each allocated block has exactly one clear owner that releases it."
        ),
    },
    "attempting free on address which was not malloc()-ed": {
        "title": "Invalid Free Target",
        "category": "memory_error",
        "hint": (
            "Attempted to free a stack variable, a string literal, or an offset pointer. "
            "Pass only the original base address returned by malloc() or calloc() to free()."
        ),
    },
    "alloc-dealloc-mismatch": {
        "title": "Allocation-Deallocation Mismatch",
        "category": "memory_error",
        "hint": (
            "The memory deallocation function does not match how it was allocated. "
            "Use free() exclusively for pointers created with malloc(), calloc(), or realloc()."
        ),
    },
    "detected memory leaks": {
        "title": "Memory Leak (Unfreed Buffer)",
        "category": "leak",
        "hint": (
            "Memory allocated on the heap was never released. Ensure a corresponding free() "
            "is executed before functions return or main() exits."
        ),
    },
    # Fatal Crashes & Segmentation Faults
    "SEGV on unknown address": {
        "title": "Segmentation Fault / Bad Memory Access",
        "category": "crash",
        "hint": (
            "Crashed attempting to access an unmapped or restricted virtual memory address. "
            "Check for NULL pointer dereferences, uninitialized pointers, or deep recursion."
        ),
    },
    "null-dereference": {
        "title": "Null Pointer Dereference",
        "category": "crash",
        "hint": (
            "Attempted to read or write through a NULL pointer. Always verify return "
            "values of malloc() or search lookup functions before accessing fields or indices."
        ),
    },
    # Undefined Behavior (UBSan)
    "division-by-zero": {
        "title": "Division / Modulo by Zero",
        "category": "undefined_behavior",
        "hint": (
            "A denominator or modulo operand evaluated to 0. Add a safety guard "
            "(e.g., if (denom != 0)) prior to the division or modulo operation."
        ),
    },
    "signed-integer-overflow": {
        "title": "Signed Integer Overflow",
        "category": "undefined_behavior",
        "hint": (
            "Arithmetic operation exceeded the range of a signed integer. Use a larger "
            "type (e.g., long long or int64_t) or validate values before operating."
        ),
    },
    "shift-out-of-bounds": {
        "title": "Invalid Bit Shift",
        "category": "undefined_behavior",
        "hint": (
            "Shift count is negative or exceeds the bit width of the operand type. "
            "Ensure 0 <= shift_amount < type_bit_width."
        ),
    },
    "applying non-zero offset to null pointer": {
        "title": "Invalid Pointer Arithmetic",
        "category": "undefined_behavior",
        "hint": (
            "Pointer arithmetic calculated an offset on a pointer that evaluated to NULL. "
            "Verify pointer validity before calculating offsets."
        ),
    },
    "use-of-uninitialized-value": {
        "title": "Uninitialized Memory Read",
        "category": "undefined_behavior",
        "hint": (
            "Read data from a variable or buffer before writing to it. Initialize "
            "variables upon declaration or use calloc() instead of malloc()."
        ),
    },
}

# ---------------------------------------------------------------------------
# Helper Routines
# ---------------------------------------------------------------------------

def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate the spawned process group cleanly to avoid zombie subprocesses."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _extract_issue_details(stderr: str, source_filename: str) -> dict[str, Any]:
    """Parse raw ASan/UBSan diagnostics into structured line-level data."""
    matched_key = next((k for k in DIAGNOSTIC_KB if k in stderr), None)

    line_no, col_no = 0, 0
    loc_match = re.search(rf"{re.escape(source_filename)}:(\d+)(?::(\d+))?", stderr)
    if loc_match:
        try:
            line_no = int(loc_match.group(1))
            col_no = int(loc_match.group(2)) if loc_match.group(2) else 0
        except (ValueError, TypeError):
            pass

    if matched_key:
        info = DIAGNOSTIC_KB[matched_key]
        key, title, cat, hint = matched_key, info["title"], info["category"], info["hint"]
    elif err_match := re.search(r"ERROR: \w+Sanitizer: ([\w-]+)", stderr):
        raw_name = err_match.group(1)
        key, title, cat = raw_name.lower(), raw_name.replace("-", " ").title(), "memory_error"
        hint = "Check pointer boundaries, allocation lifecycles, and bounds."
    elif ub_match := re.search(r"runtime error: (.+)", stderr):
        desc = ub_match.group(1).split("\n")[0].strip()
        key, title, cat = "undefined-behavior", f"Undefined Behavior: {desc}", "undefined_behavior"
        hint = "Ensure operations comply with ISO C rules (valid pointer arithmetic, valid ranges)."
    else:
        key, title, cat = "generic-runtime-fault", "Runtime Fault / Abnormal Termination", "crash"
        hint = "Inspect runtime boundary checks and verify program flow termination."

    return {
        "key": key,
        "title": title,
        "category": cat,
        "line": line_no,
        "column": col_no,
        "hint": hint,
    }


def _run_sandboxed_case(
    binary_path: str,
    stdin_data: str,
    timeout_s: float,
    env: dict[str, str],
    output_limit_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    """Execute a single test case with timeout and output capture limits."""
    start = time.monotonic()

    try:
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            proc = subprocess.Popen(
                [binary_path],
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                env=env,
                start_new_session=True,
            )

            try:
                proc.communicate(input=stdin_data, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
                return {
                    "ok": False,
                    "timed_out": True,
                    "stdout": "",
                    "stderr": "",
                    "returncode": -signal.SIGKILL,
                    "elapsed_s": round(time.monotonic() - start, 4),
                }

            outputs: list[str] = []
            for f in (stdout_file, stderr_file):
                f.seek(0)
                raw = f.read(output_limit_bytes + 1)
                outputs.append(raw[:output_limit_bytes].decode("utf-8", errors="replace"))

            return {
                "ok": proc.returncode == 0,
                "timed_out": False,
                "stdout": outputs[0],
                "stderr": outputs[1],
                "returncode": proc.returncode,
                "elapsed_s": round(time.monotonic() - start, 4),
            }

    except Exception as exc:
        return {
            "ok": False,
            "timed_out": False,
            "stdout": "",
            "stderr": f"Subprocess launch error: {type(exc).__name__}: {exc}",
            "returncode": None,
            "elapsed_s": round(time.monotonic() - start, 4),
        }


# ---------------------------------------------------------------------------
# Main Analyzer Entry Point
# ---------------------------------------------------------------------------

def evaluate(
    source_path: str,
    binary_path: str,
    params: dict | None = None,
    limits: dict | None = None,
) -> dict:
    """Analyze a student's C code using AddressSanitizer and UndefinedBehaviorSanitizer."""
    params = params if isinstance(params, dict) else {}
    limits = limits if isinstance(limits, dict) else {}

    if not source_path or not os.path.isfile(source_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Source file not found: {source_path!r}"},
            "feedback": "Evaluation failed: source file not found.",
        }

    source_path_obj = Path(source_path).resolve()
    filename = source_path_obj.name

    penalties = {
        "memory_error": float(params.get("memory_penalty", 0.35)),
        "leak": float(params.get("leak_penalty", 0.20)),
        "crash": float(params.get("crash_penalty", 0.35)),
        "undefined_behavior": float(params.get("ub_penalty", 0.20)),
        "timeout": float(params.get("timeout_penalty", 0.25)),
    }

    # -----------------------------------------------------------------------
    # Resolve Inputs: Priority to sample_input (default in rules.json),
    # followed by test_cases, then test_inputs fallback
    # -----------------------------------------------------------------------
    test_cases: list[dict[str, Any]] = []

    if "sample_input" in params and params["sample_input"] is not None:
        raw_sample = str(params["sample_input"])
        test_cases = [{"input": raw_sample, "label": "sample_input"}]
    elif "test_cases" in params and isinstance(params["test_cases"], list) and params["test_cases"]:
        test_cases = params["test_cases"]
    elif "test_inputs" in params and isinstance(params["test_inputs"], list) and params["test_inputs"]:
        test_cases = [
            {"input": str(ti), "label": f"test_input_{idx}"}
            for idx, ti in enumerate(params["test_inputs"], 1)
        ]
    else:
        test_cases = [{"input": "\n", "label": "default_case"}]

    timeout_s = float(limits.get("time_limit_s", 2.0))
    compiler = str(params.get("compiler", "gcc"))
    compile_timeout = float(limits.get("compile_timeout_s", 10.0))

    with tempfile.TemporaryDirectory(prefix="cgrade_sanitizer_") as tmp_dir:
        sanitized_bin = os.path.join(tmp_dir, "sanitized_target")

        compile_cmd = [
            compiler,
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-g",
            "-O1",
            str(source_path_obj),
            "-o",
            sanitized_bin,
            "-lm",
        ]

        try:
            comp_proc = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=compile_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "score": 0.0,
                "evidence": {"error": "Compiler timed out during sanitizer instrumentation."},
                "feedback": "Evaluation failed: compiler timed out.",
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "score": 0.0,
                "evidence": {"error": f"Failed to invoke compiler: {exc}"},
                "feedback": "Evaluation failed: unable to run compiler.",
            }

        if comp_proc.returncode != 0:
            return {
                "score": 0.0,
                "evidence": {
                    "compiler_stderr": comp_proc.stderr[-3000:],
                    "compile_returncode": comp_proc.returncode,
                },
                "feedback": "Evaluation failed: compilation failed with sanitizer flags enabled.",
            }

        env = os.environ.copy()
        env["ASAN_OPTIONS"] = "detect_leaks=1:color=never:abort_on_error=0:symbolize=1"
        env["UBSAN_OPTIONS"] = "halt_on_error=1:color=never:print_stacktrace=1"

        executed_records: list[dict[str, Any]] = []
        unique_issues: dict[str, dict[str, Any]] = {}
        total_deduction = 0.0

        def _record_issue(
            dedup_key: str,
            label: str,
            title: str,
            category: str,
            line: int,
            column: int,
            location: str,
            how_to_improve: str,
            penalty: float,
        ) -> None:
            nonlocal total_deduction
            if dedup_key not in unique_issues:
                total_deduction += penalty
                unique_issues[dedup_key] = {
                    "first_failed_test": label,
                    "title": title,
                    "category": category,
                    "line": line,
                    "column": column,
                    "location": location,
                    "how_to_improve": how_to_improve,
                    "penalty": penalty,
                    "occurrences": 1,
                }
            else:
                unique_issues[dedup_key]["occurrences"] += 1

        for idx, case in enumerate(test_cases, 1):
            inp_data = str(case.get("input", ""))
            # Handle literal escaped newlines from JSON strings like "5\\n 9 2 7 4 1"
            if "\\n" in inp_data:
                inp_data = inp_data.replace("\\n", "\n")
            if inp_data and not inp_data.endswith("\n"):
                inp_data += "\n"

            label = str(case.get("label", f"test_{idx}"))

            run_res = _run_sandboxed_case(
                sanitized_bin,
                inp_data,
                timeout_s,
                env,
            )

            if run_res["timed_out"]:
                _record_issue(
                    dedup_key="timeout:infinite_loop",
                    label=label,
                    title="Execution Timeout",
                    category="timeout",
                    line=0,
                    column=0,
                    location=f"{filename} (Runtime)",
                    how_to_improve="Process exceeded time limit. Verify loop termination logic and recursion base cases.",
                    penalty=penalties["timeout"],
                )
                executed_records.append({
                    "test": label,
                    "status": "TIMEOUT",
                    "returncode": run_res["returncode"],
                    "elapsed_s": run_res["elapsed_s"],
                })
                continue

            stderr = run_res["stderr"]
            has_violation = (
                not run_res["ok"]
                or "Sanitizer" in stderr
                or "runtime error:" in stderr
            )

            if has_violation and stderr.strip():
                diag = _extract_issue_details(stderr, filename)
                category = diag["category"]
                line = diag["line"]
                loc_str = f"Line {line}" if line > 0 else "Execution runtime"

                _record_issue(
                    dedup_key=f"{diag['key']}:{line}",
                    label=label,
                    title=diag["title"],
                    category=category,
                    line=line,
                    column=diag["column"],
                    location=f"{filename} at {loc_str}",
                    how_to_improve=diag["hint"],
                    penalty=penalties.get(category, 0.20),
                )

            executed_records.append({
                "test": label,
                "status": "FAILED" if has_violation else "PASSED",
                "returncode": run_res["returncode"],
                "elapsed_s": run_res["elapsed_s"],
            })

    final_score = round(max(0.0, 1.0 - total_deduction), 2)
    issues_list = list(unique_issues.values())

    if not issues_list:
        feedback = "Passed all runtime safety checks. No memory corruption, memory leaks, or undefined behavior detected."
    else:
        feedback_lines = [f"Detected {len(issues_list)} distinct runtime issue(s):"]
        for i, item in enumerate(issues_list, 1):
            occ_str = f" (failed in {item['occurrences']} tests)" if item["occurrences"] > 1 else ""
            feedback_lines.append(
                f"{i}. [{item['title']}] at {item['location']}{occ_str}.\n"
                f"   How to Fix: {item['how_to_improve']}"
            )
        feedback = "\n".join(feedback_lines)

    return {
        "score": final_score,
        "evidence": {
            "total_unique_issues": len(issues_list),
            "deductions_applied": round(total_deduction, 2),
            "issues": issues_list,
            "executed_tests": executed_records,
        },
        "feedback": feedback,
    }