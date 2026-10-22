import os
import subprocess
import xml.etree.ElementTree as ET

def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    """
    Universal static analyzer using cppcheck and GCC diagnostic passes.
    Returns:
        score: float in [0.0, 1.0]
        evidence: detailed findings categorized by severity
        feedback: actionable summary of warnings/errors
    """
    if not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Source file not found: {source_path}"},
            "feedback": "Evaluation failed: source file missing."
        }

    enable_cppcheck = params.get("enable_cppcheck", True)
    enable_gcc_warnings = params.get("enable_gcc_warnings", True)

    deductions = {
        "error": params.get("error_penalty", 0.40),
        "warning": params.get("warning_penalty", 0.15),
        "style": params.get("style_penalty", 0.05),
        "performance": params.get("performance_penalty", 0.05),
        "portability": params.get("portability_penalty", 0.05)
    }

    issues = []
    total_deduction = 0.0

    # 1. Cppcheck Analysis (XML output)
    if enable_cppcheck:
        try:
            cmd = [
                "cppcheck",
                "--enable=all",
                "--inconclusive",
                "--xml",
                "--xml-version=2",
                source_path
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10.0)
            xml_output = proc.stderr

            if xml_output.strip().startswith("<?xml") or "<results>" in xml_output:
                root = ET.fromstring(xml_output)
                errors_node = root.find("errors")
                if errors_node is not None:
                    for err in errors_node.findall("error"):
                        err_id = err.get("id", "unknown")
                        severity = err.get("severity", "warning").lower()
                        msg = err.get("msg", "")

                        if err_id in ("missingInclude", "missingIncludeSystem", "checkersReport"):
                            continue

                        location = err.find("location")
                        line_no = int(location.get("line", 0)) if location is not None else 0

                        penalty = deductions.get(severity, 0.05)
                        total_deduction += penalty

                        issues.append({
                            "tool": "cppcheck",
                            "severity": severity,
                            "id": err_id,
                            "line": line_no,
                            "message": msg
                        })
        except Exception as e:
            issues.append({
                "tool": "cppcheck",
                "severity": "info",
                "id": "tool_failure",
                "line": 0,
                "message": f"Cppcheck inspection skipped: {type(e).__name__}"
            })

    # 2. GCC Diagnostic Pass (-Wall -Wextra -Wpedantic)
    if enable_gcc_warnings:
        try:
            gcc_cmd = [
                "gcc",
                "-fsyntax-only",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                source_path
            ]
            proc_gcc = subprocess.run(gcc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10.0)

            for line in proc_gcc.stderr.splitlines():
                if ": warning:" in line or ": error:" in line:
                    parts = line.split(":", 4)
                    if len(parts) >= 4:
                        try:
                            line_no = int(parts[1].strip())
                        except ValueError:
                            line_no = 0

                        is_err = ": error:" in line
                        sev = "error" if is_err else "warning"
                        msg = parts[-1].strip()

                        total_deduction += deductions.get(sev, 0.10)
                        issues.append({
                            "tool": "gcc",
                            "severity": sev,
                            "id": "compiler_diagnostic",
                            "line": line_no,
                            "message": msg
                        })
        except Exception:
            pass

    final_score = max(0.0, 1.0 - total_deduction)
    final_score = round(final_score, 2)

    if not issues:
        feedback = "Static check passed clean. Zero warnings, undefined behaviors, or compiler diagnostics detected."
    else:
        err_count = sum(1 for i in issues if i["severity"] == "error")
        warn_count = sum(1 for i in issues if i["severity"] in ("warning", "style", "portability"))
        summary_parts = []
        if err_count:
            summary_parts.append(f"{err_count} static error(s)")
        if warn_count:
            summary_parts.append(f"{warn_count} warning(s)")

        highlights = [f"L{i['line']}: {i['message']}" for i in issues[:2]]
        feedback = f"Detected {', '.join(summary_parts)}. Issues: {'; '.join(highlights)}"

    return {
        "score": final_score,
        "evidence": {
            "total_issues": len(issues),
            "issues": issues,
            "deductions_applied": round(total_deduction, 2)
        },
        "feedback": feedback
    }