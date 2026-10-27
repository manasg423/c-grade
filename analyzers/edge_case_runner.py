# import os
# import glob
# import subprocess
# import requests
# from core.sandbox import run_safe

# # Standard C language boundary constants across data types
# INT_MAX = "2147483647"
# INT_MIN = "-2147483648"

# def _generate_generic_boundary_cases(case_types: list) -> list:
#     """Generates standard C boundary inputs depending on metric categories in rules.json."""
#     generated = []
#     if "zero" in case_types:
#         generated.extend(["0", "0 0", "0\n0"])
#     if "negatives" in case_types:
#         generated.extend(["-1", "-1 -1", "-100 50"])
#     if "int_overflow" in case_types or "limits" in case_types:
#         generated.extend([
#             f"{INT_MAX} 1",
#             f"{INT_MAX} 0",
#             f"{INT_MIN} -1",
#             f"{INT_MAX}",
#             f"{INT_MIN}"
#         ])
#     if "empty" in case_types or "whitespace" in case_types:
#         generated.extend(["", "\n", "   "])
#     return generated

# def _get_ollama_universal_cases(source_code: str, oracle_code: str) -> list:
#     """
#     Asks Ollama to look at the C code and deduce what kind of input it expects
#     (e.g., integers, strings, arrays) and provide 3 tricky corner-case inputs.
#     """
#     reference = oracle_code if oracle_code else source_code
#     prompt = (
#         "Analyze the following C program to see what format it reads from standard input (stdin) "
#         "(e.g., integers, single characters, strings, arrays).\n"
#         "Generate 3 tricky edge-case or corner-case inputs (such as minimum/maximum bounds, "
#         "empty inputs, or extreme values) that might cause edge bugs or runtime crashes.\n"
#         "Return ONLY the raw input lines that should be piped to stdin. Do not write explanations, "
#         "do not write markdown, do not write code comments.\n\n"
#         f"```c\n{reference[:800]}\n```"
#     )
#     try:
#         res = requests.post(
#             "http://127.0.0.1:11434/api/generate",
#             json={
#                 "model": "qwen3.5:2b",
#                 "prompt": prompt,
#                 "stream": False,
#                 "options": {
#                     "temperature": 0,
#                     "seed": 42
#                 }
#             },
#             timeout=5.0
#         )
#         if res.status_code == 200:
#             lines = res.json().get("response", "").strip().splitlines()
#             cleaned = [line.strip() for line in lines if line.strip() and not line.startswith("`")]
#             return cleaned[:3]
#     except Exception:
#         pass
#     return []

# def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
#     timeout_s = limits.get("time_limit_s", 1.0)
    
#     # 1. Locate and compile reference oracle if available
#     # Look in the test directory configured in rules.json (or tests/)
#     testset_dir = params.get("testset_dir", os.path.dirname(params.get("oracle_path", "tests/add/")))
#     oracle_src = params.get("oracle_path", os.path.join(testset_dir, "oracle.c"))
    
#     oracle_bin = "/tmp/c_grade_bin/universal_oracle_bin"
#     os.makedirs(os.path.dirname(oracle_bin), exist_ok=True)
    
#     oracle_code = ""
#     has_oracle = False
#     if os.path.exists(oracle_src):
#         with open(oracle_src, "r", encoding="utf-8", errors="ignore") as f:
#             oracle_code = f.read()
#         comp = subprocess.run(["gcc", "-O2", oracle_src, "-o", oracle_bin], capture_output=True, text=True)
#         has_oracle = (comp.returncode == 0)

#     # 2. Compile student code with UBSan to catch overflows and undefined behavior
#     student_san_bin = "/tmp/c_grade_bin/student_edge_san"
#     subprocess.run(["gcc", "-O1", "-fsanitize=undefined", source_path, "-o", student_san_bin], capture_output=True)
#     target_bin = student_san_bin if os.path.exists(student_san_bin) else binary_path

#     # 3. Assemble Edge-Case Inputs dynamically
#     test_cases = []

#     # A. Explicit inputs provided directly in rules.json
#     if "test_inputs" in params:
#         test_cases.extend(params["test_inputs"])

