"""
core/reporter.py
----------------
Collates all analyzer results and first-pass gatekeeper data into a
structured final report. Writes two artefacts into the reports/ directory:

  reports/<problem>/<stem>.json  — machine-readable audit report (includes full evidence)
  reports/<problem>/<stem>.txt   — student-facing report (actionable, without plagiarism)

Public API
----------
  generate_report(submission_path, problem_id, rules, first_pass_result,
                  analyzer_results) -> dict
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tool_version(cmd: list[str]) -> str:
    """Return the first line of a tool's --version output, or 'n/a'."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
        return (result.stdout or result.stderr).splitlines()[0].strip()
    except Exception:
        return "n/a"


def _collect_tool_versions() -> dict:
    return {
        "gcc":      _tool_version(["gcc",      "--version"]),
        "cppcheck": _tool_version(["cppcheck", "--version"]),
        "python":   platform.python_version(),
        "platform": platform.platform(),
    }


def _letter_grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 65:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def _build_usefulness_limitations() -> dict:
    """Fixed usefulness/limitations section as specified by the assignment."""
    return {
        "usefulness": [
            {
                "use_case": "Automated grading for large university courses",
                "example": (
                    "An instructor grades 400+ student C submissions in minutes "
                    "with uniform, per-metric feedback instead of slow manual TA grading."
                ),
                "benefit": "Uniform, explainable feedback at scale."
            },
            {
                "use_case": "Technical hiring and coding-interview screens",
                "example": (
                    "A company evaluates 500 candidates on memory leaks (ASan), "
                    "time complexity, and edge-case safety; every rejection is justified "
                    "metric by metric."
                ),
                "benefit": "Objective, multi-dimensional first-round filtering."
            },
            {
                "use_case": "Plagiarism and LLM-copy triage for instructors",
                "example": (
                    "Two submissions reach a similarity of 0.93 against the 0.85 "
                    "threshold; the agent flags only that pair, saving the instructor "
                    "from reviewing all 300 files."
                ),
                "benefit": "Narrows manual review to a small set of suspicious cases."
            },
            {
                "use_case": "Self-service debugging lab for students",
                "example": (
                    "Students upload draft solutions to the Student Portal before the "
                    "deadline and immediately see compiler warnings, memory bugs, and "
                    "concrete counter-examples."
                ),
                "benefit": "Instant, actionable feedback without instructor involvement."
            },
        ],
        "limitations": [
            {
                "limitation": "Tests cannot prove full correctness",
                "example": (
                    "A submission passes all 10 hidden tests but fails when the input "
                    "has extra whitespace or a trailing newline that no test covers. "
                    "The agent still reports 10/10."
                ),
                "mitigation": (
                    "The agent auto-generates extra adversarial inputs via the LLM and "
                    "compares student output against a reference oracle.c."
                )
            },
            {
                "limitation": "Complexity estimator requires Linux perf or clang coverage",
                "example": (
                    "On a Windows host, perf is unavailable; the SanitizerCoverage "
                    "fallback needs clang. An O(N^2) bubble-sort may be misclassified "
                    "if both tools are absent."
                ),
                "mitigation": "Install WSL2 with perf, or use the clang fallback pipeline."
            },
            {
                "limitation": "Plagiarism detector can be fooled by deep refactoring",
                "example": (
                    "A student copies code but renames every identifier and reorders "
                    "all functions; the k-gram structural similarity may drop below 0.85 "
                    "and escape detection."
                ),
                "mitigation": (
                    "Lower the threshold in rules.json or enable AST-level diffing "
                    "with lizard's call-graph comparison."
                )
            },
            {
                "limitation": "LLM readability score depends on the local Ollama server",
                "example": (
                    "If Ollama is not running, the agent falls back to a heuristic "
                    "that cannot detect semantic naming issues like calling a variable "
                    "'tmp' where 'carry' is intended."
                ),
                "mitigation": "The deterministic heuristic fallback keeps the pipeline live."
            },
        ]
    }


