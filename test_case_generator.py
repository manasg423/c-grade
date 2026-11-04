from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Optional


OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen3.5:2b"


def compile_oracle(oracle_path: str, output_bin: str) -> Optional[str]:
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


def query_ollama_chat(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.5,
    num_predict: int = 700,
    timeout: float = 90.0,
    seed: Optional[int] = None,
) -> Optional[dict]:
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
    system_prompt = (
        "You are an expert autonomous software testing agent for C programming assignments. "
        "Your task is to analyze a reference C implementation (oracle.c), an optional problem description, "
        "and sample I/O to synthesize a rigorous, production-grade test suite.\n\n"
        "Guidelines:\n"
        "1. I/O Contract Alignment: Analyze main() and all input-reading calls (scanf, fgets, getchar, cin) alongside the sample input. "
        "If the input expects a size N followed by an array of N elements, your generated strings MUST contain exactly N elements. "
        "Never generate an array length that contradicts its header.\n"
        "2. Problem Semantics: Infer preconditions, invariants, and constraints primarily from the oracle code. "
        "If a 'Problem Description' is provided in the prompt, strictly use it to guide the valid cases and intended behavior"
        "(e.g., ensuring majority elements exist if requested).\n"
        "3. Synthesize Two Categories:\n"
        "   - 'io_tests': Diverse, valid inputs testing the core algorithm. Radically vary the sizes (e.g., N=3, N=15, N=50) and value distributions.\n"
        "   - 'edge_cases': Adversarial inputs testing boundaries and C-specific limits. Force conditions like: minimum valid sizes (N=0 or N=1), "
        "all identical elements, negative/zero values, and extreme integer bounds (e.g., 2147483647 or -2147483648).\n"
        "4. Output Format: Return STRICTLY valid JSON matching this exact schema:\n"
        "{\n"
        '  "io_tests": ["raw_stdin_string_1", "raw_stdin_string_2"],\n'
        '  "edge_cases": ["raw_stdin_edge_1", "raw_stdin_edge_2"]\n'
        "}\n"
        "CRITICAL: You must properly escape all newlines inside the JSON strings as `\\n` (e.g., \"5\\n1 2 3 4 5\\n\"). Do not output raw line breaks."
    )

    sample_sec = f"\nTeacher's Sample Input:\n```\n{sample_input}\n```\n" if sample_input else ""
    output_sec = f"\nTeacher's Sample Output:\n```\n{sample_output}\n```\n" if sample_output else ""
    desc_sec = f"\nProblem Description:\n{problem_description}\n" if problem_description else ""

    io_results: list[str] = []
    edge_results: list[str] = []

    needed_io = max(1, num_io)
    needed_edge = max(1, num_edge)

    gen_seed = random.randint(1, 1000000)
    gen_temp = round(random.uniform(0.35, 0.75), 2)

    user_prompt = (
        f"Problem Identifier: {problem_id}\n"
        f"{desc_sec}{sample_sec}{output_sec}\n"
        f"Reference Implementation (oracle.c):\n```c\n{oracle_code}\n```\n\n"
        f"Task: Generate exactly {needed_io} diverse 'io_tests' and {needed_edge} 'edge_cases'.\n\n"
        "EXECUTION RULES:\n"
        "- Generate entirely NEW inputs. Do not simply reproduce the sample case.\n"
        "- Ensure every test case string perfectly matches the input format expected by the oracle's scanf/fgets calls.\n"
        "- Make the 'io_tests' highly variable in size and data variation.\n"
        "- Make the 'edge_cases' intentionally tricky to break fragile code (empty cases, max values, off-by-one setups).\n"
        "- EVERY string must end with a trailing escaped newline (`\\n`).\n"
        "- Output ONLY the raw JSON object. Do not include markdown formatting blocks (```json) or conversational filler."
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

    rng = random.Random(gen_seed)
    while len(io_results) < needed_io and io_results:
        base = io_results[len(io_results) % len(io_results)]
        lines = [l.strip() for l in base.strip().split("\n") if l.strip()]
        if len(lines) == 2:
            body_parts = lines[1].split()
            perm = list(body_parts)
            rng.shuffle(perm)
            var_inp = f"{lines[0]}\n" + " ".join(perm) + "\n"
            if var_inp not in io_results:
                io_results.append(var_inp)
            else:
                break
        elif len(lines) == 1:
            tokens = lines[0].split()
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

    normalized_io = [_ensure_multiline_format(inp, sample_input) for inp in io_results]
    normalized_edge = [_ensure_multiline_format(inp, sample_input) for inp in edge_results]

    return {
        "io_tests": normalized_io[:needed_io],
        "edge_cases": normalized_edge[:needed_edge]
    }


def _ensure_multiline_format(inp_str: str, sample_input: Optional[str]) -> str:
    if not inp_str:
        return "\n"

    lines = [l.strip() for l in inp_str.strip().split("\n") if l.strip()]
    if not lines:
        return "\n"

    if len(lines) == 1 and sample_input and "\n" in sample_input.strip():
        sample_lines = [l.strip() for l in sample_input.strip().split("\n") if l.strip()]
        k = len(sample_lines[0].split())
        tokens = lines[0].split()
        if len(tokens) > k:
            lines = [" ".join(tokens[:k]), " ".join(tokens[k:])]

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


def emergency_fallback_tests(sample_input: Optional[str], num_io: int, num_edge: int) -> dict:
    rng = random.Random(42)
    io_cases = []
    edge_cases = []

    if sample_input and sample_input.strip():
        lines = [l.strip() for l in sample_input.strip().split("\n") if l.strip()]
        io_cases.append(sample_input.strip() + "\n")
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
    if not tests_dir:
        tests_dir = os.path.join("problems", problem_id, "tests")
    if not oracle_path:
        oracle_path = os.path.join(tests_dir, "oracle.c")

    os.makedirs(tests_dir, exist_ok=True)
    auto_dir = os.path.join(tests_dir, "auto_generated")
    edge_dir = os.path.join(tests_dir, "edge_cases")
    os.makedirs(auto_dir, exist_ok=True)
    os.makedirs(edge_dir, exist_ok=True)

    oracle_code = ""
    if os.path.isfile(oracle_path):
        with open(oracle_path, "r", encoding="utf-8", errors="ignore") as f:
            oracle_code = f.read()

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
            model=DEFAULT_MODEL
        )

        method = f"ollama-{DEFAULT_MODEL}"

        if not test_data.get("io_tests"):
            test_data = emergency_fallback_tests(sample_input, num_io, num_edge)
            method = "sample-input-fuzzer"

        io_inputs = test_data.get("io_tests", [])[:num_io]
        edge_inputs = test_data.get("edge_cases", [])[:num_edge]

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

        for idx, inp in enumerate(io_inputs, 1):
            in_file = os.path.join(auto_dir, f"gen_{idx:02d}.in")
            out_file = os.path.join(auto_dir, f"gen_{idx:02d}.out")

            with open(in_file, "w", encoding="utf-8") as f_in:
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

        for idx, inp in enumerate(edge_inputs, 1):
            in_file = os.path.join(edge_dir, f"edge_{idx:02d}.in")
            out_file = os.path.join(edge_dir, f"edge_{idx:02d}.out")

            with open(in_file, "w", encoding="utf-8") as f_in:
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