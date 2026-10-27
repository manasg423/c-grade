
"""C-Grade I/O correctness analyzer.

Uses the paths already written by ui.py into each problem's rules JSON:
  params.testset_dir -> tests/<problem>/
  params.oracle_path  -> tests/<problem>/oracle.c

No per-assignment rules edits are required. Teacher-authored *.in/*.out pairs
are preferred. If auto-generation is enabled, a compiled oracle supplies the
expected output for deterministic generated inputs.

This analyzer supports C programs only. It does not claim that arbitrary input
formats can be inferred perfectly from C source; source-based inference is a
best-effort fallback, and all generated cases are compared against the oracle.
"""
from __future__ import annotations

import glob
import os
import random
import signal
import subprocess
import tempfile
import time
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - resource is Unix-specific
    resource = None


def _positive_number(value: Any, default: float) -> float:
    try:
        result = float(value)
        if result > 0 and result < float("inf"):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _positive_int(value: Any, default: int) -> int:
    try:
        result = int(value)
        if result > 0:
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _result(score: float, evidence: dict, feedback: str) -> dict:
    """Keep the analyzer's shared result contract consistent."""
    try:
        score = float(score)
    except (TypeError, ValueError, OverflowError):
        score = 0.0
    return {
        "score": max(0.0, min(1.0, score)),
        "evidence": evidence if isinstance(evidence, dict) else {},
        "feedback": str(feedback),
    }


def _inconclusive(reason: str, **evidence: Any) -> dict:
    data = {
        "evaluation_status": "inconclusive",
        "error": str(reason),
        "tests_passed": 0,
        "tests_total": 0,
        "test_source": "none",
        "executed_tests": [],
        "failing": [],
    }
    data.update(evidence)
    return _result(0.0, data, f"I/O evaluation inconclusive: {reason}")


def _resource_limiter(limits: dict):
    """Apply best-effort POSIX limits to the child process, not the grader."""
    if resource is None:
        return None

    cpu_s = _positive_int(limits.get("cpu_limit_s", 2), 2)
    memory_mb = _positive_int(limits.get("memory_limit_mb", 64), 64)
    output_mb = _positive_int(limits.get("output_limit_mb", 2), 2)

    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
        if hasattr(resource, "RLIMIT_AS"):
            mem = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        if hasattr(resource, "RLIMIT_FSIZE"):
            out = output_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
        if hasattr(resource, "RLIMIT_NOFILE"):
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    return apply


def _terminate_process_group(proc: subprocess.Popen) -> None:
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
    """Run with timeout, process-group cleanup, and bounded file-backed output."""
    start = time.monotonic()
    output_limit = (
        _positive_int(limits.get("output_limit_mb", 2), 2) * 1024 * 1024
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
                _terminate_process_group(proc)
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "",
                    "returncode": proc.returncode,
                    "reason": f"Timed out after {timeout_s:.2f}s",
                    "elapsed_s": round(time.monotonic() - start, 6),
                }

            stdout_file.seek(0)
            stderr_file.seek(0)

            stdout_raw = stdout_file.read(output_limit + 1)
            stderr_raw = stderr_file.read(output_limit + 1)

            exceeded = (
                len(stdout_raw) > output_limit
                or len(stderr_raw) > output_limit
            )

            stdout = stdout_raw[:output_limit].decode(
                "utf-8", errors="replace"
            )
            stderr = stderr_raw[:output_limit].decode(
                "utf-8", errors="replace"
            )

            if exceeded:
                return {
                    "ok": False,
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": proc.returncode,
                    "reason": "Output exceeded configured output limit",
                    "elapsed_s": round(time.monotonic() - start, 6),
                }

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
                "elapsed_s": round(time.monotonic() - start, 6),
            }

    except FileNotFoundError as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "reason": f"Executable not found: {exc}",
            "elapsed_s": round(time.monotonic() - start, 6),
        }

    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "reason": f"Execution error: {type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - start, 6),
        }


def _compile_oracle(
    oracle_path: str,
    output_path: str,
    params: dict,
    limits: dict,
) -> dict:
    compiler = str(params.get("compiler", "gcc"))
    flags = params.get("oracle_compile_flags", ["-O0", "-std=c11"])

    if isinstance(flags, str):
        flags = flags.split()

    if not isinstance(flags, list) or not all(
        isinstance(item, str) for item in flags
    ):
        return {
            "ok": False,
            "reason": (
                "oracle_compile_flags must be a list of strings or a string"
            ),
        }

    cmd = [compiler, *flags, oracle_path, "-o", output_path, "-lm"]
    timeout = _positive_number(limits.get("compile_timeout_s", 10), 10)

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": f"Oracle compilation timed out after {timeout:.1f}s",
        }

    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "reason": (
                f"Could not compile oracle: {type(exc).__name__}: {exc}"
            ),
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": "Oracle compilation failed",
            "compiler_stderr": proc.stderr[-6000:],
        }

    return {
        "ok": True,
        "compiler_stderr": proc.stderr[-2000:],
    }


