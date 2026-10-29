import os
import subprocess
import xml.etree.ElementTree as ET
import lizard


def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    if not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Source file not found: {source_path}"},
            "feedback": "Evaluation failed: source file missing."
        }

    deductions = {
        "error": params.get("error_penalty", 0.40),
        "warning": params.get("warning_penalty", 0.15),
        "style": params.get("style_penalty", 0.05)
    }

    max_complexity = params.get("max_cyclomatic", 8)
    issues = []
    total_deduction = 0.0
    analyzer_failures = []

    def add_issue(tool, severity, issue_id, line, message):
        nonlocal total_deduction

        penalty = deductions.get(severity, 0.0)
        total_deduction += penalty

        issues.append({
            "tool": tool,
            "severity": severity,
            "id": issue_id,
            "line": line,
            "message": message,
            "penalty": penalty
        })

    # 1. Cppcheck
    if params.get("enable_cppcheck", True):
        try:
            result = subprocess.run(
                [
                    "cppcheck", "--enable=warning,style,performance,portability",
                    "--xml", "--xml-version=2", source_path
                ],
                capture_output=True, text=True, timeout=10
            )

            if result.stderr.strip():
                root = ET.fromstring(result.stderr)

                for error in root.findall(".//error"):
                    if error.get("id") in (
                        "missingInclude", "missingIncludeSystem", "checkersReport"
                    ):
                        continue

                    location = error.find("location")
                    line = int(location.get("line", 0)) if location is not None else 0
                    severity = error.get("severity", "warning").lower()

                    if severity not in deductions:
                        severity = "warning"

                    add_issue(
                        "cppcheck", severity, error.get("id", "unknown"),
                        line, error.get("msg", "")
                    )

        except Exception as e:
            analyzer_failures.append(f"Cppcheck: {type(e).__name__}")

    # 2. GCC warnings and errors
    if params.get("enable_gcc_warnings", True):
        try:
            result = subprocess.run(
                ["gcc", "-fsyntax-only", "-Wall", "-Wextra",
                 "-Wpedantic", source_path],
                capture_output=True, text=True, timeout=10
            )

            for line in result.stderr.splitlines():
                if ": warning:" not in line and ": error:" not in line:
                    continue

                parts = line.split(":", 4)
                try:
                    line_no = int(parts[1])
                except (ValueError, IndexError):
                    line_no = 0

                severity = "error" if ": error:" in line else "warning"
                message = parts[-1].strip()

                add_issue(
                    "gcc", severity, "compiler_diagnostic",
                    line_no, message
                )

        except Exception as e:
            analyzer_failures.append(f"GCC: {type(e).__name__}")
        # Formatting check using clang-format

    # Basic formatting check
    if params.get("enable_format_check", True):
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            bad_lines = 0

            for line in lines:
                stripped = line.strip()

                if len(stripped) > 120:
                    bad_lines += 1

                # Multiple statements on one line
                if stripped.count(";") > 1:
                    bad_lines += 1

                # Multiple control structures on one line
                if ("if(" in stripped or "for(" in stripped
                        or "while(" in stripped) and ";" in stripped:
                    bad_lines += 1

            if bad_lines > 0:
                add_issue(
                    "formatting",
                    "style",
                    "poor_formatting",
                    0,
                    f"Poor formatting detected in {bad_lines} places."
                )

        except Exception as e:
            analyzer_failures.append(
                f"Formatting check: {type(e).__name__}"
            )
    # 3. Lizard: cyclomatic complexity only
    if params.get("enable_lizard", True):
        try:
            analysis = lizard.analyze_file(source_path)

            for function in analysis.function_list:
                complexity = function.cyclomatic_complexity

                if complexity > max_complexity:
                    add_issue(
                        "lizard", "warning", "high_complexity",
                        function.start_line,
                        f"Function '{function.name}' has cyclomatic "
                        f"complexity {complexity}; maximum allowed is "
                        f"{max_complexity}."
                    )

        except Exception as e:
            analyzer_failures.append(f"Lizard: {type(e).__name__}")

    score = round(max(0.0, 1.0 - total_deduction), 2)

    if issues:
        error_count = sum(i["severity"] == "error" for i in issues)
        warning_count = sum(i["severity"] == "warning" for i in issues)
        style_count = sum(i["severity"] == "style" for i in issues)

        summary = []
        if error_count:
            summary.append(f"{error_count} error(s)")
        if warning_count:
            summary.append(f"{warning_count} warning(s)")
        if style_count:
            summary.append(f"{style_count} style issue(s)")

        highlights = [
            f"L{i['line']}: {i['message']}" for i in issues[:2]
        ]
        feedback = (
            f"Detected {', '.join(summary)}. Issues: "
            + "; ".join(highlights)
        )
    else:
        feedback = "No penalized issues detected."

    if analyzer_failures:
        feedback += " Analyzer failures: " + "; ".join(analyzer_failures)

    return {
        "score": score,
        "evidence": {
            "total_issues": len(issues),
            "issues": issues,
            "deductions_applied": round(total_deduction, 2),
            "analyzer_failures": analyzer_failures
        },
        "feedback": feedback
    }