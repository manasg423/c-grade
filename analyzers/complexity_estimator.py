import math
import os
import random
import re
import shutil
import statistics
import subprocess
import tempfile
from typing import Optional


_VALID_INPUT_TYPES = {
    "string",
    "integer_array",
    "raw_array",
    "single_number",
    "fixed_arithmetic",
}


def _strip_comments(code: str) -> str:
    """Legacy fallback only. This simple comment stripper is not a C parser."""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"//.*", "", code)
    return code


def _replace_c_function_calls(code: str, replacements: dict[str, str]) -> str:
    """Replace named function-call identifiers while preserving C strings/comments.

    This is a small lexical scanner, not a full C parser. It avoids rewriting text
    inside string literals, character literals, and comments.
    """
    out = []
    i = 0
    n = len(code)
    state = "normal"

    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if state == "normal":
            if ch == '"':
                state = "string"
                out.append(ch)
                i += 1
                continue
            if ch == "'":
                state = "char"
                out.append(ch)
                i += 1
                continue
            if ch == "/" and nxt == "/":
                state = "line_comment"
                out.extend([ch, nxt])
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                out.extend([ch, nxt])
                i += 2
                continue

            if ch.isalpha() or ch == "_":
                j = i + 1
                while j < n and (code[j].isalnum() or code[j] == "_"):
                    j += 1
                ident = code[i:j]
                k = j
                while k < n and code[k].isspace():
                    k += 1
                if ident in replacements and k < n and code[k] == "(":
                    out.append(replacements[ident])
                else:
                    out.append(ident)
                i = j
                continue

            out.append(ch)
            i += 1
            continue

        if state == "string":
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if ch == '"':
                state = "normal"
            i += 1
            continue

        if state == "char":
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if ch == "'":
                state = "normal"
            i += 1
            continue

        if state == "line_comment":
            out.append(ch)
            i += 1
            if ch == "\n":
                state = "normal"
            continue

        if state == "block_comment":
            out.append(ch)
            if ch == "*" and nxt == "/":
                out.append(nxt)
                i += 2
                state = "normal"
            else:
                i += 1

    return "".join(out)


def _auto_detect_input_type(source_path: str, configured_type: Optional[str]) -> str:
    """Respect explicit valid configuration; otherwise make a best-effort guess."""
    if configured_type in _VALID_INPUT_TYPES:
        return configured_type

    if not source_path or not os.path.exists(source_path):
        return "string"

    try:
        with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
            # For detection, strip comments only after protecting quoted strings.
            code = f.read()
        code = _strip_comments(code)

        # String-like input commonly uses %s, %c, fgets/gets/getchar, or getline.
        if re.search(r"%\d*s|%s|\bfgets\s*\(|\bgets\s*\(|\bgetline\s*\(|\bgetchar\s*\(|%c", code):
            return "string"

        # An input read inside a loop is a useful, but not conclusive, array clue.
        if re.search(r"\b(for|while)\b[^{}]*\{[^}]*\bscanf\s*\(", code, flags=re.DOTALL):
            return "integer_array"

        scanf_calls = re.findall(r'\bscanf\s*\(\s*"([^"]*)"', code)
        joined = " ".join(scanf_calls)
        num_specs = re.findall(r"%(?:ll|l|h|hh)?[diufegFEG]", joined)

        if len(num_specs) == 1:
            return "single_number"
        if len(num_specs) >= 2:
            return "fixed_arithmetic"

    except Exception:
        pass

    # Keep the historical default when the source does not provide enough clues.
    return "string"


def _get_trial_sizes(input_type: str, trials: int) -> list[int]:
    trials = max(2, int(trials))
    if input_type == "single_number":
        primes = [13, 127, 1009, 10007, 104729, 250007, 500009]
        if trials <= len(primes):
            return primes[:trials]
        # Extend with larger values while retaining the same monotonic input family.
        sizes = list(primes)
        candidate = primes[-1] + 2
        while len(sizes) < trials:
            is_prime = True
            limit = int(math.sqrt(candidate))
            for d in range(3, limit + 1, 2):
                if candidate % d == 0:
                    is_prime = False
                    break
            if is_prime:
                sizes.append(candidate)
            candidate += 2
        return sizes

    # Keep within a range that is reasonably safe for common fixed-size C buffers.
    return [10 + i * 20 for i in range(trials)]


