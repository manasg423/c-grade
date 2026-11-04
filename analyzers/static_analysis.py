
import os
import re
import subprocess
import xml.etree.ElementTree as ET

try:
    import lizard
except ImportError:
    lizard = None


def evaluate(
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict
) -> dict:
    if not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {
                "error": f"Source file not found: {source_path}"
            },
            "feedback": "Evaluation failed: source file missing."
        }

    params = params or {}

    deductions = {
        "error": float(params.get("error_penalty", 0.40)),
        "warning": float(params.get("warning_penalty", 0.15)),
        "style": float(params.get("style_penalty", 0.05))
    }

    max_complexity = int(params.get("max_cyclomatic", 8))

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

    if params.get("enable_cppcheck", True):

        try:
            result = subprocess.run(
                [
                    "cppcheck",
                    "--enable=warning,style,performance,portability",
                    "--xml",
                    "--xml-version=2",
                    source_path
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            xml_output = result.stderr.strip()

            if xml_output:
                try:
                    root = ET.fromstring(xml_output)

                    for error in root.findall(".//error"):

                        issue_id = error.get("id", "unknown")

                        if issue_id in (
                            "missingInclude",
                            "missingIncludeSystem",
                            "checkersReport"
                        ):
                            continue

                        location = error.find("location")

                        line_no = 0

                        if location is not None:
                            try:
                                line_no = int(
                                    location.get("line", 0)
                                )
                            except (ValueError, TypeError):
                                line_no = 0

                        severity = error.get(
                            "severity", "warning"
                        ).lower()

                        if severity not in deductions:
                            severity = "warning"

                        add_issue(
                            "cppcheck",
                            severity,
                            issue_id,
                            line_no,
                            error.get("msg", "")
                        )

                except ET.ParseError:
                    analyzer_failures.append(
                        "Cppcheck: Invalid XML output"
                    )

        except subprocess.TimeoutExpired:
            analyzer_failures.append("Cppcheck: Timeout")

        except FileNotFoundError:
            analyzer_failures.append("Cppcheck: Not installed")

        except Exception as e:
            analyzer_failures.append(
                f"Cppcheck: {type(e).__name__}"
            )

    if params.get("enable_gcc_warnings", True):

        try:
            result = subprocess.run(
                [
                    "gcc",
                    "-fsyntax-only",
                    "-Wall",
                    "-Wextra",
                    "-Wpedantic",
                    source_path
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            diagnostic_pattern = re.compile(
                r"^(.*?):(\d+)(?::\d+)?:\s*"
                r"(warning|error):\s*(.*)$"
            )

            for output_line in result.stderr.splitlines():

                match = diagnostic_pattern.match(output_line)

                if not match:
                    continue

                line_no = int(match.group(2))
                severity = match.group(3).lower()
                message = match.group(4).strip()

                add_issue(
                    "gcc",
                    severity,
                    "compiler_diagnostic",
                    line_no,
                    message
                )

        except subprocess.TimeoutExpired:
            analyzer_failures.append("GCC: Timeout")

        except FileNotFoundError:
            analyzer_failures.append("GCC: Not installed")

        except Exception as e:
            analyzer_failures.append(
                f"GCC: {type(e).__name__}"
            )

    if params.get("enable_format_check", True):

        try:
            with open(
                source_path,
                "r",
                encoding="utf-8",
                errors="replace"
            ) as f:
                lines = f.readlines()

            bad_lines = 0

            for line in lines:

                stripped = line.strip()

                if not stripped:
                    continue

                if len(line.rstrip("\r\n")) > 120:
                    bad_lines += 1

                statement_line = re.sub(
                    r"\bfor\s*\([^)]*\)",
                    "",
                    stripped
                )

                if statement_line.count(";") > 1:
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

    if params.get("enable_lizard", True):

        if lizard is None:

            analyzer_failures.append(
                "Lizard: Package not installed"
            )

        else:

            try:
                analysis = lizard.analyze_file(source_path)

                for function in analysis.function_list:

                    complexity = (
                        function.cyclomatic_complexity
                    )

                    if complexity > max_complexity:

                        add_issue(
                            "lizard",
                            "warning",
                            "high_complexity",
                            function.start_line,
                            (
                                f"Function '{function.name}' has "
                                f"cyclomatic complexity {complexity}; "
                                f"maximum allowed is {max_complexity}."
                            )
                        )

            except Exception as e:
                analyzer_failures.append(
                    f"Lizard: {type(e).__name__}"
                )

    score = round(
        max(0.0, 1.0 - total_deduction),
        2
    )

    error_count = sum(
        1 for issue in issues
        if issue["severity"] == "error"
    )

    warning_count = sum(
        1 for issue in issues
        if issue["severity"] == "warning"
    )

    style_count = sum(
        1 for issue in issues
        if issue["severity"] == "style"
    )

    if analyzer_failures:

        feedback = "Static analysis incomplete; review diagnostics."

    elif error_count and warning_count and style_count:

        feedback = (
            "Fix errors, warnings, and improve code quality."
        )

    elif error_count and warning_count:

        feedback = (
            "Fix errors and resolve compiler warnings."
        )

    elif error_count and style_count:

        feedback = (
            "Fix errors and improve code quality."
        )

    elif warning_count and style_count:

        feedback = (
            "Resolve warnings and improve code quality."
        )

    elif error_count:

        feedback = "Fix detected errors before submission."

    elif warning_count:

        feedback = "Resolve all detected compiler warnings."

    elif style_count:

        feedback = "Improve code formatting and readability."

    elif not issues:

        feedback = "No static analysis issues detected."

    else:

        feedback = "Review detected static analysis issues."

    return {
        "score": score,

        "evidence": {
            "total_issues": len(issues),

            "error_count": error_count,
            "warning_count": warning_count,
            "style_count": style_count,

            "issues": issues,

            "deductions_applied": round(
                total_deduction, 2
            ),

            "analyzer_failures": analyzer_failures
        },

        "feedback": feedback
    }
