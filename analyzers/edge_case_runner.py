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

    prompt = "\n".join([
        "You generate edge-case tests for a C programming assignment.",
        (
            "Carefully analyze the oracle C source and infer the exact "
            "standard-input format from its scanf, fgets, getchar, and "
            "input-parsing logic."
        ),
        (
            f"Generate exactly {count} distinct, tricky, valid edge-case "
            "inputs for THIS assignment."
        ),
        (
            "Each case should target a different meaningful edge condition "
            "for the inferred task. Consider the smallest valid size, "
            "duplicate values, already sorted data, reverse-sorted data, "
            "repeated values, zero or negative values when allowed, and "
            "boundary behavior relevant to the actual problem."
        ),
        (
            "Do not blindly use generic integer limits or invalid/empty "
            "stdin unless the assignment's input format supports them."
        ),
        (
            "Do not simply generate ordinary random cases. Make each case "
            "useful for finding a plausible edge-case bug."
        ),
        (
            "Every input must be a complete stdin payload ending with "
            "a newline. Follow the input format exactly."
        ),
        (
            'Return ONLY JSON in this schema: '
            '{"input_format":"brief format description",'
            '"test_cases":[{"input":"complete stdin payload"}]}'
        ),
        "Do not include expected outputs, explanations, markdown, or code fences.",
        "ORACLE C SOURCE START",
        source,
        "ORACLE C SOURCE END",
    ])

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
            "seed": 42,
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