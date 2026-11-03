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
  problems/<problem>/tests/auto_generated/gen_X.in  &  gen_X.out
  problems/<problem>/tests/edge_cases/edge_XX.in    &  edge_XX.out

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
_IO_FORMAT_CACHE: dict[tuple[str, str], str] = {}


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

def analyze_io_format_with_llm(oracle_code: str, model: str = DEFAULT_MODEL) -> str:
    """Infer the standard input/output contract using only the oracle source."""
    if not oracle_code.strip():
        return ""
    cache_key = (model, oracle_code)
    if cache_key in _IO_FORMAT_CACHE:
        return _IO_FORMAT_CACHE[cache_key]

    response = query_ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "Analyze the supplied C source alone and infer the complete stdin grammar. Trace every "
                    "input read in execution order, including scanf format strings, fgets/getline, getchar, "
                    "character and string reads, loops, branches, repeated records, test-case counts, EOF, "
                    "and sentinels. Distinguish token-based whitespace from significant line boundaries, "
                    "and state exact field types, counts, delimiters, conditional sections, and constraints "
                    "enforced by the code. Describe any ambiguities instead of guessing. Return JSON with "
                    "exactly one key, io_format, whose value is a concise but detailed string."
                ),
            },
            {
                "role": "user",
                "content": f"Analyze only this oracle.c source:\n```c\n{oracle_code}\n```",
            },
        ],
        model=model,
        temperature=0.1,
        num_predict=250,
        timeout=100.0,
    )
    if not isinstance(response, dict):
        return ""
    io_format = response.get("io_format")
    if not isinstance(io_format, str) or not io_format.strip():
        return ""
    if len(_IO_FORMAT_CACHE) >= 16:
        _IO_FORMAT_CACHE.pop(next(iter(_IO_FORMAT_CACHE)))
    _IO_FORMAT_CACHE[cache_key] = io_format.strip()
    return _IO_FORMAT_CACHE[cache_key]


def generate_tests_with_llm(
    problem_id: str,
    oracle_code: str,
    sample_input: Optional[str] = None,
    problem_description: Optional[str] = None,
    num_io: int = 5,
    num_edge: int = 5,
    model: str = DEFAULT_MODEL,
    sample_output: Optional[str] = None,
) -> dict:
    """Generate C test inputs using oracle-derived format analysis."""
    io_format = analyze_io_format_with_llm(oracle_code, model=model)

    system_prompt = (
        "You are an expert C-program test generator. Use the supplied I/O-format analysis, complete oracle.c, "
        "optional problem description, and teacher sample I/O. Cross-check the analysis against every input read. "
        "scanf whitespace may be interchangeable; fgets/getchar or explicit parsing can make line breaks, spaces, "
        "or characters significant. Never impose a layout the code does not require.\n\n"
        "Every returned test must be one complete, semantically valid stdin input accepted by the program from "
        "the first byte through EOF. Include no labels, comments, explanations, missing fields, or extra tokens.\n\n"
        "Trace reads, branches, loops, and termination rules. Identify fixed-size, count-prefixed, repeated, "
        "sentinel-terminated, EOF-terminated, conditional, line-based, and token-based input as applicable. "
        "Respect field types, counts, delimiters, order, exact characters, and relationships. Keep edge cases "
        "within the legal constraints; do not assume empty input, negatives, or extreme values are valid.\n\n"
        "Vary sizes only when the grammar permits it; for fixed-size formats, vary values and relationships. "
        "Return fewer cases rather than malformed or uncertain cases. Before responding, internally trace every "
        "candidate through all reads to EOF and discard any candidate with missing, extra, malformed, wrongly "
        "counted, or uncertain fields. Do not repair candidates.\n\n"
        "Return only valid JSON with exactly these keys: io_tests and edge_cases. Each value is an array of "
        "strings; every string must contain only the exact stdin for one run. Preserve required separators and "
        "line breaks, and do not add a trailing newline unless the format allows or requires it."
    )

    sample_sec = f"\nTeacher's Sample Input:\n```\n{sample_input}\n```\n" if sample_input is not None else ""
    output_sec = f"\nTeacher's Sample Output:\n```\n{sample_output}\n```\n" if sample_output is not None else ""
    desc_sec = f"\nProblem Description:\n{problem_description}\n" if problem_description else ""
    format_sec = (
        f"\nOracle I/O Format Analysis:\n{io_format}\n"
        if io_format
        else "\nOracle I/O Format Analysis:\nUnavailable; infer the input grammar from the oracle and teacher context.\n"
    )

    needed_io = max(1, num_io)
    needed_edge = max(1, num_edge)

    gen_seed = random.randint(1, 1000000)
    gen_temp = round(random.uniform(0.35, 0.75), 2)

    user_prompt = (
        f"Problem Identifier: {problem_id}\n"
        f"{format_sec}{desc_sec}{sample_sec}{output_sec}\n"
        f"Reference Implementation (oracle.c):\n```c\n{oracle_code}\n```\n\n"
        f"Task: Generate and self-audit up to {needed_io} 'io_tests' and {needed_edge} 'edge_cases'. Return fewer "
        "rather than include any input that does not strictly conform.\n\n"
        "EXECUTION RULES:\n"
        "- Follow the Oracle I/O Format Analysis and trace the C source; do not assume the sample's line breaks "
        "are significant unless the reader makes them significant.\n"
        "- Check every count, branch-dependent field, record terminator, and sentinel/EOF condition. Include no "
        "missing or extra input.\n"
        "- Respect all input constraints and required data properties described by the code, sample I/O, and problem.\n"
        "- Generate inputs distinct from the sample when a valid alternative is clear.\n"
        "- Vary sizes only for variable-size grammars; vary values and relationships for fixed-size grammars.\n"
        "- 'edge_cases': legal boundary cases only; never deliberately malformed input.\n"
        "- Preserve each input string exactly as the complete stdin for one execution; do not require or add a "
        "trailing newline unless the input contract calls for it.\n"
        "- Before answering, internally trace every input read for every candidate from start to EOF. Discard any "
        "candidate with a missing, extra, malformed, wrongly counted, or uncertain field. Do not repair candidates.\n"
        "- Output ONLY the raw JSON object. Do not include markdown formatting blocks (```json) or conversational filler."
    )

    res = query_ollama_chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        model=model,
        temperature=gen_temp,
        seed=gen_seed,
        num_predict=450,
        timeout=100.0
    )

    raw_io = res.get("io_tests", []) if isinstance(res, dict) else []
    raw_edge = res.get("edge_cases", []) if isinstance(res, dict) else []
    if not isinstance(raw_io, list):
        raw_io = []
    if not isinstance(raw_edge, list):
        raw_edge = []
    candidate_io = list(dict.fromkeys(
        value for value in raw_io[:needed_io] if isinstance(value, str)
    ))
    candidate_edge = list(dict.fromkeys(
        value for value in raw_edge[:needed_edge] if isinstance(value, str)
    ))

    return {"io_tests": candidate_io, "edge_cases": candidate_edge}


