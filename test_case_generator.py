"""
test_case_generator.py
-----------------------
Universal, completely LLM-generated blackbox test case generator for C-Grade Engine.

Design Principles:
  1. Completely LLM Generated:
     Uses local Ollama (/api/chat with think: False) to analyze ANY teacher reference C code (oracle.c),
     sample input, and problem description.
     The LLM identifies:
       - The exact standard input/output layout (lines, tokens, delimiters, types).
       - The core algorithm, invariants, preconditions, and return expectations.
     The LLM synthesizes:
       - 'io_tests': typical, diverse, non-trivial inputs satisfying problem requirements.
       - 'edge_cases': boundary, corner, adversarial inputs (minimal sizes, identical elements, negatives, limits).
     Zero problem-specific hardcoding. Completely blackbox and universal for any C problem in the world.

  2. Pure Ground-Truth Blackbox Execution:
     Compiles the teacher's reference implementation (oracle.c) to a native executable binary.
     Pipes each generated input to stdin and captures stdout to produce matching .out files.

Output Structure:
  tests/<problem>/auto_generated/gen_X.in  &  gen_X.out
  tests/<problem>/edge_cases/edge_XX.in    &  edge_XX.out

Public API:
  generate_all_test_cases(problem_id, tests_dir=None, oracle_path=None, sample_input=None, num_io=5, num_edge=5) -> dict
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen3.5:2b"


# ---------------------------------------------------------------------------
# Blackbox Oracle Compilation & Execution
# ---------------------------------------------------------------------------

def compile_oracle(oracle_path: str, output_bin: str) -> Optional[str]:
    """Compiles oracle.c into an executable binary using gcc or clang."""
    if not os.path.isfile(oracle_path):
        return None

    if os.name == "nt" and not output_bin.lower().endswith(".exe"):
        output_bin += ".exe"

    compilers = [shutil.which("gcc"), shutil.which("clang")]
    compilers = [c for c in compilers if c]
    if not compilers:
        compilers = ["gcc"]

    for compiler in compilers:
        try:
            cmd = [compiler, "-O2", oracle_path, "-o", output_bin, "-lm"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and os.path.isfile(output_bin):
                return output_bin
            if os.name == "nt" and os.path.isfile(output_bin + ".exe"):
                return output_bin + ".exe"
        except Exception:
            continue
    return None


def run_oracle(oracle_bin: str, stdin_text: str, timeout: float = 2.0) -> str:
    """Executes compiled oracle binary on stdin_text and returns stdout string."""
    try:
        proc = subprocess.run(
            [oracle_bin],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# LLM Chat Query Helper
# ---------------------------------------------------------------------------

def query_ollama_chat(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.5,
    num_predict: int = 700,
    timeout: float = 90.0,
    seed: Optional[int] = None,
) -> Optional[dict]:
    """
    Sends chat prompt to local Ollama server using /api/chat with think: False.
    Fast (20-30s), avoids context length exhaustion from hidden thinking tokens.
    """
    if seed is None:
        seed = random.randint(1, 1000000)

    payload = {
        "model": model,
        "messages": messages,
        "think": False,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": temperature,
            "seed": seed,
            "num_predict": num_predict,
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body.get("message", {}).get("content", "").strip()
            if not content:
                return None
            return json.loads(content)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Completely LLM-Generated Test Suite Synthesis
# ---------------------------------------------------------------------------

def generate_tests_with_llm(
    problem_id: str,
    oracle_code: str,
    sample_input: Optional[str] = None,
    problem_description: Optional[str] = None,
    num_io: int = 5,
    num_edge: int = 5,
    model: str = DEFAULT_MODEL
) -> dict:
    """
    Autonomous LLM test case generator.
    Completely blackbox and problem-independent.
    """
    system_prompt = (
        "You are an expert autonomous test case generator for C programming assignments. "
        "Your task is to analyze ANY reference C implementation (oracle.c) and sample input, "
        "and synthesize a rigorous, meaningful, and comprehensive test suite.\n\n"
        "Guidelines:\n"
        "1. Analyze main() and all input-reading calls (scanf, fgets, getchar, cin) alongside the sample input "
        "to deduce the EXACT standard input format (line breaks, number of tokens per line, types).\n"
        "2. Understand the problem logic, preconditions, and invariants from the code and comments "
        "(e.g. if the code assumes a majority element exists in an array of size n, every standard test case must have an element appearing > n/2 times; "
        "if it finds a two-sum target, target must be achievable for positive cases; if it sorts an array, provide varied sequences).\n"
        "3. Synthesize:\n"
        "   - 'io_tests': typical, diverse, non-trivial test inputs representing valid problem instances across different sizes.\n"
        "   - 'edge_cases': boundary, corner, and adversarial inputs (empty length inputs,single length inputs, all identical elements, negative numbers, extreme values, or invalid inputs handled by code).\n"
        "4. Return STRICTLY valid JSON with the schema:\n"
        "{\n"
        '  "io_tests": ["raw_stdin_string_1", "raw_stdin_string_2", ...],\n'
        '  "edge_cases": ["raw_stdin_edge_1", "raw_stdin_edge_2", ...]\n'
        "}"
    )

    sample_sec = f"\nTeacher's Sample Input:\n```\n{sample_input}\n```\n" if sample_input else ""
    desc_sec = f"\nProblem Description:\n{problem_description}\n" if problem_description else ""

    io_results: list[str] = []
    edge_results: list[str] = []

    needed_io = max(1, num_io)
    needed_edge = max(1, num_edge)

    # Use randomized temperature and seed to ensure every regeneration produces fresh, distinct test cases
    gen_seed = random.randint(1, 1000000)
    gen_temp = round(random.uniform(0.35, 0.75), 2)

    user_prompt = (
        f"Problem Identifier: {problem_id}\n"
        f"{desc_sec}{sample_sec}\n"
        f"Reference Implementation (oracle.c):\n```c\n{oracle_code}\n```\n\n"
        f"Generate {needed_io} diverse 'io_tests' and {needed_edge} 'edge_cases'.\n"
        "CRITICAL RULES:\n"
        "- Each string MUST be the exact raw text to pipe to stdin, ending with newline '\\n'.\n"
        "- Follow the exact input format demonstrated in the sample input and oracle.c.\n"
        "- Ensure every io_test is valid, meaningful, and conforms to problem assumptions.\n"
        "- Produce fresh, distinct inputs with varied sizes and varied values."
    )

    res = query_ollama_chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        model=model,
        temperature=gen_temp,
        seed=gen_seed,
        num_predict=800,
        timeout=100.0
    )

    if isinstance(res, dict):
        for s in res.get("io_tests", []):
            st_clean = str(s).strip()
            if st_clean and st_clean + "\n" not in io_results:
                io_results.append(st_clean + "\n")
        for s in res.get("edge_cases", []):
            st_clean = str(s).strip()
            if st_clean and st_clean + "\n" not in edge_results:
                edge_results.append(st_clean + "\n")

    # If the LLM returned fewer than requested, synthesize variations of the LLM outputs
    rng = random.Random(gen_seed)
    while len(io_results) < needed_io and io_results:
        base = io_results[len(io_results) % len(io_results)]
        lines = [l.strip() for l in base.strip().split("\n") if l.strip()]
        if len(lines) == 2:
            body_parts = lines[1].split()
            # Permute body tokens preserving counts/majority
            perm = list(body_parts)
            rng.shuffle(perm)
            var_inp = f"{lines[0]}\n" + " ".join(perm) + "\n"
            if var_inp not in io_results:
                io_results.append(var_inp)
            else:
                break
        elif len(lines) == 1:
            tokens = lines[0].split()
            # Mutate with offset
            offset = rng.randint(2, 10)
            mut_tokens = []
            for t in tokens:
                try:
                    mut_tokens.append(str(int(t) + offset))
                except ValueError:
                    mut_tokens.append(t)
            var_inp = " ".join(mut_tokens) + "\n"
            if var_inp not in io_results:
                io_results.append(var_inp)
            else:
                break
        else:
            break

    # Normalize inputs formatting to match multi-line sample if single line returned
    normalized_io = [_ensure_multiline_format(inp, sample_input) for inp in io_results]
    normalized_edge = [_ensure_multiline_format(inp, sample_input) for inp in edge_results]

    return {
        "io_tests": normalized_io[:needed_io],
        "edge_cases": normalized_edge[:needed_edge]
    }


def _ensure_multiline_format(inp_str: str, sample_input: Optional[str]) -> str:
    """
    Validates and normalizes LLM-generated input text.
    If Line 1 contains a size N and Line 2 contains an array, guarantees
    that N matches the actual number of elements supplied on Line 2, preventing
    scanf from reading uninitialized memory.
    """
    if not inp_str:
        return "\n"

    lines = [l.strip() for l in inp_str.strip().split("\n") if l.strip()]
    if not lines:
        return "\n"

    # If single-line but sample_input has multiple lines
    if len(lines) == 1 and sample_input and "\n" in sample_input.strip():
        sample_lines = [l.strip() for l in sample_input.strip().split("\n") if l.strip()]
        k = len(sample_lines[0].split())
        tokens = lines[0].split()
        if len(tokens) > k:
            lines = [" ".join(tokens[:k]), " ".join(tokens[k:])]

    # Reconcile size header with array length if 2 lines
    if len(lines) == 2:
        header_tokens = lines[0].split()
        body_tokens = lines[1].split()
        if header_tokens and body_tokens:
            try:
                size_val = int(header_tokens[0])
                actual_len = len(body_tokens)
                if size_val > 0 and size_val != actual_len:
                    header_tokens[0] = str(actual_len)
                    lines[0] = " ".join(header_tokens)
            except ValueError:
                pass

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Emergency Structural Fuzzer (Only if LLM server is unreachable)
# ---------------------------------------------------------------------------

def emergency_fallback_tests(sample_input: Optional[str], num_io: int, num_edge: int) -> dict:
    """
    Universal structural mutation of the teacher's sample input
    used strictly if Ollama is unreachable.
    """
    rng = random.Random(42)
    io_cases = []
    edge_cases = []

    if sample_input and sample_input.strip():
        lines = [l.strip() for l in sample_input.strip().split("\n") if l.strip()]
        io_cases.append(sample_input.strip() + "\n")
        # Mutate numbers
        for _ in range(num_io - 1):
            mutated_lines = []
            for l in lines:
                parts = l.split()
                mut = []
                for p in parts:
                    try:
                        val = int(p)
                        mut.append(str(val + rng.randint(-3, 5)))
                    except ValueError:
                        mut.append(p)
                mutated_lines.append(" ".join(mut))
            io_cases.append("\n".join(mutated_lines) + "\n")
    else:
        for i in range(num_io):
            io_cases.append(f"{i + 1}\n")

    edge_cases = ["0\n", "-1\n", "1\n1\n", "2147483647\n", "-2147483648\n"]

    return {
        "io_tests": io_cases[:num_io],
        "edge_cases": edge_cases[:num_edge]
    }


# ---------------------------------------------------------------------------
# Main Orchestration: File Writing & Blackbox Execution
# ---------------------------------------------------------------------------

def generate_all_test_cases(
    problem_id: str,
    tests_dir: Optional[str] = None,
    oracle_path: Optional[str] = None,
    sample_input: Optional[str] = None,
    problem_description: Optional[str] = None,
    num_io: int = 5,
    num_edge: int = 5,
    force_regenerate: bool = True
) -> dict:
    """
    Universal test case generator.
    Writes:
      tests/<problem>/auto_generated/gen_X.in  &  gen_X.out
      tests/<problem>/edge_cases/edge_XX.in    &  edge_XX.out
    """
    if not tests_dir:
        tests_dir = os.path.join("tests", problem_id)
    if not oracle_path:
        oracle_path = os.path.join(tests_dir, "oracle.c")

    os.makedirs(tests_dir, exist_ok=True)
    auto_dir = os.path.join(tests_dir, "auto_generated")
    edge_dir = os.path.join(tests_dir, "edge_cases")
    os.makedirs(auto_dir, exist_ok=True)
    os.makedirs(edge_dir, exist_ok=True)

    # 1. Read Oracle Code
    oracle_code = ""
    if os.path.isfile(oracle_path):
        with open(oracle_path, "r", encoding="utf-8", errors="ignore") as f:
            oracle_code = f.read()

    # If sample_input not passed, attempt reading from rules JSON
    if not sample_input:
        rules_file = os.path.join("rules", f"{problem_id}_rules.json")
        if os.path.isfile(rules_file):
            try:
                with open(rules_file, "r", encoding="utf-8") as rf:
                    r_data = json.load(rf)
                    sample_input = r_data.get("smoke_input", "")
            except Exception:
                pass

    # 2. Compile Reference Oracle Binary
    oracle_bin = None
    temp_dir = tempfile.mkdtemp(prefix="cg_oracle_")
    try:
        bin_target = os.path.join(temp_dir, "oracle_exec.exe" if os.name == "nt" else "oracle_exec")
        compiled_path = compile_oracle(oracle_path, bin_target)
        if compiled_path and os.path.isfile(compiled_path):
            oracle_bin = compiled_path

        # 3. Generate Inputs with LLM
        test_data = generate_tests_with_llm(
            problem_id=problem_id,
            oracle_code=oracle_code,
            sample_input=sample_input,
            problem_description=problem_description,
            num_io=num_io,
            num_edge=num_edge,
            model=DEFAULT_MODEL
        )

        method = f"ollama-{DEFAULT_MODEL}"

        # If LLM returned empty, use emergency structural mutation
        if not test_data.get("io_tests"):
            test_data = emergency_fallback_tests(sample_input, num_io, num_edge)
            method = "sample-input-fuzzer"

        io_inputs = test_data.get("io_tests", [])[:num_io]
        edge_inputs = test_data.get("edge_cases", [])[:num_edge]

        # 4. Clean existing generated files if force_regenerate
        if force_regenerate:
            for d in [auto_dir, edge_dir]:
                for f in Path(d).glob("*.in"):
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
                for f in Path(d).glob("*.out"):
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass

        generated_io_files = []
        generated_edge_files = []

        # 5. Write I/O Test Cases (.in and matching .out)
        for idx, inp in enumerate(io_inputs, 1):
            in_file = os.path.join(auto_dir, f"gen_{idx:02d}.in")
            out_file = os.path.join(auto_dir, f"gen_{idx:02d}.out")

            with open(in_file, "w", encoding="utf-8") as f_in:
                f_in.write(inp)

            # Ground-truth .out generated purely via blackbox oracle execution
            expected_out = ""
            if oracle_bin:
                expected_out = run_oracle(oracle_bin, inp)

            with open(out_file, "w", encoding="utf-8") as f_out:
                f_out.write(expected_out)

            generated_io_files.append((f"gen_{idx:02d}.in", f"gen_{idx:02d}.out"))

        # Write auto_generated manifest
        with open(os.path.join(auto_dir, ".ollama_manifest.json"), "w", encoding="utf-8") as mf:
            json.dump({
                "generator": method,
                "problem": problem_id,
                "oracle_compiled": bool(oracle_bin),
                "count": len(generated_io_files),
                "files": [f[0] for f in generated_io_files]
            }, mf, indent=2)

        # 6. Write Edge Cases (.in and matching .out)
        for idx, inp in enumerate(edge_inputs, 1):
            in_file = os.path.join(edge_dir, f"edge_{idx:02d}.in")
            out_file = os.path.join(edge_dir, f"edge_{idx:02d}.out")

            with open(in_file, "w", encoding="utf-8") as f_in:
                f_in.write(inp)

            # Ground-truth .out generated purely via blackbox oracle execution
            expected_out = ""
            if oracle_bin:
                expected_out = run_oracle(oracle_bin, inp)

            with open(out_file, "w", encoding="utf-8") as f_out:
                f_out.write(expected_out)

            generated_edge_files.append((f"edge_{idx:02d}.in", f"edge_{idx:02d}.out"))

        # Write edge_cases manifest
        with open(os.path.join(edge_dir, ".manifest.json"), "w", encoding="utf-8") as mf:
            json.dump({
                "generator": method,
                "problem": problem_id,
                "oracle_compiled": bool(oracle_bin),
                "count": len(generated_edge_files),
                "files": [f[0] for f in generated_edge_files]
            }, mf, indent=2)

        return {
            "success": True,
            "problem": problem_id,
            "method": method,
            "oracle_compiled": bool(oracle_bin),
            "io_count": len(generated_io_files),
            "edge_count": len(generated_edge_files),
            "io_cases_count": len(generated_io_files),
            "edge_cases_count": len(generated_edge_files),
            "auto_dir": auto_dir,
            "edge_dir": edge_dir,
            "io_files": generated_io_files,
            "edge_files": generated_edge_files,
        }

    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI Command Line Interface
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal Blackbox C-Grade Test Case Generator")
    parser.add_argument("--problem", "-p", required=True, help="Problem identifier slug")
    parser.add_argument("--oracle", "-o", default=None, help="Path to reference oracle.c")
    parser.add_argument("--tests-dir", "-t", default=None, help="Destination tests directory")
    parser.add_argument("--sample", "-s", default=None, help="Sample smoke input string")
    parser.add_argument("--desc", "-d", default=None, help="Optional problem description / requirements")
    parser.add_argument("--num-io", type=int, default=5, help="Number of standard I/O test cases to generate")
    parser.add_argument("--num-edge", type=int, default=5, help="Number of adversarial edge cases to generate")

    args = parser.parse_args()

    print(f"Generating LLM test cases for problem '{args.problem}'...")
    res = generate_all_test_cases(
        problem_id=args.problem,
        tests_dir=args.tests_dir,
        oracle_path=args.oracle,
        sample_input=args.sample,
        problem_description=args.desc,
        num_io=args.num_io,
        num_edge=args.num_edge,
    )
    print(f"Done! Method: {res['method']} (Oracle compiled: {res['oracle_compiled']})")
    print(f"  Generated {res['io_count']} I/O tests in {res['auto_dir']}")
    print(f"  Generated {res['edge_count']} Edge cases in {res['edge_dir']}")