def _read_c_source(path: str, max_chars: int = 30000) -> str:
    try:
        return Path(path).read_text(
            encoding="utf-8", errors="replace"
        )[:max_chars]
    except OSError:
        return ""


def _ollama_generate_inputs(
    oracle_path: str,
    count: int,
    params: dict,
) -> tuple[list[str], dict]:
    """Ask local Ollama to infer stdin protocol and create ordinary cases."""
    source = _read_c_source(oracle_path)

    if not source.strip():
        raise ValueError("Oracle source is empty or could not be read")

    endpoint = str(
        params.get(
            "ollama_url",
            "http://localhost:11434/api/generate",
        )
    ).rstrip("/")

    if not endpoint.endswith("/api/generate"):
        endpoint += "/api/generate"

    model = str(params.get("ollama_model", "qwen3.5:2b"))
    timeout = _positive_number(params.get("ollama_timeout_s", 90), 90)

    prompt = "\n".join([
        "You generate NORMAL, EASY input cases for a C programming assignment.",
        "Infer the exact standard-input format by carefully reading the oracle C source.",
        (
            'Return ONLY valid JSON: {"input_format":"short protocol '
            'description","test_cases":[{"input":"complete stdin text"}]}'
        ),
        f"Generate exactly {count} distinct, ordinary, small, valid test cases.",
        (
            "Focus on basic correctness, not edge cases, stress tests, "
            "invalid input, or huge values."
        ),
        (
            "Follow the actual input protocol. If it reads n then n array "
            "elements, put n on the first line and exactly n elements "
            "on the next line."
        ),
        "Use small values and normal sizes (typically 3 to 8 elements for array tasks).",
        "Each input must end with a newline. No explanations, markdown, expected outputs, or C code.",
        "Do not invent an input format. The oracle source is the specification.",
        "ORACLE C SOURCE START",
        source,
        "ORACLE C SOURCE END",
    ])

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
        "think": False,
    }

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(
                response.read().decode("utf-8", errors="replace")
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
            "Ollama did not return valid JSON in its response field"
        ) from exc

    cases = decoded.get("test_cases", []) if isinstance(decoded, dict) else []
    inputs = []

    for case in cases:
        if isinstance(case, dict) and isinstance(case.get("input"), str):
            value = case["input"]

            # Convert escaped separators returned literally by the model.
            value = (
                value.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\t", "\t")
            )

            if not value.endswith("\n"):
                value += "\n"

            if value.strip():
                inputs.append(value)

    inputs = list(dict.fromkeys(inputs))[:count]

    if not inputs:
        raise ValueError("Ollama returned no usable input cases")

    return inputs, {
        "model": model,
        "input_format": str(
            decoded.get("input_format", "unspecified")
        ),
    }


def _cached_llm_inputs(
    testset_dir: str,
    count: int,
) -> tuple[list[str], dict]:
    """Reuse cache files created by this LLM generator."""
    folder = Path(testset_dir) / "auto_generated"
    manifest = folder / ".ollama_manifest.json"

    if not manifest.is_file():
        return [], {}

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))

        if data.get("generator") != "ollama-qwen3.5-v1":
            return [], {}

        inputs = []

        for name in data.get("files", []):
            candidate = folder / str(name)

            if (
                candidate.suffix != ".in"
                or candidate.parent.resolve() != folder.resolve()
                or not candidate.is_file()
            ):
                continue

            value = candidate.read_text(
                encoding="utf-8", errors="replace"
            )

            if value.strip():
                inputs.append(value)

            if len(inputs) >= count:
                break

        return inputs, {
            "model": data.get("model", "qwen3.5"),
            "input_format": data.get("input_format", "cached"),
            "cache_reused": bool(inputs),
        }

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [], {}


