"""Translate teacher assignment controls into the C-Grade rules format."""

from __future__ import annotations


def build_rules(
    problem_id: str,
    time_limit: float,
    memory_limit: int,
    sample_input: str,
    input_type: str,
    expected_complexity: str,
    auto_gen_count: int,
    enabled: dict[str, bool],
    sample_output: str = "",
) -> dict:
    analyzer_specs = [
        ("io_tests", "correctness", 0.20, {
            "testset_dir": f"problems/{problem_id}/tests/",
            "oracle_path": f"problems/{problem_id}/tests/oracle.c",
            "auto_generate": True,
            "num_generated_tests": int(auto_gen_count),
            "seed": 42,
        }),
        ("edge_case_runner", "logical", 0.10, {
            "oracle_path": f"problems/{problem_id}/tests/oracle.c",
            "cases": ["empty", "whitespace"],
            "test_inputs": [sample_input, "0", "-1"],
            "seed": 42,
        }),
        ("sanitizer_run", "memory_safety", 0.10, {"sample_input": sample_input}),
        ("complexity_estimator", "complexity", 0.10, {
            "input_type": input_type,
            "expected_complexity": expected_complexity,
            "trials": 5,
            "base_n": 10,
            "scale_step": 20,
        }),
        ("static_analysis", "static_analysis", 0.10, {
            "enable_cppcheck": True,
            "enable_gcc_warnings": True,
            "error_penalty": 0.30,
            "warning_penalty": 0.10,
        }),
        ("compiler_warnings", "compiler_warnings", 0.10, {}),
        ("function_split", "modularity", 0.10, {}),
        ("similarity_flag", "plagiarism_check", 0.10, {
            "submissions_dir": f"problems/{problem_id}/submissions/",
        }),
        ("llm_review", "readability", 0.10, {
            "model": "qwen3.5:2b",
            "temperature": 0,
            "seed": 42,
        }),
    ]
    selected = [spec for spec in analyzer_specs if enabled.get(spec[0], False)]
    if not selected:
        raise ValueError("Select at least one analyzer.")

    total_weight = sum(spec[2] for spec in selected)
    accumulated = 0.0
    metrics = []
    for index, (analyzer, name, raw_weight, params) in enumerate(selected):
        weight = (
            round(1.0 - accumulated, 2)
            if index == len(selected) - 1
            else round(raw_weight / total_weight, 2)
        )
        accumulated += weight
        metrics.append({
            "name": name,
            "analyzer": analyzer,
            "weight": weight,
            "params": params,
        })

    return {
        "problem": problem_id,
        "time_limit_s": time_limit,
        "memory_limit_mb": int(memory_limit),
        "smoke_input": sample_input,
        "sample_output": sample_output,
        "metrics": metrics,
    }