def _generate_scaled_input(input_type: str, n: int, seed: int = 42) -> str:
    rng = random.Random(seed + n)

    if input_type == "string":
        chars = [chr(rng.randint(97, 122)) for _ in range(n)]
        return "".join(chars) + "\n"

    if input_type == "integer_array":
        elements = [str(rng.randint(1, 100)) for _ in range(n)]
        return f"{n}\n" + " ".join(elements) + "\n"

    if input_type == "raw_array":
        elements = [str(rng.randint(1, 100)) for _ in range(n)]
        return " ".join(elements) + "\n"

    if input_type == "single_number":
        return f"{n}\n"

    if input_type == "fixed_arithmetic":
        return f"{n} {n + 7}\n"

    chars = [chr(rng.randint(97, 122)) for _ in range(n)]
    return "".join(chars) + "\n"


def _clang_instrumented_source(raw_code: str) -> str:
    """Prepare source for Clang SanitizerCoverage edge counting.

    Also redirect direct strlen(...) calls to an instrumented equivalent, because
    the platform C library itself is usually not compiled with our coverage hooks.
    """
    code = _replace_c_function_calls(raw_code, {"strlen": "__cg_strlen"})

    prefix = r'''#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

static volatile unsigned long long __cg_steps = 0ULL;
static uint32_t __cg_next_guard = 1;

__attribute__((no_sanitize("coverage")))
void __sanitizer_cov_trace_pc_guard_init(uint32_t *start, uint32_t *stop) {
    if (start == stop || *start) return;
    for (uint32_t *p = start; p < stop; ++p) *p = __cg_next_guard++;
}

__attribute__((no_sanitize("coverage")))
void __sanitizer_cov_trace_pc_guard(uint32_t *guard) {
    if (guard && *guard) __cg_steps++;
}

__attribute__((no_sanitize("coverage"), destructor))
static void __cg_dump_steps(void) {
    fprintf(stderr, "\n__CGRADE_STEPS__=%llu\n", __cg_steps);
}

static size_t __cg_strlen(const char *s);
'''

    suffix = r'''

/* Count the work normally hidden inside the system strlen() implementation. */
__attribute__((no_sanitize("coverage")))
static size_t __cg_strlen(const char *s) {
    const char *p = s;
    while (*p != '\0') {
        __cg_steps++;
        ++p;
    }
    /* Count inspection of the terminating NUL as one additional character read. */
    __cg_steps++;
    return (size_t)(p - s);
}
'''
    return prefix + "\n" + code + "\n" + suffix


def _instrument_c_code(raw_code: str) -> str:
    """Legacy GCC fallback; regex instrumentation is less reliable than Clang."""
    code = _replace_c_function_calls(raw_code, {"strlen": "__cg_strlen"})
    code = _strip_comments(code)

    header = r'''#include <stdio.h>
#include <stddef.h>
static volatile unsigned long long __cg_steps = 0ULL;
__attribute__((destructor)) static void __cg_dump(void) {
    fprintf(stderr, "\n__CGRADE_STEPS__=%llu\n", __cg_steps);
}
static size_t __cg_strlen(const char *s) {
    const char *p = s;
    while (*p != '\0') { __cg_steps++; ++p; }
    __cg_steps++;
    return (size_t)(p - s);
}
'''

    # Best-effort wrapping for simple loops with a single statement body.
    code = re.sub(
        r"\bwhile\s*\(([^)]+)\)\s*([^;{]+;)",
        r"while (\1) { \2 }",
        code,
    )
    code = re.sub(
        r"\bfor\s*\(([^)]+)\)\s*([^;{]+;)",
        r"for (\1) { \2 }",
        code,
    )
    return header + "\n" + code.replace("{", "{ __cg_steps++; ")


