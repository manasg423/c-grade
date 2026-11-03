from __future__ import annotations

import os
import re
import json
import requests
from typing import Any


# ---------------------------------------------------------------------------
# Heuristic Fallback with Professional English Feedback
# ---------------------------------------------------------------------------

def _heuristic_readability_fallback(code: str, failure_reason: str = "") -> dict[str, Any]:
    lines = code.splitlines()
    non_empty = [l for l in lines if l.strip()]

    if not non_empty:
        return {
            "score": 0.0,
            "feedback": (
                "Summary: The submitted source file is empty.\n\n"
                "How to Improve:\n"
                "  - Provide a valid C implementation containing executable routines and logic."
            ),
            "evidence": {"reasons": ["No source code detected."]}
        }

    has_comments = bool(re.search(r"//|/\*[\s\S]*?\*/", code))

    cryptic_vars = (
        set(re.findall(
            r"\b(?:int|char|long|float|double)\s+([a-zA-Z])\b",
            code
        )) - {"i", "j", "k", "n"}
    )

    indented_lines = sum(
        1 for l in non_empty
        if l.startswith("    ") or l.startswith("\t")
    )

    indent_ratio = indented_lines / max(1, len(non_empty))

    score = 10.0
    strengths: list[str] = []
    improvements: list[str] = []

    # Documentation assessment
    if not has_comments:
        score -= 1.5
        improvements.append(
            "Documentation: Include concise function headers and inline comments explaining complex algorithm steps."
        )
    else:
        strengths.append(
            "Documentation: Appropriate comments and explanatory annotations are present."
        )

    # Identifier naming assessment
    if cryptic_vars:
        score -= 1.5
        var_list = ", ".join(f"'{v}'" for v in sorted(cryptic_vars))
        improvements.append(
            f"Identifier Naming: Replace single-character identifiers ({var_list}) with self-documenting, descriptive names (e.g., 'primary_input', 'total_count')."
        )
    else:
        strengths.append(
            "Identifier Naming: Variable and function identifiers follow descriptive naming standards."
        )

    # Formatting assessment
    if indent_ratio < 0.3:
        score -= 1.5
        improvements.append(
            "Code Formatting: Standardize indentation across all blocks (prefer 4 spaces or 1 tab consistently) to improve structural hierarchy."
        )
    else:
        strengths.append(
            "Code Formatting: Clean vertical spacing and consistent block indentation maintained."
        )

    score = max(0.0, min(10.0, score))
    normalized = round(score / 10.0, 2)

    feedback_sections = [
        "Summary: The code exhibits acceptable baseline structure, with opportunities to enhance technical documentation and readability."
    ]

    if strengths:
        feedback_sections.append(
            "Strengths:\n  - " + "\n  - ".join(strengths)
        )

    if improvements:
        feedback_sections.append(
            "Areas for Improvement:\n  - " + "\n  - ".join(improvements)
        )
    else:
        feedback_sections.append(
            "Areas for Improvement:\n  - Code structure fully adheres to professional readability standards."
        )

    return {
        "score": normalized,
        "evidence": {
            "model_used": "deterministic-heuristic-fallback",
            "fallback_trigger_reason": failure_reason,
            "raw_response": {
                "score": score,
                "strengths": strengths,
                "improvements": improvements
            }
        },
        "feedback": "\n\n".join(feedback_sections)
    }


# ---------------------------------------------------------------------------
# Helper: Extract Valid JSON Object
# ---------------------------------------------------------------------------

def _extract_json(raw_text: str) -> dict[str, Any] | None:
    cleaned = raw_text.strip()

    if not cleaned:
        return None

    if "```" in cleaned:
        cleaned = re.sub(
            r"^```[a-zA-Z]*\s*\n?",
            "",
            cleaned
        )
        cleaned = re.sub(
            r"\n?```\s*$",
            "",
            cleaned
        ).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)

    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Main Analyzer Entry Point
# ---------------------------------------------------------------------------