def generate_actionable_suggestions(metrics: list[dict], first_pass: dict) -> list[str]:
    """
    Generates short, concrete, actionable suggestions for the student
    on how to improve their grade. Excludes plagiarism completely.
    """
    suggestions: list[str] = []

    # 1. Gatekeeper / Compilation / Infinite loop
    if not first_pass.get("compiled", True):
        return [
            "Fix compilation errors: verify syntax, add necessary header files (e.g. #include <stdio.h>), and check semicolon placement."
        ]
    if first_pass.get("suspected_infinite_loop", False):
        suggestions.append(
            "Fix potential infinite loop: ensure loop counters advance and termination conditions are reachable."
        )

    for m in metrics:
        a_name = m.get("analyzer", "")
        # NEVER give plagiarism suggestions to the student
        if a_name == "similarity_flag" or "plagiarism" in a_name.lower() or "originality" in a_name.lower():
            continue

        score = float(m.get("score", 1.0))
        ev = m.get("evidence", {}) or {}

        # Only give tips where student lost marks (score < 0.85)
        if score >= 0.85:
            continue

        if a_name == "function_split":
            suggestions.append(
                "Extract logic from main() into dedicated helper function(s) (e.g., calculation or validation helpers)."
            )

        elif a_name == "compiler_warnings":
            issues = ev.get("issues", [])
            tips_set = set()
            for issue in issues:
                msg = issue.get("message", "").lower()
                if "unused" in msg:
                    tips_set.add("remove unused variables")
                elif "format" in msg:
                    tips_set.add("fix printf/scanf format specifiers (e.g., %d, %lld)")
                elif "implicit" in msg:
                    tips_set.add("include required library headers")
                elif "return" in msg:
                    tips_set.add("ensure all non-void functions return a value")
            if tips_set:
                suggestions.append(f"Resolve compiler warnings: {', '.join(sorted(tips_set))}.")
            else:
                suggestions.append("Compile locally with 'gcc -Wall -Wextra' and resolve all diagnostic warnings.")

        elif a_name == "static_analysis":
            issues = ev.get("issues", [])
            has_complexity = any(i.get("id") == "high_complexity" for i in issues)
            has_formatting = any(i.get("id") == "poor_formatting" for i in issues)
            if has_complexity:
                suggestions.append(
                    "Reduce function complexity: simplify nested if/loop branches and keep functions under 40 lines."
                )
            elif has_formatting:
                suggestions.append(
                    "Improve code formatting: place one statement per line and maintain clean layout."
                )
            else:
                suggestions.append(
                    "Refactor code structure: adhere to standard C conventions and avoid deeply nested logic."
                )

        elif a_name == "edge_case_runner" or m.get("name") == "logical":
            failing = ev.get("failing", [])
            overflow_fail = any(
                "overflow" in str(f.get("note", "")).lower() or "overflow" in str(f.get("why", "")).lower()
                for f in failing
            )
            if overflow_fail:
                suggestions.append(
                    "Handle integer overflow explicitly: use wider types (e.g., 'long long') or pre-check arithmetic bounds."
                )
            else:
                suggestions.append(
                    "Account for edge cases: test your code with zero, negative numbers, and boundary limits."
                )

        elif a_name == "io_tests" or m.get("name") == "correctness":
            passed = ev.get("tests_passed", 0)
            total = ev.get("tests_total", 0)
            if total > 0:
                suggestions.append(
                    f"Fix functional errors: {passed}/{total} tests passed. Check standard output formatting and boundary cases."
                )
            else:
                suggestions.append(
                    "Review test cases: ensure standard input/output matches the problem specification exactly."
                )

        elif a_name == "sanitizer_run" or m.get("name") == "memory_safety":
            suggestions.append(
                "Fix memory safety issues: prevent out-of-bounds array access, initialize all pointers, and free allocated memory."
            )

        elif a_name == "complexity_estimator" or m.get("name") == "complexity":
            req = ev.get("required_complexity", ev.get("expected_complexity", "optimal"))
            suggestions.append(
                f"Optimize time complexity: reduce nested iterations to meet the required {req} asymptotic bound."
            )

        elif a_name == "llm_review" or m.get("name") == "readability":
            raw = ev.get("raw_response", {})
            reasons = raw.get("reasons", []) if isinstance(raw, dict) else []
            reasons_str = " ".join(reasons).lower()
            tips = []
            if "name" in reasons_str or "single-letter" in reasons_str or "terse" in reasons_str:
                tips.append("use descriptive variable names instead of single letters")
            if "comment" in reasons_str:
                tips.append("add a brief comment above main() explaining the approach")
            if "indent" in reasons_str or "cramp" in reasons_str:
                tips.append("format with consistent indentation")
            if tips:
                suggestions.append(f"Improve readability: {'; '.join(tips)}.")
            else:
                suggestions.append(
                    "Improve code readability: use meaningful variable names, add comments, and maintain consistent indentation."
                )

    # Deduplicate while preserving order
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            unique_suggestions.append(s)

    if not unique_suggestions:
        unique_suggestions.append(
            "Great job! Your submission meets all functional correctness, structure, and quality standards."
        )

    return unique_suggestions[:5]