# ---------------------------------------------------------------------------
# Emergency Structural Fuzzer (Only if Ollama is unreachable)
# ---------------------------------------------------------------------------

def emergency_fallback_tests(sample_input: Optional[str], num_io: int, num_edge: int) -> dict:
    """Use only the teacher-provided sample when no model can validate new inputs."""
    sample_cases = [sample_input] if sample_input and sample_input.strip() else []
    return {"io_tests": sample_cases[:num_io], "edge_cases": []}


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
    force_regenerate: bool = True,
    sample_output: Optional[str] = None,
) -> dict:
    """
        Universal test case generator.
        Writes:
            problems/<problem>/tests/auto_generated/gen_X.in  &  gen_X.out
            problems/<problem>/tests/edge_cases/edge_XX.in    &  edge_XX.out
        """
    if not tests_dir:
        tests_dir = os.path.join("problems", problem_id, "tests")
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

    # Fill missing sample values from the assignment rules.
    if not sample_input or sample_output is None:
        rules_file = os.path.join("problems", problem_id, f"{problem_id}_rules.json")
        if os.path.isfile(rules_file):
            try:
                with open(rules_file, "r", encoding="utf-8") as rf:
                    r_data = json.load(rf)
                    if not sample_input:
                        sample_input = r_data.get("smoke_input", "")
                    if sample_output is None:
                        sample_output = r_data.get("sample_output", "")
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

        test_data = generate_tests_with_llm(
            problem_id=problem_id,
            oracle_code=oracle_code,
            sample_input=sample_input,
            sample_output=sample_output,
            problem_description=problem_description,
            num_io=num_io,
            num_edge=num_edge,
            model=DEFAULT_MODEL,
        )

        method = f"ollama-{DEFAULT_MODEL}"

        # If no I/O case passed validation, retain only the known-valid teacher sample.
        if not test_data.get("io_tests"):
            fallback = emergency_fallback_tests(sample_input, num_io, num_edge)
            test_data["io_tests"] = fallback["io_tests"]
            method = "teacher-sample-fallback"

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

            with open(in_file, "w", encoding="utf-8", newline="") as f_in:
                f_in.write(inp)

            expected_out = ""
            if oracle_bin:
                expected_out = run_oracle(oracle_bin, inp)

            with open(out_file, "w", encoding="utf-8") as f_out:
                f_out.write(expected_out)

            generated_io_files.append((f"gen_{idx:02d}.in", f"gen_{idx:02d}.out"))

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

            with open(in_file, "w", encoding="utf-8", newline="") as f_in:
                f_in.write(inp)

            expected_out = ""
            if oracle_bin:
                expected_out = run_oracle(oracle_bin, inp)

            with open(out_file, "w", encoding="utf-8") as f_out:
                f_out.write(expected_out)

            generated_edge_files.append((f"edge_{idx:02d}.in", f"edge_{idx:02d}.out"))

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
    parser.add_argument("--sample-output", default=None, help="Teacher sample output string")
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
        sample_output=args.sample_output,
        problem_description=args.desc,
        num_io=args.num_io,
        num_edge=args.num_edge,
    )
    print(f"Done! Method: {res['method']} (Oracle compiled: {res['oracle_compiled']})")
    print(f"  Generated {res['io_count']} I/O tests in {res['auto_dir']}")
    print(f"  Generated {res['edge_count']} Edge cases in {res['edge_dir']}")