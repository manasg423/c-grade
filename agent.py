"""
agent.py  —  C-Grade Main Orchestrator
=======================================
CLI entry-point that runs the full grading pipeline unattended:

  python agent.py --source <file.c> --rules <rules.json>

The agent:
  1. Reads and validates the rules JSON (weights must sum to 1.00).
  2. Runs the Phase-1 Gatekeeper (compile, static-sanity, smoke-test).
  3. Dispatches all configured specialist analyzers in parallel via
     ThreadPoolExecutor, forwarding source, binary, params, and limits.
  4. Collates results and calls core.reporter.generate_report() to write
     both a machine-readable JSON report and a human-readable .txt report.
  5. Prints a clean summary to stdout.

Contract: same submission + same rules = same grade.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure project root is importable regardless of cwd
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.first_pass import run_first_pass
from core.reporter import generate_report


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_WORKERS = 8          # Max parallel analyzer threads
EXECUTION_ANALYZERS = {  # These need a valid binary; skip on smoke-test fail
    "io_tests",
    "edge_case_runner",
    "sanitizer_run",
    "complexity_estimator",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rules(rules_path: str) -> dict:
    """Load rules JSON; raise ValueError if weights don't sum to ~1.00."""
    if not os.path.exists(rules_path):
        raise FileNotFoundError(f"Rules file not found: {rules_path}")

    with open(rules_path, "r", encoding="utf-8") as f:
        rules = json.load(f)

    total_w = sum(float(m.get("weight", 0.0)) for m in rules.get("metrics", []))
    if abs(total_w - 1.0) > 1e-4:
        raise ValueError(
            f"Rules weights sum to {total_w:.4f}, expected 1.00. "
            f"Fix {rules_path}."
        )
    return rules


def _run_single_analyzer(
    analyzer_name: str,
    source_path: str,
    binary_path: str,
    params: dict,
    limits: dict,
) -> tuple[str, dict]:
    """
    Load and call analyzers.<name>.evaluate(…) safely.
    Returns (analyzer_name, result_dict).
    The result always has keys: score, evidence, feedback.
    """
    try:
        mod = importlib.import_module(f"analyzers.{analyzer_name}")
        mod = importlib.reload(mod)          # always use the freshest code
        eval_fn = getattr(mod, "evaluate", None)

        if eval_fn is None:
            return analyzer_name, {
                "score":    0.0,
                "evidence": {"error": f"No evaluate() in analyzers/{analyzer_name}.py"},
                "feedback": f"{analyzer_name}: missing evaluate().",
            }

        # Support analyzers with varying signatures
        sig        = inspect.signature(eval_fn)
        num_params = len(sig.parameters)

        if num_params >= 4:
            res = eval_fn(source_path, binary_path, params, limits)
        elif num_params == 3:
            res = eval_fn(source_path, binary_path, params)
        elif num_params == 2:
            res = eval_fn(source_path, params)
        else:
            res = eval_fn(source_path)

        if isinstance(res, dict):
            return analyzer_name, {
                "score":    float(res.get("score", 0.0)),
                "evidence": res.get("evidence", res),
                "feedback": str(res.get("feedback", "Completed.")),
            }

        return analyzer_name, {
            "score":    0.0,
            "evidence": {"raw_return": str(res)},
            "feedback": "Invalid analyzer return format.",
        }

    except Exception as exc:
        return analyzer_name, {
            "score":    0.0,
            "evidence": {"error": f"{type(exc).__name__}: {exc}"},
            "feedback": f"{analyzer_name} raised {type(exc).__name__}: {exc}",
        }


def _dispatch_analyzers(
    metrics: list[dict],
    source_path: str,
    binary_path: str,
    limits: dict,
    skip_execution: bool = False,
) -> dict[str, dict]:
    """
    Run all enabled analyzers in parallel.
    If skip_execution=True, execution-based analyzers are replaced with a
    placeholder (0 score) so source-only analyzers still run.
    """
    futures = {}
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for m in metrics:
            a_name = m["analyzer"]
            params = dict(m.get("params", {}))

            if skip_execution and a_name in EXECUTION_ANALYZERS:
                results[a_name] = {
                    "score":    0.0,
                    "evidence": {"skipped": "Binary failed smoke test; execution skipped."},
                    "feedback": f"{a_name} skipped: binary did not pass smoke test.",
                }
                continue

            future = pool.submit(
                _run_single_analyzer,
                a_name, source_path, binary_path, params, limits,
            )
            futures[future] = a_name

        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result

    return results