#     # B. Load inputs from dedicated edge files (e.g. tests/*/edge_*.in) if present
#     edge_files = sorted(glob.glob(os.path.join(testset_dir, "edge_*.in")))
#     for ef in edge_files:
#         try:
#             with open(ef, "r") as f:
#                 test_cases.append(f.read().rstrip("\n"))
#         except Exception:
#             pass

#     # C. Rule-specified case keywords (e.g. "cases": ["negatives", "zero", "int_overflow"])
#     rule_cases = params.get("cases", [])
#     if rule_cases:
#         test_cases.extend(_generate_generic_boundary_cases(rule_cases))

#     # D. LLM-derived boundary cases
#     try:
#         with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
#             student_code = f.read()
#         llm_cases = _get_ollama_universal_cases(student_code, oracle_code)
#         for lc in llm_cases:
#             if lc not in test_cases:
#                 test_cases.append(lc)
#     except Exception:
#         pass

#     # Remove duplicates while preserving order
#     unique_cases = []
#     for c in test_cases:
#         if c not in unique_cases:
#             unique_cases.append(c)

#     if not unique_cases:
#         return {
#             "score": 1.0,
#             "evidence": {"note": "No edge cases specified or generated."},
#             "feedback": "Edge case check passed (no cases applied)."
#         }

#     # 4. Run student binary vs oracle on all cases
#     passed = 0
#     total = len(unique_cases)
#     counter_examples = []

#     for case_input in unique_cases:
#         stdin_data = case_input + "\n" if not case_input.endswith("\n") else case_input
        
#         # Oracle expected
#         expected = None
#         if has_oracle:
#             o_res = run_safe([oracle_bin], stdin_data=stdin_data, timeout_s=timeout_s, memory_mb=None)
#             expected = o_res["stdout"].strip()

#         # Student run
#         s_res = run_safe([target_bin], stdin_data=stdin_data, timeout_s=timeout_s, memory_mb=None)
#         got = s_res["stdout"].strip()
#         err = s_res.get("stderr", "")

#         has_fault = ("runtime error:" in err) or ("AddressSanitizer" in err)
        
#         # Verdict logic
#         if has_oracle:
#             is_correct = (got == expected) and not has_fault and not s_res["timed_out"]
#         else:
#             # If no oracle exists, test whether it survives without crashing or timing out
#             is_correct = not has_fault and not s_res["timed_out"] and (s_res["returncode"] == 0)

#         if is_correct:
#             passed += 1
#         else:
#             why = "Output mismatch against reference oracle."
#             if has_fault:
#                 fault_lines = [l.strip() for l in err.splitlines() if "runtime error:" in l]
#                 why = fault_lines[0] if fault_lines else "Runtime sanitizer fault detected."
#             elif s_res["timed_out"]:
#                 why = f"Execution timed out after {timeout_s}s on boundary input."
#             elif s_res["returncode"] != 0:
#                 why = f"Process crashed with returncode {s_res['returncode']}."

#             counter_examples.append({
#                 "input": case_input.strip(),
#                 "expected": expected if expected is not None else "Clean exit without crash",
#                 "got": got if not has_fault else f"{got} (Runtime fault)",
#                 "why": why
#             })

#     score = passed / total if total > 0 else 0.0
#     feedback = f"{passed}/{total} edge cases passed."
#     if counter_examples:
#         first = counter_examples[0]
#         feedback += f" Fails on input '{first['input']}': {first['why']}"