# ---------------------------------------------------------------------------
# Human-readable text report builder (STUDENT VIEW)
# ---------------------------------------------------------------------------

def _build_text_report(report: dict) -> str:
    sub     = os.path.basename(report["submission"])
    grade   = report["grade"]
    metrics = report["metrics"]
    fp      = report["first_pass"]

    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(f"  C-Grade Student Feedback  —  {sub}")
    lines.append(f"  Generated : {report['generated_at']}")
    lines.append(f"  Problem   : {report['problem']}")
    lines.append("=" * 62)
    lines.append("")
    lines.append(
        f"  Overall Grade : {grade:.1f} / 100  [{_letter_grade(grade)}]"
    )
    lines.append("")

    # --- Gatekeeper summary ---
    lines.append("  -- Gatekeeper Status --")
    lines.append(f"  Compiled       : {'YES' if fp.get('compiled') else 'NO'}")
    lines.append(f"  Smoke Test     : {'PASSED' if fp.get('smoke_ok') else 'FAILED'}")
    if fp.get("suspected_infinite_loop"):
        lines.append("  Loop Check     : SUSPICIOUS (Potential infinite loop)")
    if fp.get("error"):
        lines.append(f"  Diagnostic     : {fp['error']}")
    lines.append("")

    # --- Student Score table (EXCLUDES PLAGIARISM) ---
    col_w = [18, 7, 9, 8]
    hdr = (
        f"  {'Metric':<{col_w[0]}} {'Weight':>{col_w[1]}} "
        f"{'Score':>{col_w[2]}} {'Contrib':>{col_w[3]}}  Summary"
    )
    lines.append(hdr)
    lines.append("  " + "-" * (len(hdr) - 2))

    for m in metrics:
        a_name = m.get("analyzer", "")
        m_name = m.get("name", "")
        # NEVER expose plagiarism/similarity to student
        if a_name == "similarity_flag" or "plagiarism" in m_name.lower() or "originality" in m_name.lower():
            continue

        name    = m_name.upper()
        weight  = m["weight"]
        score   = m["score"]
        contrib = m["contribution"]
        fb      = m.get("feedback", "")
        # Omit internal failure notes from student summary
        if "Analyzer failures:" in fb:
            fb = fb.split("Analyzer failures:")[0].strip()
        fb_s    = (fb[:50] + "...") if len(fb) > 53 else fb
        w_pct   = f"{int(round(weight * 100))}%"
        s_fmt   = f"{score * 10:.1f}/10"
        c_fmt   = f"{contrib:.1f}"
        lines.append(
            f"  {name:<{col_w[0]}} {w_pct:>{col_w[1]}} "
            f"{s_fmt:>{col_w[2]}} {c_fmt:>{col_w[3]}}  {fb_s}"
        )

    lines.append("")

    # --- Counter-examples (failing tests) ---
    failing_tests: list[dict] = []
    for m in metrics:
        # Don't check plagiarism
        if m.get("analyzer") == "similarity_flag":
            continue
        for fail in m.get("evidence", {}).get("failing", []):
            failing_tests.append({"_metric": m["name"], **fail})

    if failing_tests:
        lines.append("  -- Where Your Code Failed --")
        for i, ft in enumerate(failing_tests[:4], 1):
            m_label = ft.get("_metric", "test").upper()
            inp = str(ft.get("input", "")).replace("\n", " ").strip()
            exp = str(ft.get("expected", "")).replace("\n", " ").strip()
            got = str(ft.get("got", "")).replace("\n", " ").strip()
            lines.append(f"  Test {i:02d}  [{m_label}]")
            lines.append(f"          INPUT    : {inp or '(empty)'}")
            lines.append(f"          EXPECTED : {exp or '(empty)'}")
            lines.append(f"          GOT      : {got or '(no output / crashed)'}")
            if ft.get("note"):
                lines.append(f"          WHY      : {ft['note']}")
        lines.append("")

    # --- Meaningful, actionable suggestions (NO PLAGIARISM) ---
    actionable_tips = generate_actionable_suggestions(metrics, fp)
    lines.append("  -- Suggestions to Raise Your Grade --")
    for tip in actionable_tips:
        lines.append(f"  - {tip}")
    lines.append("")

    lines.append("=" * 62)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    submission_path: str,
    problem_id: str,
    rules: dict,
    first_pass_result: dict,
    analyzer_results: dict,
) -> dict:
    """
    Build the full report dictionary, write it to disk, and return it.

    Parameters
    ----------
    submission_path   : absolute or relative path to the student's .c file
    problem_id        : problem slug (e.g. "add", "sort")
    rules             : the parsed rules.json dict
    first_pass_result : dict returned by core.first_pass.run_first_pass()
    analyzer_results  : {analyzer_name: {"score", "evidence", "feedback"}}

    Returns
    -------
    The in-memory report dict (same object written to JSON).
    """
    metrics_cfg  = rules.get("metrics", [])
    total_score  = 0.0
    total_weight = 0.0
    metric_rows: list[dict] = []
    flags: list[str] = []

    for m in metrics_cfg:
        a_name = m["analyzer"]
        m_name = m.get("name", a_name)
        weight = float(m.get("weight", 0.0))
        total_weight += weight

        res    = analyzer_results.get(a_name, {})
        score  = float(res.get("score", 0.0))
        total_score  += score * weight
        contribution  = round(score * weight * 100, 1)

        # Collect similarity/plagiarism flag for instructor audit JSON only
        evidence = res.get("evidence", {})
        if a_name == "similarity_flag" and evidence.get("flagged"):
            flags.append(
                f"Plagiarism suspected: similarity "
                f"{evidence.get('effective_similarity', '?')} "
                f"against {evidence.get('most_similar_file', 'unknown')}."
            )

        metric_rows.append({
            "name":         m_name,
            "analyzer":     a_name,
            "weight":       weight,
            "score":        round(score, 4),
            "contribution": contribution,
            "evidence":     evidence,
            "feedback":     res.get("feedback", ""),
        })

    final_grade = (
        round((total_score / total_weight) * 100, 1)
        if total_weight > 0 else 0.0
    )

    rules_hash = first_pass_result.get("rules_hash", "")
    suggestions = generate_actionable_suggestions(metric_rows, first_pass_result)

    report = {
        "submission":       os.path.basename(submission_path),
        "submission_path":  submission_path,
        "problem":          problem_id,
        "rules_hash":       rules_hash,
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "grade":            final_grade,
        "letter_grade":     _letter_grade(final_grade),
        "first_pass": {
            "compiled":                first_pass_result.get("compiled",                 False),
            "smoke_ok":                first_pass_result.get("smoke_ok",                 False),
            "suspected_infinite_loop": first_pass_result.get("suspected_infinite_loop",  False),
            "static_sanity":           first_pass_result.get("static_sanity",            False),
            "error":                   first_pass_result.get("error"),
        },
        "metrics":               metric_rows,
        "suggestions":           suggestions,
        "flags":                 flags,
        "usefulness_limitations": _build_usefulness_limitations(),
        "tool_versions":          _collect_tool_versions(),
    }

    # ------------------------------------------------------------------
    # Persist to disk
    # ------------------------------------------------------------------
    stem       = os.path.splitext(os.path.basename(submission_path))[0]
    report_dir = os.path.join("reports", problem_id)
    os.makedirs(report_dir, exist_ok=True)

    json_path = os.path.join(report_dir, f"{stem}.json")
    txt_path  = os.path.join(report_dir, f"{stem}.txt")

    with open(json_path, "w", encoding="utf-8") as fj:
        json.dump(report, fj, indent=2, default=str)

    with open(txt_path, "w", encoding="utf-8") as ft:
        ft.write(_build_text_report(report))

    report["_json_path"] = json_path
    report["_txt_path"]  = txt_path

    return report
