import os
import json
import hashlib
import subprocess
from core.sandbox import run_safe

def run_first_pass(source_path: str, rules_path: str) -> dict:
    result = {
        "rules_hash": "",
        "rules_valid": False,
        "static_sanity": False,
        "compiled": False,
        "binary_path": "",
        "smoke_ok": False,
        "suspected_infinite_loop": False,
        "error": None
    }

    # 1. Validate rules.json and compute SHA-256 hash
    if not os.path.exists(rules_path):
        result["error"] = f"Rules file not found: {rules_path}"
        return result

    with open(rules_path, "rb") as f:
        content = f.read()
        result["rules_hash"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
        rules = json.loads(content.decode("utf-8"))

    total_weight = sum(m.get("weight", 0.0) for m in rules.get("metrics", []))
    if abs(total_weight - 1.0) > 1e-4:
        result["error"] = f"Rules weights sum to {total_weight}, expected 1.00"
        return result
    result["rules_valid"] = True

    # 2. Check Static Sanity (main, I/O, return)
    if not os.path.exists(source_path):
        result["error"] = f"Source file not found: {source_path}"
        return result

    with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
        code = f.read()

    has_main = "main" in code
    has_io = any(fn in code for fn in ["scanf", "printf", "puts", "getchar", "fgets"])
    has_return = "return" in code

    if not (has_main and has_io and has_return):
        result["error"] = "Static sanity failed: missing main(), I/O, or return."
        return result
    result["static_sanity"] = True

    # 3. Sandbox Compilation Check
    binary_dir = "/tmp/c_grade_bin"
    os.makedirs(binary_dir, exist_ok=True)
    binary_name = os.path.splitext(os.path.basename(source_path))[0] + "_bin"
    binary_path = os.path.join(binary_dir, binary_name)

    compile_cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-std=c11", source_path, "-o", binary_path]
    compile_res = subprocess.run(compile_cmd, capture_output=True, text=True)

    if compile_res.returncode != 0:
        result["error"] = f"Compilation failed: {compile_res.stderr}"
        return result

    result["compiled"] = True
    result["binary_path"] = binary_path

    # 4. Smoke Run (Test with 1 sample input under 1s / 64MB)
    time_limit = rules.get("time_limit_s", 1.0)
    mem_limit = rules.get("memory_limit_mb", 64)

    smoke_run = run_safe([binary_path], stdin_data="7 5\n", timeout_s=time_limit, memory_mb=mem_limit)

    if smoke_run["timed_out"]:
        result["suspected_infinite_loop"] = True
        result["error"] = "Smoke test timed out. Suspected infinite loop."
        return result

    if smoke_run["returncode"] != 0:
        result["error"] = f"Smoke test crashed with returncode {smoke_run['returncode']}"
        return result

    result["smoke_ok"] = True
    return result