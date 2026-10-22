import sys
import json
import argparse
import importlib
from core.first_pass import run_first_pass

def test_submission(source_path: str, analyzer_name: str, rules_path: str = "rules/add_rules.json"):
    print("=" * 65)
    print(f"RUNNING EVALUATION ON: {source_path}")
    print("=" * 65)
    
    # Stage 1: Gatekeeper Sanity Sweep
    gatekeeper = run_first_pass(source_path, rules_path)
    print(f"Rules Valid:            {gatekeeper['rules_valid']}")
    print(f"Static Sanity:          {gatekeeper['static_sanity']}")
    print(f"Compilation:            {gatekeeper['compiled']}")
    print(f"Smoke Test:             {'PASSED' if gatekeeper['smoke_ok'] else 'FAILED'}")
    print(f"Suspected Loop/Crash:   {gatekeeper['suspected_infinite_loop']}")
    
    if gatekeeper["error"]:
        print(f"Gatekeeper Note:        {gatekeeper['error']}")
    
    if not gatekeeper["smoke_ok"]:
        print("\nStopping: Binary failed smoke check or timed out.")
        print("=" * 65)
        return

    # Stage 2: Run Selected Analyzer
    print("-" * 65)
    print(f"RUNNING ANALYZER: {analyzer_name}")
    print("-" * 65)
    
    with open(rules_path, "r") as f:
        rules = json.load(f)
        
    params = {}
    for m in rules.get("metrics", []):
        if m.get("analyzer") == analyzer_name:
            params = m.get("params", {})
            break
            
    limits = {
        "time_limit_s": rules.get("time_limit_s", 1.0),
        "memory_limit_mb": rules.get("memory_limit_mb", 64)
    }
    
    module = importlib.import_module(f"analyzers.{analyzer_name}")
    result = module.evaluate(source_path, gatekeeper["binary_path"], params, limits)
    
    print(f"Score:    {result['score']}")
    print(f"Feedback: {result['feedback']}")
    print("Evidence:")
    print(json.dumps(result["evidence"], indent=2))
    print("=" * 65)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test C-Grade Analyzers on Submissions")
    parser.add_argument("--source", type=str, default="submissions/add.c", help="Path to student C file")
    parser.add_argument("--analyzer", type=str, default="io_tests", help="Analyzer name (e.g., io_tests, sanitizer_run)")
    parser.add_argument("--rules", type=str, default="rules/add_rules.json", help="Path to rules.json")
    
    args = parser.parse_args()
    test_submission(args.source, args.analyzer, args.rules)