import os
import subprocess


def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    """
    Check C source code for compiler warnings and errors using GCC.

    Returns:
        score: float between 0.0 and 1.0
        evidence: compiler diagnostics and deductions
        feedback: summary of findings
    """

    if not os.path.isfile(source_path):
        return {
            "score": 0.0,
            "evidence": {
                "error": f"Source file not found: {source_path}"
            },
            "feedback": "Evaluation failed: source file missing."
        }

    warning_penalty = params.get("warning_penalty", 0.10)
    error_penalty = params.get("error_penalty", 0.40)

    flags = params.get(
        "flags",
        ["-Wall", "-Wextra", "-Wpedantic"]
    )

    timeout = limits.get("timeout", 10)

    command = [
        "gcc",
        "-fsyntax-only",
        *flags,
        source_path
    ]

    issues = []
    total_deduction = 0.0

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        for line in result.stderr.splitlines():
            if ": warning:" not in line and ": error:" not in line:
                continue

            parts = line.split(":", 4)

            filename = parts[0] if len(parts) > 0 else source_path

            try:
                line_number = int(parts[1])
            except (ValueError, IndexError):
                line_number = 0

            try:
                column_number = int(parts[2])
            except (ValueError, IndexError):
                column_number = 0

            severity = "error" if ": error:" in line else "warning"
            message = parts[-1].strip()

            penalty = error_penalty if severity == "error" else warning_penalty
            total_deduction += penalty

            issues.append({
                "tool": "gcc",
                "file": filename,
                "line": line_number,
                "column": column_number,
                "severity": severity,
                "id": "compiler_diagnostic",
                "message": message,
                "penalty": penalty
            })

        score = round(max(0.0, 1.0 - total_deduction), 2)

        if issues:
            error_count = sum(
                issue["severity"] == "error" for issue in issues
            )
            warning_count = sum(
                issue["severity"] == "warning" for issue in issues
            )

            summary = []
            if error_count:
                summary.append(f"{error_count} error(s)")
            if warning_count:
                summary.append(f"{warning_count} warning(s)")

            highlights = [
                f"L{issue['line']}: {issue['message']}"
                for issue in issues[:2]
            ]

            feedback = (
                f"Detected {', '.join(summary)}. Issues: "
                + "; ".join(highlights)
            )
        else:
            feedback = "No compiler warnings or errors detected."

        return {
            "score": score,
            "evidence": {
                "total_issues": len(issues),
                "issues": issues,
                "deductions_applied": round(total_deduction, 2),
                "return_code": result.returncode
            },
            "feedback": feedback
        }

    except subprocess.TimeoutExpired:
        return {
            "score": 0.0,
            "evidence": {
                "error": "GCC timed out.",
                "issues": [],
                "deductions_applied": 0.0
            },
            "feedback": "Evaluation failed: GCC timed out."
        }

    except FileNotFoundError:
        return {
            "score": 0.0,
            "evidence": {
                "error": "GCC is not installed or not available in PATH.",
                "issues": [],
                "deductions_applied": 0.0
            },
            "feedback": "Evaluation failed: GCC not found."
        }

    except Exception as e:
        return {
            "score": 0.0,
            "evidence": {
                "error": f"{type(e).__name__}: {e}",
                "issues": [],
                "deductions_applied": 0.0
            },
            "feedback": "Evaluation failed: unexpected compiler error."
        }