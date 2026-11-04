from __future__ import annotations

import glob
import os
import subprocess


def _normalize_tokens(text: str) -> list[str]:
    return text.strip().split()


def _run_binary_safe(binary_path: str, stdin_data: str, timeout_s: float = 1.0) -> dict:
    try:
        proc = subprocess.run(
            [binary_path],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "timed_out": False,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Process timed out after {timeout_s} seconds.",
            "returncode": -1,
            "timed_out": True,
            "error": "TIMEOUT",
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
            "timed_out": False,
            "error": str(e),
        }


def _discover_test_cases(testset_dir: str) -> list[dict]:
    cases = []
    seen_stems = set()

    search_dirs = [testset_dir, os.path.join(testset_dir, "auto_generated")]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for in_path in sorted(glob.glob(os.path.join(d, "*.in"))):
            stem = os.path.splitext(os.path.basename(in_path))[0]
            if stem in seen_stems:
                continue
            out_path = os.path.join(d, f"{stem}.out")
            if os.path.isfile(out_path):
                seen_stems.add(stem)
                cases.append({
                    "stem": stem,
                    "in_path": in_path,
                    "out_path": out_path,
                })
    return cases


def evaluate(
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict = None,
) -> dict:
    params = params or {}
    limits = limits or {}

    if binary_path and not os.path.isfile(binary_path) and os.path.isfile(binary_path + ".exe"):
        binary_path = binary_path + ".exe"

    if not binary_path or not os.path.isfile(binary_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Student binary not found: {binary_path}"},
            "feedback": "Evaluation failed: missing compiled binary."
        }

    timeout_s = float(limits.get("time_limit_s", 1.0))
    testset_dir = params.get("testset_dir", "")
    if not testset_dir and params.get("oracle_path"):
        testset_dir = os.path.dirname(params["oracle_path"])

    if not testset_dir or not os.path.isdir(testset_dir):
        return {
            "score": 0.0,
            "evidence": {"error": f"Testset directory not found: {testset_dir}"},
            "feedback": "Evaluation failed: test directory missing."
        }

    test_cases = _discover_test_cases(testset_dir)
    if not test_cases:
        return {
            "score": 0.0,
            "evidence": {"error": f"No test cases (.in / .out) found in {testset_dir}"},
            "feedback": "Evaluation failed: no test cases available. Generate test cases in Teacher Portal."
        }

    passed = 0
    failing = []
    executed = []

    for tc in test_cases:
        with open(tc["in_path"], "r", encoding="utf-8", errors="ignore") as f_in:
            in_content = f_in.read()
        with open(tc["out_path"], "r", encoding="utf-8", errors="ignore") as f_out:
            expected_content = f_out.read()

        run_res = _run_binary_safe(binary_path, in_content, timeout_s=timeout_s)
        got_stdout = run_res["stdout"]

        tokens_got = _normalize_tokens(got_stdout)
        tokens_exp = _normalize_tokens(expected_content)

        is_match = (tokens_got == tokens_exp) and not run_res["timed_out"] and (run_res["returncode"] == 0)

        record = {
            "test_file": tc["stem"],
            "input": in_content.strip(),
            "expected": expected_content.strip(),
            "got": got_stdout.strip(),
            "status": "PASSED" if is_match else "FAILED",
            "returncode": run_res["returncode"],
        }
        executed.append(record)

        if is_match:
            passed += 1
        else:
            if run_res["timed_out"]:
                note = f"Timed out after {timeout_s}s (potential infinite loop)"
            elif run_res["returncode"] != 0:
                note = f"Runtime crash (exit code {run_res['returncode']})"
            else:
                note = "Standard output mismatch"

            record["note"] = note
            failing.append(record)

    total = len(test_cases)
    score = round(passed / max(1, total), 2)

    if passed == total:
        feedback = f"Passed all {passed}/{total} I/O test cases."
    else:
        first_fail = failing[0]
        feedback = (
            f"{passed}/{total} tests pass. Fails on input: {first_fail['input'][:40]} "
            f"({first_fail.get('note', 'mismatch')})."
        )

    return {
        "score": score,
        "evidence": {
            "tests_passed": passed,
            "tests_total": total,
            "executed_tests": executed,
            "failing": failing,
        },
        "feedback": feedback,
    }