def _run_profiled_program(binary_path: str, stdin_data: str, timeout: float) -> Optional[int]:
    try:
        proc = subprocess.run(
            [binary_path],
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    match = re.search(r"__CGRADE_STEPS__=(\d+)", proc.stderr)
    if not match:
        return None
    return max(1, int(match.group(1)))


def _profile_exact_steps(
    source_path: str,
    input_type: str,
    sizes: list[int],
    seed: int,
    timeout: float,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Return per-input execution-cost proxies and the measurement method."""
    if not source_path or not os.path.exists(source_path):
        return None, None

    try:
        with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_code = f.read()
    except OSError:
        return None, None

    temp_dir = tempfile.mkdtemp(prefix="cgrade_steps_")
    source_dir = os.path.dirname(os.path.abspath(source_path))
    try:
        inst_src = os.path.join(temp_dir, "inst.c")
        inst_bin = os.path.join(temp_dir, "inst.out")
        compiler = shutil.which("clang")
        method = "compiler_edge_counter"

        if compiler:
            with open(inst_src, "w", encoding="utf-8") as f:
                f.write(_clang_instrumented_source(raw_code))
            command = [
                compiler,
                "-O0",
                "-fno-inline",
                "-fno-builtin",
                "-fsanitize-coverage=trace-pc-guard,no-prune",
                "-I",
                source_dir,
                inst_src,
                "-o",
                inst_bin,
                "-lm",
            ]
            comp = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            comp = None

        # If Clang is unavailable or rejects the source/extensions, try the older GCC path.
        if not compiler or comp is None or comp.returncode != 0 or not os.path.exists(inst_bin):
            compiler = shutil.which("gcc")
            if not compiler:
                return None, None
            method = "legacy_source_instrumentation"
            with open(inst_src, "w", encoding="utf-8") as f:
                f.write(_instrument_c_code(raw_code))
            comp = subprocess.run(
                [compiler, "-O0", inst_src, "-o", inst_bin, "-lm", "-I", source_dir],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if comp.returncode != 0 or not os.path.exists(inst_bin):
                return None, None

        measurements = []
        for n in sizes:
            inp = _generate_scaled_input(input_type, n, seed)
            steps = _run_profiled_program(inst_bin, inp, timeout)
            if steps is None:
                return None, None
            measurements.append(
                {
                    "n": n,
                    "cost": steps,
                    "generated_input": inp.strip(),
                }
            )

        return measurements, method

    except Exception:
        return None, None
    finally:
        try:
            for root, _, files in os.walk(temp_dir, topdown=False):
                for name in files:
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError:
                        pass
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        except OSError:
            pass


def _run_with_timing(binary_path: str, stdin_data: str, timeout: float) -> int:
    """Timing fallback. Process-startup cost can obscure tiny algorithmic work."""
    import time

    samples = []
    # A few repetitions reduce (but do not eliminate) timer noise.
    for _ in range(3):
        start = time.perf_counter_ns()
        try:
            subprocess.run(
                [binary_path],
                input=stdin_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except Exception:
            pass
        samples.append(max(1, time.perf_counter_ns() - start))
    return max(1, int(statistics.median(samples)))


def _model_feature(complexity: str, n: float) -> float:
    n = max(2.0, float(n))
    if complexity == "O(1)":
        return 1.0
    if complexity == "O(log N)":
        return math.log2(n)
    if complexity == "O(N)":
        return n
    if complexity == "O(N log N)":
        return n * math.log2(n)
    if complexity == "O(N^2)":
        return n * n
    if complexity == "O(N^3)":
        return n * n * n
    return 1.0


def _fit_model_error(ns: list[float], costs: list[float], complexity: str) -> float:
    """Fit cost ~= intercept + scale*f(n), then return mean log-relative error."""
    if complexity == "O(1)":
        prediction = [max(1.0, statistics.mean(costs))] * len(costs)
    else:
        xs = [_model_feature(complexity, n) for n in ns]
        x_mean = statistics.mean(xs)
        y_mean = statistics.mean(costs)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        if denominator <= 1e-12:
            prediction = [max(1.0, y_mean)] * len(costs)
        else:
            slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, costs)) / denominator
            slope = max(0.0, slope)
            intercept = y_mean - slope * x_mean
            if intercept < 0:
                intercept = 0.0
                x_sq = sum(x * x for x in xs)
                slope = (sum(x * y for x, y in zip(xs, costs)) / x_sq) if x_sq else 0.0
            prediction = [max(1.0, intercept + slope * x) for x in xs]

    errors = [abs(math.log(max(1.0, p) / max(1.0, y))) for p, y in zip(prediction, costs)]
    return statistics.mean(errors)


def _detect_complexity(measurements: list[dict]) -> str:
    """Choose the simplest growth model close to the best fit across all trials."""
    if len(measurements) < 3:
        return "O(1)"

    ns = [float(m["n"]) for m in measurements]
    costs = [float(max(1, m["cost"])) for m in measurements]
    if max(costs) / min(costs) < 1.20:
        return "O(1)"

    candidates = ["O(1)", "O(log N)", "O(N)", "O(N log N)", "O(N^2)", "O(N^3)"]
    errors = {name: _fit_model_error(ns, costs, name) for name in candidates}
    best_error = min(errors.values())

    # Prefer a simpler class when its fit is practically indistinguishable.
    tolerance = 0.035
    for candidate in candidates:
        if errors[candidate] <= best_error + tolerance:
            return candidate
    return min(errors, key=errors.get)


def evaluate(source_path: str, binary_path: str, params: dict, limits: dict = None) -> dict:
    """Estimate asymptotic complexity while preserving the original API/schema.

    Required external input/output shape remains:
      evaluate(source_path, binary_path, params, limits=None) ->
        {"score": float, "evidence": {...}, "feedback": str}
    """
    if limits is None:
        limits = {}

    if not os.path.exists(source_path) and not os.path.exists(binary_path):
        return {
            "score": 0.0,
            "evidence": {"error": "Neither source nor binary found."},
            "feedback": "Evaluation failed: missing files.",
        }

    # No configured input_type means "auto"; explicit valid values still win.
    configured_type = params.get("input_type")
    input_type = _auto_detect_input_type(source_path, configured_type)
    expected_complexity = params.get("expected_complexity", "O(N)")
    trials = int(params.get("trials", 5))
    seed = int(params.get("seed", 42))
    timeout = float(limits.get("time_limit_s", 2.0))

    sizes = _get_trial_sizes(input_type, trials)

    measurements, mode = _profile_exact_steps(source_path, input_type, sizes, seed, timeout)

    if not measurements:
        mode = "time_ns_fallback"
        measurements = []
        if os.path.exists(binary_path):
            for n in sizes:
                inp = _generate_scaled_input(input_type, n, seed)
                measurements.append(
                    {
                        "n": n,
                        "cost": _run_with_timing(binary_path, inp, timeout),
                        "generated_input": inp.strip(),
                    }
                )
        else:
            return {
                "score": 0.0,
                "evidence": {"error": "Profiling failed and no executable binary was found."},
                "feedback": "Evaluation failed: could not compile or profile the source, and no binary was available.",
            }

    first_cost = measurements[0]["cost"]
    last_cost = measurements[-1]["cost"]
    first_n = measurements[0]["n"]
    last_n = measurements[-1]["n"]

    n_ratio = last_n / max(1, first_n)
    cost_ratio = last_cost / max(1, first_cost)
    detected = _detect_complexity(measurements)

    rank = {
        "O(1)": 1,
        "O(log N)": 2,
        "O(N)": 3,
        "O(N log N)": 4,
        "O(N^2)": 5,
        "O(N^3)": 6,
    }

    exp_rank = rank.get(expected_complexity, 3)
    det_rank = rank.get(detected, 3)

    if det_rank == exp_rank:
        score = 1.0
        feedback = (
            f"Expected {detected} scaling is consistent with the measured growth "
            f"(execution-cost proxy grew {cost_ratio:.2f}x over {n_ratio:.2f}x input growth)."
        )
    elif det_rank < exp_rank:
        if expected_complexity != "O(1)" and detected == "O(1)":
            score = 0.5
            feedback = (
                f"Expected {expected_complexity} scaling, but observed nearly constant execution cost; "
                "check whether the generated input reaches the input-dependent code path."
            )
        else:
            score = 1.0
            feedback = f"Better-than-expected {detected} scaling observed (target was {expected_complexity})."
    else:
        score = 0.4
        feedback = (
            f"Suboptimal scaling: expected {expected_complexity}, but observed {detected} "
            f"(execution-cost proxy grew {cost_ratio:.2f}x over {n_ratio:.2f}x input growth)."
        )

    return {
        "score": score,
        "evidence": {
            "input_type": input_type,
            "profiling_method": mode,
            "expected_complexity": expected_complexity,
            "detected_complexity": detected,
            "cost_growth_ratio": round(cost_ratio, 2),
            "input_growth_ratio": round(n_ratio, 2),
            "trials": measurements,
        },
        "feedback": feedback,
    }