def _save_llm_inputs(
    testset_dir: str,
    inputs: list[str],
    metadata: dict,
) -> str:
    folder = Path(testset_dir) / "auto_generated"
    folder.mkdir(parents=True, exist_ok=True)

    files = []

    for idx, value in enumerate(inputs, 1):
        name = f"gen_{idx}.in"
        (folder / name).write_text(value, encoding="utf-8")
        files.append(name)

    manifest = {
        "generator": "ollama-qwen3.5-v1",
        "model": metadata.get("model", "qwen3.5"),
        "input_format": metadata.get("input_format", "unspecified"),
        "files": files,
    }

    (folder / ".ollama_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return str(folder)


def _load_static_tests(
    testset_dir: str,
    recursive: bool = False,
) -> list[dict]:
    """Load paired test files; generated oracle files are not loaded twice."""
    if not testset_dir or not os.path.isdir(testset_dir):
        return []

    pattern = "**/*.in" if recursive else "*.in"
    paths = sorted(
        glob.glob(
            os.path.join(testset_dir, pattern),
            recursive=recursive,
        )
    )

    root = Path(testset_dir).resolve()
    tests = []

    for raw_path in paths:
        path = Path(raw_path)

        try:
            rel = path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue

        if "auto_generated" in rel.parts:
            continue

        out_path = path.with_suffix(".out")

        if not out_path.is_file():
            continue

        try:
            inp = path.read_text(
                encoding="utf-8", errors="replace"
            )
            expected = out_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue

        tests.append({
            "input": inp,
            "expected": expected,
            "source": "static",
            "label": str(rel),
        })

    return tests


def _load_rule_tests(params: dict) -> list[dict]:
    """Optional explicit test cases; existing UI need not configure these."""
    configured = params.get("test_cases", [])

    if not isinstance(configured, list):
        return []

    result = []

    for i, case in enumerate(configured, 1):
        if isinstance(case, dict) and "input" in case and "expected" in case:
            result.append({
                "input": str(case["input"]),
                "expected": str(case["expected"]),
                "source": "rules",
                "label": str(case.get("name", f"rule_{i}")),
            })

    return result


def _normalize_output(text: str, mode: str) -> Any:
    if mode == "exact":
        return text

    if mode == "strip":
        return text.strip()

    if mode == "tokens":
        return text.split()

    raise ValueError(
        "output_comparison must be 'tokens', 'strip', or 'exact'"
    )


def evaluate(
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict = None,
) -> dict:
    """Evaluate a compiled student C program using the existing C-Grade contract."""
    params = params if isinstance(params, dict) else {}
    limits = limits if isinstance(limits, dict) else {}

    if not binary_path or not os.path.isfile(binary_path):
        return _inconclusive(
            f"Student binary not found: {binary_path!r}"
        )

    timeout = _positive_number(
        limits.get("time_limit_s", 1.0), 1.0
    )
    seed = int(params.get("seed", 42))
    comparison = str(
        params.get("output_comparison", "tokens")
    ).lower()

    try:
        _normalize_output("", comparison)
    except ValueError as exc:
        return _inconclusive(str(exc))

    testset_dir = str(
        params.get("testset_dir", params.get("tests_dir", ""))
    )
    oracle_path = str(
        params.get("oracle_path", params.get("oracle_source", ""))
    )

    if not testset_dir and oracle_path:
        testset_dir = os.path.dirname(oracle_path)

    tests = _load_static_tests(
        testset_dir,
        bool(params.get("recursive_tests", False)),
    )
    tests.extend(_load_rule_tests(params))

    sources_used = {case["source"] for case in tests}
    oracle_evidence: dict[str, Any] = {}
    saved_auto_dir = ""

    auto_generate = bool(params.get("auto_generate", True))
    generated_count = min(
        _positive_int(
            params.get(
                "num_generated_tests",
                params.get("auto_generate_count", 5),
            ),
            5,
        ),
        100,
    )

    # Oracle is assumed correct. LLM-generated inputs are cached for later runs.
    if auto_generate and oracle_path and os.path.isfile(oracle_path):
        try:
            with tempfile.TemporaryDirectory(
                prefix="cgrade_io_oracle_"
            ) as temp_dir:
                oracle_binary = os.path.join(temp_dir, "oracle.bin")
                compiled = _compile_oracle(
                    oracle_path,
                    oracle_binary,
                    params,
                    limits,
                )

                oracle_evidence["oracle_compile_ok"] = compiled["ok"]

                if compiled.get("compiler_stderr"):
                    oracle_evidence["oracle_compiler_stderr"] = (
                        compiled["compiler_stderr"]
                    )

                if not compiled["ok"]:
                    oracle_evidence["oracle_error"] = compiled.get(
                        "reason", "Oracle compilation failed"
                    )

                    if compiled.get("compiler_stderr"):
                        oracle_evidence["oracle_error_stderr"] = (
                            compiled["compiler_stderr"]
                        )

                else:
                    inputs, metadata = (
                        _cached_llm_inputs(testset_dir, generated_count)
                        if testset_dir
                        else ([], {})
                    )

                    if not inputs:
                        inputs, metadata = _ollama_generate_inputs(
                            oracle_path,
                            generated_count,
                            params,
                        )

                        if testset_dir:
                            saved_auto_dir = _save_llm_inputs(
                                testset_dir,
                                inputs,
                                metadata,
                            )
                    else:
                        saved_auto_dir = os.path.join(
                            testset_dir, "auto_generated"
                        )

                    oracle_evidence["generated_input_type"] = (
                        metadata.get("input_format", "unspecified")
                    )
                    oracle_evidence["ollama_model"] = metadata.get(
                        "model",
                        params.get("ollama_model", "qwen3.5"),
                    )
                    oracle_evidence["generated_input_cache_reused"] = bool(
                        metadata.get("cache_reused", False)
                    )

                    oracle_failures = []
                    generated_ok = 0
                    validated_inputs = []

                    for idx, inp in enumerate(inputs, 1):
                        oracle_run = _run_process(
                            [oracle_binary],
                            inp,
                            timeout,
                            limits,
                        )

                        if not oracle_run["ok"]:
                            oracle_failures.append({
                                "case": idx,
                                "reason": oracle_run["reason"],
                                "returncode": oracle_run["returncode"],
                            })
                            continue

                        label = f"gen_{idx}"
                        expected = oracle_run["stdout"]

                        if saved_auto_dir:
                            try:
                                Path(
                                    saved_auto_dir, f"{label}.in"
                                ).write_text(inp, encoding="utf-8")

                                Path(
                                    saved_auto_dir, f"{label}.out"
                                ).write_text(expected, encoding="utf-8")

                            except OSError as exc:
                                oracle_evidence["save_warning"] = str(exc)

                        tests.append({
                            "input": inp,
                            "expected": expected,
                            "source": "auto_generated",
                            "label": f"{label}.in",
                        })

                        validated_inputs.append(inp)
                        generated_ok += 1

                    if saved_auto_dir and validated_inputs:
                        try:
                            _save_llm_inputs(
                                testset_dir,
                                validated_inputs,
                                metadata,
                            )
                        except OSError as exc:
                            oracle_evidence["save_warning"] = str(exc)

                    oracle_evidence["oracle_cases_generated"] = generated_ok

                    if oracle_failures:
                        oracle_evidence["oracle_case_errors"] = (
                            oracle_failures
                        )

                    if generated_ok:
                        sources_used.add("auto_generated")

        except Exception as exc:
            oracle_evidence["generation_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

    if not tests:
        reason = (
            "No usable test cases were found. The default UI paths were "
            "checked, but there were no paired .in/.out files and no usable "
            "oracle-generated tests."
        )
        return _inconclusive(reason, **oracle_evidence)

    passed = 0
    failing = []
    executed = []

    for case in tests:
        run = _run_process(
            [binary_path],
            case["input"],
            timeout,
            limits,
        )

        output_matches = False

        if run["ok"]:
            try:
                output_matches = (
                    _normalize_output(run["stdout"], comparison)
                    == _normalize_output(case["expected"], comparison)
                )
            except ValueError:
                output_matches = False

        success = run["ok"] and output_matches

        record = {
            "test_file": case["label"],
            "source": case["source"],
            "input": case["input"],
            "expected": case["expected"],
            "got": run["stdout"],
            "status": "PASSED" if success else "FAILED",
            "returncode": run["returncode"],
            "elapsed_s": run["elapsed_s"],
        }

        if run["reason"]:
            record["note"] = run["reason"]

        if run["stderr"]:
            record["stderr"] = run["stderr"][-4000:]

        executed.append(record)

        if success:
            passed += 1
        else:
            failing.append(record)

    total = len(tests)
    score = passed / total
    source_label = (
        "+".join(sorted(sources_used))
        if sources_used
        else "unknown"
    )

    if passed == total:
        feedback = (
            f"Passed all {passed}/{total} I/O tests ({source_label})."
        )
    else:
        feedback = (
            f"Passed {passed}/{total} I/O tests ({source_label}); "
            f"{len(failing)} test(s) failed."
        )

    return _result(score, {
        "evaluation_status": "completed",
        "tests_passed": passed,
        "tests_total": total,
        "test_source": source_label,
        "output_comparison": comparison,
        "seed": seed,
        "saved_auto_tests_dir": saved_auto_dir,
        "executed_tests": executed,
        "failing": failing,
        **oracle_evidence,
    }, feedback)