#     return {
#         "score": score,
#         "evidence": {
#             "cases_evaluated": total,
#             "cases_passed": passed,
#             "counter_examples": counter_examples
#         },
#         "feedback": feedback
#     }
"""LLM-driven edge-case analyzer for C-Grade Engine.

- Uses tests/<problem>/oracle.c as the input-format reference.
- If tests/<problem>/edge_cases/*.in already exist, uses only those cases.
- Otherwise asks local Ollama/Qwen 3.5 for five edge cases, validates them
  with the trusted oracle, and saves them for reuse.
- Does not use hardcoded input examples.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:
    resource = None


def _positive_number(value: Any, default: float) -> float:
    try:
        n = float(value)
        if n > 0 and n < float("inf"):
            return n
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _positive_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        if n > 0:
            return n
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _result(score: float, evidence: dict, feedback: str) -> dict:
    return {
        "score": max(0.0, min(1.0, float(score))),
        "evidence": evidence,
        "feedback": feedback,
    }


def _inconclusive(reason: str, **details: Any) -> dict:
    evidence = {
        "evaluation_status": "inconclusive",
        "error": reason,
        "cases_evaluated": 0,
        "cases_passed": 0,
        "counter_examples": [],
    }
    evidence.update(details)
    return _result(
        0.0,
        evidence,
        f"Edge-case evaluation inconclusive: {reason}",
    )


def _resource_limiter(limits: dict):
    """Best-effort process limits on POSIX systems."""
    if resource is None:
        return None

    cpu_s = _positive_int(limits.get("cpu_limit_s", 2), 2)
    memory_mb = _positive_int(limits.get("memory_limit_mb", 64), 64)
    output_mb = _positive_int(limits.get("output_limit_mb", 2), 2)

    def apply_limits():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))

        if hasattr(resource, "RLIMIT_AS"):
            memory = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

        if hasattr(resource, "RLIMIT_FSIZE"):
            size = output_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))

        if hasattr(resource, "RLIMIT_NOFILE"):
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    return apply_limits


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass

    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_process(
    command: list[str],
    stdin_data: str,
    timeout_s: float,
    limits: dict,
) -> dict:
    """Run a process with timeout, resource limits, and bounded output."""
    start = time.monotonic()
    output_limit = (
        _positive_int(limits.get("output_limit_mb", 2), 2)
        * 1024 * 1024
    )

    try:
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": stdout_file,
                "stderr": stderr_file,
                "text": True,
                "start_new_session": True,
            }

            preexec = _resource_limiter(limits)
            if preexec is not None:
                kwargs["preexec_fn"] = preexec

            proc = subprocess.Popen(command, **kwargs)

            try:
                proc.communicate(input=stdin_data, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "",
                    "returncode": proc.returncode,
                    "reason": f"Timed out after {timeout_s:.2f}s",
                    "timed_out": True,
                    "elapsed_s": round(time.monotonic() - start, 6),
                }

            stdout_file.seek(0)
            stderr_file.seek(0)

            out_bytes = stdout_file.read(output_limit + 1)
            err_bytes = stderr_file.read(output_limit + 1)

            if len(out_bytes) > output_limit or len(err_bytes) > output_limit:
                return {
                    "ok": False,
                    "stdout": out_bytes[:output_limit].decode(
                        "utf-8", "replace"
                    ),
                    "stderr": err_bytes[:output_limit].decode(
                        "utf-8", "replace"
                    ),
                    "returncode": proc.returncode,
                    "reason": "Output exceeded configured output limit",
                    "timed_out": False,
                    "elapsed_s": round(time.monotonic() - start, 6),
                }

            stdout = out_bytes.decode("utf-8", "replace")
            stderr = err_bytes.decode("utf-8", "replace")

            return {
                "ok": proc.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": proc.returncode,
                "reason": (
                    ""
                    if proc.returncode == 0
                    else f"Exited with code {proc.returncode}"
                ),
                "timed_out": False,
                "elapsed_s": round(time.monotonic() - start, 6),
            }

    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "reason": f"Execution error: {type(exc).__name__}: {exc}",
            "timed_out": False,
            "elapsed_s": round(time.monotonic() - start, 6),
        }


def _compile_c(
    source: str,
    output: str,
    params: dict,
    timeout_s: float,
) -> dict:
    compiler = str(params.get("compiler", "gcc"))
    flags = params.get(
        "oracle_compile_flags",
        ["-O0", "-std=c11"],
    )

    if isinstance(flags, str):
        flags = flags.split()

    if not isinstance(flags, list) or not all(
        isinstance(item, str) for item in flags
    ):
        return {
            "ok": False,
            "reason": "Compile flags must be strings",
        }

    try:
        proc = subprocess.run(
            [compiler, *flags, source, "-o", output, "-lm"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "Compilation timed out"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "reason": f"Compilation error: {exc}",
        }

    if proc.returncode:
        return {
            "ok": False,
            "reason": "C compilation failed",
            "stderr": proc.stderr[-6000:],
        }

    return {
        "ok": True,
        "stderr": proc.stderr[-2000:],
    }


def _read_source(path: str, max_chars: int = 30000) -> str:
    try:
        return Path(path).read_text(
            encoding="utf-8",
            errors="replace",
        )[:max_chars]
    except OSError:
        return ""


def _generate_with_ollama(
    oracle_src: str,
    count: int,
    params: dict,
) -> tuple[list[str], dict]:
    """Ask Qwen to infer stdin format and generate assignment-specific edges."""
    source = _read_source(oracle_src)

    if not source.strip():
        raise ValueError("Oracle source is empty or unreadable")

    endpoint = str(
        params.get(
            "ollama_url",
            "http://127.0.0.1:11434/api/generate",
        )
    ).rstrip("/")

    if not endpoint.endswith("/api/generate"):
        endpoint += "/api/generate"

    model = str(params.get("ollama_model", "qwen3.5:2b"))
    timeout = _positive_number(
        params.get("ollama_timeout_s", 90),
        90,
    )

    prompt = f"""
    Analyze this oracle C program and infer its exact stdin format.

    Generate exactly 5 distinct, valid edge cases for this specific problem.
    Focus on inputs likely to expose logic errors, not ordinary random examples.
    Consider relevant cases such as minimum valid size, duplicates, all-equal
    values, sorted/reverse-sorted inputs, and numeric boundaries only when
    appropriate to the actual problem and constraints.

    Every case must follow the oracle's input format exactly.
    Do not invent constraints or use hardcoded examples unrelated to the program.
    Do not include expected outputs.

    Return JSON only:
    {{
    "input_format": "short description",
    "test_cases": [
        {{
        "input": "complete stdin payload ending with newline",
        "edge_condition": "short label"
        }}
    ]
    }}

    Oracle source:
    {source}
    """

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
            "seed": 42,
            "num_predict": 700,
        },
        "think": False,
    }

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            body = json.loads(
                response.read().decode("utf-8", "replace")
            )
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            f"Ollama request failed at {endpoint}: {exc}"
        ) from exc

    try:
        decoded = json.loads(body.get("response", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Ollama response was not valid JSON"
        ) from exc

    if not isinstance(decoded, dict):
        raise ValueError("Ollama JSON response must be an object")

    inputs = []

    for item in decoded.get("test_cases", []):
        if not isinstance(item, dict):
            continue

        value = item.get("input")
        if not isinstance(value, str):
            continue

        # Handle models that encode newline characters literally.
        value = (
            value.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )

        if not value.endswith("\n"):
            value += "\n"

        if value.strip() and value not in inputs:
            inputs.append(value)

        if len(inputs) >= count:
            break

    if len(inputs) < count:
        raise ValueError(
            f"Ollama returned only {len(inputs)} usable distinct inputs; "
            f"expected {count}"
        )

    return inputs[:count], {
        "model": model,
        "input_format": str(
            decoded.get("input_format", "unspecified")
        ),
    }


def _find_existing_inputs(edge_dir: str) -> list[dict]:
    """Existing .in files take precedence over LLM generation."""
    root = Path(edge_dir)

    if not root.is_dir():
        return []

    found = []

    for file in sorted(root.glob("*.in")):
        try:
            value = file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue

        if value.strip():
            found.append({
                "input": value,
                "label": file.name,
                "source": "saved_edge_cases",
            })

    return found


def _save_generated_inputs(
    edge_dir: str,
    inputs: list[str],
    metadata: dict,
) -> None:
    folder = Path(edge_dir)
    folder.mkdir(parents=True, exist_ok=True)

    names = []

    for idx, value in enumerate(inputs, 1):
        name = f"edge_{idx:02d}.in"
        (folder / name).write_text(value, encoding="utf-8")
        names.append(name)

    manifest = {
        "generator": "ollama-qwen3.5-edge-v1",
        "model": metadata.get("model", "qwen3.5:2b"),
        "input_format": metadata.get(
            "input_format",
            "unspecified",
        ),
        "files": names,
    }

    (folder / ".edge_cases_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def evaluate(
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict = None,
) -> dict:
    """C-Grade Engine analyzer entry point."""
    params = params if isinstance(params, dict) else {}
    limits = limits if isinstance(limits, dict) else {}

    timeout_s = _positive_number(
        limits.get("time_limit_s", 1.0),
        1.0,
    )
    compile_timeout = _positive_number(
        limits.get("compile_timeout_s", 10),
        10,
    )

    testset_dir = str(params.get("testset_dir", ""))
    oracle_src = str(
        params.get(
            "oracle_path",
            os.path.join(testset_dir, "oracle.c"),
        )
    )

    if not testset_dir:
        testset_dir = os.path.dirname(oracle_src) or "tests"

    edge_dir = os.path.join(testset_dir, "edge_cases")

    if not os.path.isfile(oracle_src):
        return _inconclusive(
            f"Oracle C file not found: {oracle_src}"
        )

    if not binary_path or not os.path.isfile(binary_path):
        return _inconclusive(
            f"Student binary not found: {binary_path!r}"
        )

    # If cases already exist, use ONLY those cases.
    cases = _find_existing_inputs(edge_dir)
    cache_used = bool(cases)
    generated_metadata = {}

    try:
        with tempfile.TemporaryDirectory(
            prefix="cgrade_edge_"
        ) as temp_dir:
            oracle_bin = os.path.join(temp_dir, "oracle.bin")

            oracle_compile = _compile_c(
                oracle_src,
                oracle_bin,
                params,
                compile_timeout,
            )

            if not oracle_compile["ok"]:
                return _inconclusive(
                    "Oracle compilation failed: "
                    + oracle_compile.get("reason", ""),
                    oracle_compile_stderr=oracle_compile.get(
                        "stderr", ""
                    ),
                )

            # Generate cases only when no saved .in cases exist.
            if not cases:
                inputs, generated_metadata = _generate_with_ollama(
                    oracle_src,
                    _positive_int(
                        params.get("num_generated_edge_cases", 5),
                        5,
                    ),
                    params,
                )

                # Validate with the trusted oracle before saving.
                valid_inputs = []
                rejected_inputs = []

                for value in inputs:
                    check = _run_process(
                        [oracle_bin],
                        value,
                        timeout_s,
                        limits,
                    )

                    if check["ok"]:
                        valid_inputs.append(value)
                    else:
                        rejected_inputs.append({
                            "input": value,
                            "reason": (
                                check["reason"] or check["stderr"]
                            ),
                        })

                if not valid_inputs:
                    raise ValueError(
                        "Ollama generated no inputs that the oracle "
                        "could execute"
                    )

                _save_generated_inputs(
                    edge_dir,
                    valid_inputs,
                    generated_metadata,
                )

                cases = [
                    {
                        "input": value,
                        "label": f"edge_{idx:02d}.in",
                        "source": "ollama_generated",
                    }
                    for idx, value in enumerate(valid_inputs, 1)
                ]
            else:
                rejected_inputs = []

            # Try UBSan compilation. Fall back to the already compiled
            # student executable if the sanitizer build is unavailable.
            target_bin = binary_path
            sanitizer_info = "not attempted"

            if source_path and os.path.isfile(source_path):
                sanitized = os.path.join(
                    temp_dir,
                    "student_ubsan.bin",
                )

                try:
                    comp = subprocess.run(
                        [
                            str(params.get("compiler", "gcc")),
                            "-O1",
                            "-std=c11",
                            "-fsanitize=undefined",
                            source_path,
                            "-o",
                            sanitized,
                            "-lm",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=compile_timeout,
                        check=False,
                    )

                    if comp.returncode == 0 and os.path.isfile(sanitized):
                        target_bin = sanitized
                        sanitizer_info = "enabled"
                    else:
                        sanitizer_info = (
                            "unavailable; used normal student binary"
                        )
                except (
                    OSError,
                    subprocess.TimeoutExpired,
                    subprocess.SubprocessError,
                ):
                    sanitizer_info = (
                        "unavailable; used normal student binary"
                    )

            passed = 0
            counter_examples = []
            executed = []

            for case in cases:
                inp = case["input"]

                if not inp.endswith("\n"):
                    inp += "\n"

                # The oracle determines the expected output.
                oracle_run = _run_process(
                    [oracle_bin],
                    inp,
                    timeout_s,
                    limits,
                )

                if not oracle_run["ok"]:
                    record = {
                        "test_file": case["label"],
                        "source": case["source"],
                        "input": inp,
                        "status": "INVALID_TEST",
                        "why": (
                            "Oracle could not execute this input: "
                            + (
                                oracle_run["reason"]
                                or oracle_run["stderr"]
                            )
                        ),
                    }
                    executed.append(record)
                    counter_examples.append(record)
                    continue

                expected = oracle_run["stdout"]

                student_run = _run_process(
                    [target_bin],
                    inp,
                    timeout_s,
                    limits,
                )

                got = student_run["stdout"]
                stderr = student_run["stderr"]

                sanitizer_fault = (
                    "runtime error:" in stderr.lower()
                    or "undefinedbehavior" in stderr.lower()
                    or "addresssanitizer" in stderr.lower()
                )

                # Ignore harmless whitespace differences in output.
                output_matches = got.split() == expected.split()

                success = (
                    student_run["ok"]
                    and not student_run["timed_out"]
                    and not sanitizer_fault
                    and output_matches
                )

                record = {
                    "test_file": case["label"],
                    "source": case["source"],
                    "input": inp,
                    "expected": expected,
                    "got": got,
                    "status": "PASSED" if success else "FAILED",
                    "returncode": student_run["returncode"],
                    "elapsed_s": student_run["elapsed_s"],
                }

                if stderr:
                    record["stderr"] = stderr[-3000:]

                if not success:
                    if sanitizer_fault:
                        record["why"] = (
                            "Undefined-behavior/runtime sanitizer "
                            "diagnostic detected."
                        )
                    elif student_run["timed_out"]:
                        record["why"] = (
                            f"Timed out after {timeout_s:.2f}s."
                        )
                    elif not student_run["ok"]:
                        record["why"] = (
                            student_run["reason"]
                            or "Student program exited unsuccessfully."
                        )
                    else:
                        record["why"] = "Output mismatch against oracle."

                    counter_examples.append(record)
                else:
                    passed += 1

                executed.append(record)

            valid_total = sum(
                1
                for item in executed
                if item["status"] != "INVALID_TEST"
            )

            if valid_total == 0:
                return _inconclusive(
                    "No saved/generated edge case could be validated "
                    "by the oracle.",
                    cases_evaluated=len(cases),
                    counter_examples=counter_examples,
                    edge_cases_dir=edge_dir,
                )

            score = passed / valid_total

            feedback = (
                f"{passed}/{valid_total} edge cases passed."
            )

            if counter_examples:
                first = counter_examples[0]
                feedback += (
                    f" First issue: "
                    f"{first.get('why', 'See evidence for details')}"
                )

            return _result(score, {
                "evaluation_status": "completed",
                "cases_evaluated": valid_total,
                "cases_passed": passed,
                "cases_invalid": len(cases) - valid_total,
                "counter_examples": counter_examples,
                "executed_tests": executed,
                "edge_cases_dir": edge_dir,
                "saved_cases_reused": cache_used,
                "ollama_model": generated_metadata.get(
                    "model",
                    params.get("ollama_model", "qwen3.5:2b"),
                ),
                "input_format": generated_metadata.get(
                    "input_format",
                    "saved existing cases" if cache_used else "unspecified",
                ),
                "rejected_generated_inputs": rejected_inputs,
                "student_ubsan": sanitizer_info,
                "oracle_compile_ok": True,
            }, feedback)

    except Exception as exc:
        return _inconclusive(
            f"{type(exc).__name__}: {exc}",
            edge_cases_dir=edge_dir,
        )