def evaluate(
    source_path: str,
    binary_path: str = "",
    params: dict | None = None,
    limits: dict | None = None
) -> dict[str, Any]:

    params = params or {}
    limits = limits or {}

    model = str(params.get("model", "qwen3.5:2b"))
    temperature = float(params.get("temperature", 0.1))
    seed = int(params.get("seed", 42))

    if not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {
                "error": f"Source file not found: {source_path}"
            },
            "feedback": "Evaluation failed: The designated source file does not exist on disk."
        }

    with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
        code_str = f.read()

    # -----------------------------------------------------------------------
    # Strict Professional Readability Evaluation Prompt
    # -----------------------------------------------------------------------

    system_prompt = (
        "You are an objective C code readability evaluator. "
        "Evaluate each submission independently. Do not assume that every submission "
        "deserves a score between 7 and 9.\n\n"

        "Evaluate ONLY these three dimensions:\n"

        "1. Naming conventions (40%): Meaningful variable names, function names, "
        "descriptive identifiers and consistent naming.\n"

        "2. Formatting (40%): Consistent indentation, spacing, line organization, "
        "braces, code layout and structural hierarchy.\n"

        "3. Documentation (20%): Useful comments, function descriptions and "
        "explanations of complex logic.\n\n"

        "STRICT SCORING RUBRIC (0-10):\n"

        "8.6-10.0: Good readability but some noticeable weaknesses.\n"

        "7.0-8.5: Average readability with multiple improvement opportunities.\n"

        "5.0-6.9: Poor readability with inconsistent formatting or weak naming.\n"

        "IMPORTANT EVALUATION RULES:\n"

        "- Evaluate each category independently using actual code evidence.\n"
        "- Do not give high scores merely because code is functional or correct.\n"
        "- Do not give identical scores to submissions with noticeably different readability.\n"
        "- Reward genuinely consistent indentation and meaningful identifiers.\n"
        "- Penalize missing documentation proportionately, not excessively.\n"
        "- Do not penalize short programs merely for having fewer comments.\n"
        "- Do not assess algorithmic complexity or mathematical correctness.\n"
        "- Avoid default scores. Select scores based on observed evidence.\n\n"

        "All feedback MUST use formal professional technical English.\n\n"

        "Return ONLY a valid JSON object matching this schema:\n"

        "{\n"
        '  "naming": 8.5,\n'
        '  "formatting": 9.0,\n'
        '  "comments": 7.5,\n'
        '  "summary": "Professional 3-4 words  evaluation.",\n'
        "}"
    )

    prompt = (
        "Perform a strict, evidence-based readability evaluation on this C code. "
        "Assign scores independently for naming, formatting and comments. "
        "Return the required JSON only:\n\n"
        f"```c\n{code_str}\n```"
    )

    llm_err_msg = ""

    try:
        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": temperature,
                "seed": seed,
                "num_predict": 512,
            }
        }

        # Ollama API Request
        res = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json=payload,
            timeout=120.0
        )

        if res.status_code == 200:
            response_data = res.json()
            raw_text = response_data.get("response", "").strip()

            if not raw_text:
                print(
                    "OLLAMA DEBUG:",
                    json.dumps(response_data, indent=2)
                )

                raise ValueError(
                    f"Ollama returned an empty response. "
                    f"Model: {model}, "
                    f"Done reason: {response_data.get('done_reason', 'unknown')}"
                )

            parsed = _extract_json(raw_text)

            if not parsed:
                raise ValueError(
                    f"Unable to parse structured JSON from model output: {raw_text[:120]}"
                )

            naming = float(parsed.get("naming", 7.0))
            formatting = float(parsed.get("formatting", 7.0))
            comments = float(parsed.get("comments", 5.0))

            # Weighted scoring: Formatting 40%, Naming 40%, Comments 20%
            weighted = (
                formatting * 0.40
                + naming * 0.40
                + comments * 0.20
            )

            weighted = max(0.0, min(10.0, weighted))
            normalized_score = round(weighted / 10.0, 2)

            summary = parsed.get(
                "summary",
                "Readability evaluation completed."
            )

            strengths = parsed.get("strengths", [])
            suggestions = parsed.get("suggestions", [])

            # Professional feedback presentation
            feedback_sections = [
                f"Summary: {summary}"
            ]

            formatted_feedback = "\n\n".join(feedback_sections)

            return {
                "score": normalized_score,
                "evidence": {
                    "model": model,
                    "raw_response": parsed
                },
                "feedback": formatted_feedback
            }

        else:
            llm_err_msg = f"HTTP {res.status_code}: {res.text[:200]}"

    except Exception as exc:
        llm_err_msg = f"{type(exc).__name__}: {str(exc)}"

    # Heuristic fallback
    return _heuristic_readability_fallback(
        code_str,
        failure_reason=llm_err_msg
    )