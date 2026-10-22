import os
import re
import glob
import random
import tempfile
import subprocess


def _strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"//.*", "", code)
    return code


def _detect_input_format(oracle_path: str, params: dict) -> tuple[str, int]:
    """
    Determines the input format from params['input_type'] or by inspecting
    scanf/fgets calls in oracle.c.
    Returns (format_type, count_of_scalars).
    """
    explicit_type = params.get("input_type", "").strip().lower()
    if explicit_type in ("string", "single_number", "fixed_arithmetic", "integer_array", "raw_array"):
        return explicit_type, 2 if explicit_type == "fixed_arithmetic" else 1

    if not oracle_path or not os.path.exists(oracle_path):
        return "single_number", 1

    try:
        with open(oracle_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_code = f.read()
        code = _strip_comments(raw_code)

        # 1. Check for string input
        if re.search(r"%\d*s|\bfgets\s*\(|\bgets\s*\(|\bgetline\s*\(", code):
            return "string", 1

        # 2. Check if there is a loop containing scanf (array problem: N followed by N elements)
        loop_scanf = re.search(r"\b(for|while)\b[^{]*\{[^}]*\bscanf\s*\(", code, flags=re.DOTALL)
        if loop_scanf:
            return "integer_array", 1

        # 3. Count integer/float format specifiers across all scanf calls
        scanf_strings = re.findall(r'\bscanf\s*\(\s*"([^"]*)"', code)
        fmt_joined = " ".join(scanf_strings)
        int_specs = re.findall(r"%(?:ll|l|h|hh)?[diu]", fmt_joined)
        float_specs = re.findall(r"%(?:l|L)?[feEgG]", fmt_joined)

        total_nums = len(int_specs) + len(float_specs)
        if total_nums == 1:
            return "single_number", 1
        elif total_nums >= 2:
            return "fixed_arithmetic", total_nums

    except Exception:
        pass

    return "single_number", 1


def _generate_inputs(fmt_type: str, num_scalars: int, count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    inputs = []

    if fmt_type == "string":
        pool = [
            "hello",
            "a",
            "racecar",
            "cgrade_testing_123",
            "OpenAI_Lab_Test",
            "abcdefghijklmnopqrstuvwxyz"
        ]
        for i in range(count):
            if i < len(pool):
                inputs.append(f"{pool[i]}\n")
            else:
                length = rng.randint(4, 20)
                s = "".join(chr(rng.randint(97, 122)) for _ in range(length))
                inputs.append(f"{s}\n")

    elif fmt_type == "single_number":
        curated = [7, 12, 29, 1, 97, 100, 2, 49, 0, 53]
        for i in range(count):
            if i < len(curated):
                inputs.append(f"{curated[i]}\n")
            else:
                inputs.append(f"{rng.randint(2, 500)}\n")

    elif fmt_type == "integer_array":
        for i in range(count):
            n = rng.randint(5, 12)
            arr = [str(rng.randint(-50, 100)) for _ in range(n)]
            inputs.append(f"{n}\n" + " ".join(arr) + "\n")

    elif fmt_type == "raw_array":
        for i in range(count):
            n = rng.randint(5, 12)
            arr = [str(rng.randint(-50, 100)) for _ in range(n)]
            inputs.append(" ".join(arr) + "\n")

    else:
        # fixed_arithmetic (2 or more space-separated numbers)
        k = max(2, num_scalars)
        for i in range(count):
            if i == 0:
                nums = [str(rng.randint(1, 50)) for _ in range(k)]
            elif i == 1:
                nums = ["0" for _ in range(k)]
            else:
                nums = [str(rng.randint(-500, 500)) for _ in range(k)]
            inputs.append(" ".join(nums) + "\n")

    return inputs


def _run_bin(binary_path: str, stdin_data: str, timeout: float) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            [binary_path],
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if proc.returncode != 0:
            return False, proc.stdout.strip(), f"Non-zero exit code ({proc.returncode})"
        return True, proc.stdout.strip(), ""
    except subprocess.TimeoutExpired:
        return False, "", "Execution timed out"
    except Exception as e:
        return False, "", str(e)


def evaluate(source_path: str, binary_path: str, params: dict, limits: dict = None) -> dict:
    if limits is None:
        limits = {}

    if not binary_path or not os.path.exists(binary_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Binary not found: {binary_path}"},
            "feedback": "Evaluation failed: compiled student binary missing."
        }

    testset_dir = params.get("testset_dir", params.get("tests_dir", ""))
    oracle_path = params.get("oracle_path", params.get("oracle_source", ""))
    auto_generate = params.get("auto_generate", True)
    num_generated = int(params.get("num_generated_tests", params.get("auto_generate_count", 5)))
    seed = int(params.get("seed", 42))
    timeout = float(limits.get("time_limit_s", 1.0))

    if not testset_dir and oracle_path:
        testset_dir = os.path.dirname(oracle_path)

    test_cases = []  # list of (input_str, expected_output_str, source_tag, file_label)
    sources_used = []
    auto_gen_dir = ""

    # 1. Load static .in / .out files from top-level testset_dir
    if testset_dir and os.path.isdir(testset_dir):
        in_files = sorted(glob.glob(os.path.join(testset_dir, "*.in")))
        for in_f in in_files:
            base = os.path.splitext(in_f)[0]
            out_f = base + ".out"
            if os.path.exists(out_f):
                with open(in_f, "r", encoding="utf-8", errors="ignore") as fi:
                    inp_str = fi.read()
                with open(out_f, "r", encoding="utf-8", errors="ignore") as fo:
                    exp_str = fo.read().strip()
                test_cases.append((inp_str, exp_str, "static", os.path.basename(in_f)))
        if test_cases:
            sources_used.append("static")

    # 2. Auto-generate tests via teacher oracle.c and save them to tests/<problem>/auto_generated/
    if (auto_generate or not test_cases) and oracle_path and os.path.exists(oracle_path):
        oracle_bin = os.path.join(tempfile.gettempdir(), f"oracle_{os.getpid()}.out")
        try:
            comp = subprocess.run(
                ["gcc", "-O0", oracle_path, "-o", oracle_bin, "-lm"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if comp.returncode == 0 and os.path.exists(oracle_bin):
                fmt_type, num_scalars = _detect_input_format(oracle_path, params)
                gen_inputs = _generate_inputs(fmt_type, num_scalars, num_generated, seed)

                if testset_dir:
                    auto_gen_dir = os.path.join(testset_dir, "auto_generated")
                    os.makedirs(auto_gen_dir, exist_ok=True)

                for idx, ginp in enumerate(gen_inputs, start=1):
                    ok, oracle_out, _ = _run_bin(oracle_bin, ginp, timeout)
                    if ok:
                        label = f"gen_{idx}.in"
                        # Save generated .in and .out files to disk
                        if auto_gen_dir:
                            in_file_path = os.path.join(auto_gen_dir, f"gen_{idx}.in")
                            out_file_path = os.path.join(auto_gen_dir, f"gen_{idx}.out")
                            with open(in_file_path, "w", encoding="utf-8") as fi:
                                fi.write(ginp)
                            with open(out_file_path, "w", encoding="utf-8") as fo:
                                fo.write(oracle_out + "\n")

                        test_cases.append((ginp, oracle_out, "auto_generated", label))

                if gen_inputs:
                    sources_used.append("auto_generated")
        finally:
            if os.path.exists(oracle_bin):
                try:
                    os.remove(oracle_bin)
                except OSError:
                    pass

    if not test_cases:
        return {
            "score": 0.0,
            "evidence": {
                "tests_passed": 0,
                "tests_total": 0,
                "test_source": "none",
                "executed_tests": [],
                "failing": []
            },
            "feedback": "No static test files or valid oracle found to evaluate I/O correctness."
        }

    # 3. Run student binary against all collected test cases
    passed = 0
    failing = []
    executed_tests = []

    for inp_str, expected_out, src_tag, label in test_cases:
        ok, got_out, note = _run_bin(binary_path, inp_str, timeout)
        is_pass = ok and (got_out == expected_out)

        case_record = {
            "test_file": label,
            "source": src_tag,
            "input": inp_str.strip(),
            "expected": expected_out,
            "got": got_out,
            "status": "PASSED" if is_pass else "FAILED"
        }
        if note:
            case_record["note"] = note
        executed_tests.append(case_record)

        if is_pass:
            passed += 1
        else:
            failing.append({
                "test_file": label,
                "input": inp_str.strip(),
                "expected": expected_out,
                "got": got_out,
                "note": note
            })

    total = len(test_cases)
    score = round(passed / total, 2) if total > 0 else 0.0
    source_label = "+".join(sources_used) if sources_used else "unknown"

    if passed == total:
        feedback = f"Passed all {total}/{total} I/O test cases ({source_label})."
    else:
        feedback = f"Passed {passed}/{total} I/O test cases ({source_label}). {len(failing)} test(s) failed."

    return {
        "score": score,
        "evidence": {
            "tests_passed": passed,
            "tests_total": total,
            "test_source": source_label,
            "saved_auto_tests_dir": auto_gen_dir,
            "executed_tests": executed_tests,
            "failing": failing
        },
        "feedback": feedback
    }