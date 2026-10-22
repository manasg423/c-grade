import os
import glob
import json
import random
import subprocess
import requests
from core.sandbox import run_safe

def _synthesize_inputs_via_llm_and_seed(oracle_code: str, num_tests: int, seed: int) -> list:
    inputs = []
    
    # 1. Ask Ollama to synthesize inputs based on the C oracle
    prompt = (
        "Look at this reference C solution and output valid, varied standard input (stdin) lines.\n"
        f"Provide exactly {min(num_tests, 5)} distinct, realistic test input lines separated by newlines.\n"
        "Output ONLY the raw input lines. No markdown, no quotes, no explanations.\n\n"
        f"```c\n{oracle_code[:700]}\n```"
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
                    "seed": seed
                }
            },
            timeout=5.0
        )
        if res.status_code == 200:
            lines = res.json().get("response", "").strip().splitlines()
            for line in lines:
                cleaned = line.strip().strip('"').strip("'")
                if cleaned and not cleaned.startswith("`") and cleaned not in inputs:
                    inputs.append(cleaned)
    except Exception:
        pass

    # 2. Check if this problem works with strings or numbers
    is_string_problem = any(kw in oracle_code for kw in ["char", "%s", "strlen", "string"])

    # 3. Deterministic Seeded PRNG fallback if more inputs are needed
    rng = random.Random(seed)
    words = ["hello", "algorithm", "C-Grade", "data_structures", "IIT_Patna", "supercalifragilistic", "programming"]

    while len(inputs) < num_tests:
        if is_string_problem:
            candidate = words[rng.randint(0, len(words) - 1)] + f"_{rng.randint(1, 99)}"
        else:
            candidate = f"{rng.randint(-1000, 1000)} {rng.randint(-1000, 1000)}"
            
        if candidate not in inputs:
            inputs.append(candidate)

    return inputs[:num_tests]

def _compile_oracle(oracle_path: str) -> str | None:
    if not os.path.exists(oracle_path):
        return None
    oracle_bin = "/tmp/c_grade_bin/io_oracle_bin"
    os.makedirs(os.path.dirname(oracle_bin), exist_ok=True)
    comp = subprocess.run(["gcc", "-O2", oracle_path, "-o", oracle_bin], capture_output=True, text=True)
    return oracle_bin if comp.returncode == 0 else None

def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    testset_dir = params.get("testset_dir", "tests/str_len/")
    timeout_s = limits.get("time_limit_s", 1.0)
    memory_mb = limits.get("memory_limit_mb", 64)
    seed = params.get("seed", 42)
    num_tests = params.get("num_generated_tests", 5)

    # Resolve oracle path cleanly
    oracle_path = params.get("oracle_path")
    if not oracle_path or not os.path.exists(oracle_path):
        oracle_path = os.path.join(testset_dir.rstrip("/"), "oracle.c")

    # Check for manual test files first
    in_files = sorted([f for f in glob.glob(os.path.join(testset_dir, "*.in")) if "auto_generated" not in f])

    # If no manual tests, auto-generate via Oracle + LLM/PRNG
    if not in_files or params.get("auto_generate", False):
        oracle_bin = _compile_oracle(oracle_path)

        if not oracle_bin:
            return {
                "score": 0.0,
                "evidence": {"error": f"Oracle missing or failed to compile at {oracle_path}"},
                "feedback": "Correctness evaluation failed: no test set or oracle available."
            }

        auto_dir = os.path.join(testset_dir, "auto_generated")
        os.makedirs(auto_dir, exist_ok=True)

        cached_in = sorted(glob.glob(os.path.join(auto_dir, "*.in")))
        if len(cached_in) < num_tests:
            oracle_code = ""
            if os.path.exists(oracle_path):
                with open(oracle_path, "r", encoding="utf-8", errors="ignore") as f:
                    oracle_code = f.read()

            generated_inputs = _synthesize_inputs_via_llm_and_seed(oracle_code, num_tests, seed)
            for idx, raw_input in enumerate(generated_inputs, start=1):
                in_filepath = os.path.join(auto_dir, f"test_{idx:02d}.in")
                out_filepath = os.path.join(auto_dir, f"test_{idx:02d}.out")

                res = run_safe([oracle_bin], stdin_data=raw_input + "\n", timeout_s=timeout_s, memory_mb=None)
                
                with open(in_filepath, "w") as fin:
                    fin.write(raw_input + "\n")
                with open(out_filepath, "w") as fout:
                    fout.write(res["stdout"].strip() + "\n")

        in_files = sorted(glob.glob(os.path.join(auto_dir, "*.in")))

    passed = 0
    failing = []

    for in_file in in_files:
        out_file = in_file[:-3] + ".out"
        with open(in_file, "r") as f:
            stdin_data = f.read()

        expected = ""
        if os.path.exists(out_file):
            with open(out_file, "r") as f:
                expected = f.read().strip()

        res = run_safe([binary_path], stdin_data=stdin_data, timeout_s=timeout_s, memory_mb=memory_mb)
        actual = res["stdout"].strip()

        if actual == expected and not res["timed_out"] and res["returncode"] == 0:
            passed += 1
        else:
            failing.append({
                "input": stdin_data.strip(),
                "expected": expected,
                "got": actual,
                "note": "Timeout" if res["timed_out"] else res.get("stderr", "").strip()
            })

    total = len(in_files)
    score = passed / total if total > 0 else 0.0

    feedback = f"{passed}/{total} tests pass."
    if failing:
        feedback += f" Failed on input: '{failing[0]['input']}'"

    return {
        "score": score,
        "evidence": {
            "tests_passed": passed,
            "tests_total": total,
            "test_source": "auto_generated",
            "failing": failing
        },
        "feedback": feedback
    }