def _print_summary(report: dict) -> None:
    """Pretty-print a concise grading summary to stdout."""
    grade   = report["grade"]
    letter  = report["letter_grade"]
    sub     = report["submission"]
    metrics = report["metrics"]

    print()
    print("=" * 62)
    print(f"  C-Grade — {sub}")
    print("=" * 62)
    print(f"  Final Grade : {grade:.1f} / 100  [{letter}]")
    print()
    print(f"  {'Metric':<22} {'W%':>4}  {'Score':>8}  {'Contrib':>7}")
    print("  " + "-" * 48)
    for m in metrics:
        w_pct = int(round(m["weight"] * 100))
        s_10  = f"{m['score'] * 10:.1f}/10"
        print(
            f"  {m['name'].upper():<22} {w_pct:>4}%  "
            f"{s_10:>8}  {m['contribution']:>6.1f}"
        )
    print()

    flags = report.get("flags", [])
    if flags:
        print("  FLAGS:")
        for f in flags:
            print(f"  ⚑  {f}")
        print()

    print(f"  JSON report : {report.get('_json_path', 'n/a')}")
    print(f"  TXT report  : {report.get('_txt_path',  'n/a')}")
    print("=" * 62)
    print()


# ---------------------------------------------------------------------------
# Public grading function (called by both CLI and ui.py)
# ---------------------------------------------------------------------------

def grade(
    source_path: str,
    rules_path: str,
    verbose: bool = False,
) -> dict:
    """
    Run the full C-Grade pipeline and return the finished report dict.

    Parameters
    ----------
    source_path : path to the student's .c file
    rules_path  : path to the rules JSON file
    verbose     : if True, print per-analyzer status lines

    Returns
    -------
    The report dict produced by core.reporter.generate_report().
    """
    t0 = time.time()

    # 1. Load rules
    rules      = _load_rules(rules_path)
    problem_id = rules.get("problem", os.path.splitext(os.path.basename(rules_path))[0])
    metrics    = rules.get("metrics", [])
    limits     = {
        "time_limit_s":    rules.get("time_limit_s",    1.0),
        "memory_limit_mb": rules.get("memory_limit_mb", 64),
    }
    smoke_input = rules.get("smoke_input", "7 5\n")

    if verbose:
        print(f"[agent] Rules loaded: {rules_path}  (problem={problem_id})")

    # 2. Phase-1 Gatekeeper
    if verbose:
        print("[agent] Running Phase-1 Gatekeeper …")

    fp_result  = run_first_pass(source_path, rules_path, smoke_input)
    compiled   = fp_result.get("compiled", False)
    smoke_ok   = fp_result.get("smoke_ok", False)
    binary_path = fp_result.get("binary_path", "")

    if verbose:
        print(
            f"[agent] Gatekeeper: compiled={compiled}  "
            f"smoke={'OK' if smoke_ok else 'FAIL'}  "
            f"loop={fp_result.get('suspected_infinite_loop', False)}"
        )

    # If the file can't even compile, skip all analyzers
    if not compiled:
        report = generate_report(
            source_path, problem_id, rules, fp_result, {}
        )
        if verbose:
            print(f"[agent] Compilation failed. Partial report written.")
        _print_summary(report)
        return report

    # 3. Dispatch analyzers in parallel
    skip_exec = not smoke_ok
    if verbose:
        exec_note = " (execution-based analyzers skipped)" if skip_exec else ""
        print(f"[agent] Dispatching {len(metrics)} analyzers in parallel{exec_note} …")

    analyzer_results = _dispatch_analyzers(
        metrics, source_path, binary_path, limits, skip_execution=skip_exec
    )

    if verbose:
        for name, res in analyzer_results.items():
            print(f"  [{name}]  score={res['score']:.2f}  {res['feedback'][:60]}")

    # 4. Generate and persist report
    report = generate_report(
        source_path, problem_id, rules, fp_result, analyzer_results
    )

    elapsed = time.time() - t0
    if verbose:
        print(f"[agent] Done in {elapsed:.1f}s")

    _print_summary(report)
    return report


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="C-Grade: Automated C-Program Grader",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--source", "-s",
        required=True,
        help="Path to the student's .c source file."
    )
    parser.add_argument(
        "--rules", "-r",
        required=True,
        help="Path to the rules JSON file (e.g. rules/add_rules.json)."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-phase status lines to stdout."
    )

    args = parser.parse_args()

    try:
        grade(args.source, args.rules, verbose=args.verbose)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[agent] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[agent] Interrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

