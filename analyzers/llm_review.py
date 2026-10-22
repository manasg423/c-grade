import os
import re
import json
import requests

def _heuristic_readability_fallback(code: str) -> dict:
    """
    Deterministic static fallback if the local LLM server is unreachable.
    Evaluates formatting, identifier names, and comment presence.
    """
    lines = code.splitlines()
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return {"score": 0.0, "feedback": "Empty source file.", "reasons": ["No code present."]}

    has_comments = bool(re.search(r"//|/\*", code))
    short_vars = set(re.findall(r"\b(?:int|char|long|float|double)\s+([a-zA-Z])\b", code))
    indented_lines = sum(1 for l in non_empty if l.startswith("    ") or l.startswith("\t"))
    indent_ratio = indented_lines / max(1, len(non_empty))

    score = 10.0
    reasons = []

    if not has_comments:
        score -= 2.5
        reasons.append("No explanatory comments provided.")
    if short_vars:
        score -= 2.5
        reasons.append(f"Terse/single-letter variable names detected: {', '.join(sorted(short_vars))}.")
    if indent_ratio < 0.3:
        score -= 2.0
        reasons.append("Inconsistent or cramped indentation formatting.")

    score = max(0.0, min(10.0, score))
    normalized = round(score / 10.0, 2)
    feedback = "; ".join(reasons) if reasons else "Clean structure, consistent indentation, and good formatting."

    return {
        "score": normalized,
        "evidence": {
            "model_used": "deterministic-heuristic-fallback",
            "raw_response": {"score": score, "reasons": reasons}
        },
        "feedback": feedback
    }

def evaluate(source_path: str, binary_path: str, params: dict, limits: dict) -> dict:
    model = params.get("model", "qwen3.5:2b")
    temperature = params.get("temperature", 0)
    seed = params.get("seed", 42)

    if not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {"error": f"Source file not found: {source_path}"},
            "feedback": "Evaluation failed: source file missing."
        }

    with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
        code_str = f.read()

    system_prompt = (
        "You are an automated code quality reviewer for C programs. "
        "Judge readability, meaningful naming, comments, and clean layout ONLY. "
        "Do NOT check mathematical correctness or bugs. "
        "Output strictly valid JSON: {\"score\": <integer 0-10>, \"reasons\": [\"<point 1>\", \"<point 2>\"]}"
    )

    prompt = f"Evaluate this C code readability:\n\n```c\n{code_str}\n```"

    try:
        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "think": False,
            "options": {
                "temperature": temperature,
                "seed": seed,
                "num_predict": 256
            }
        }
        res = requests.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=45.0)

        if res.status_code == 200:
            raw_text = res.json().get("response", "").strip()

            # Clean any Markdown code fences if the model emitted them
            if "```" in raw_text:
                raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text).strip()

            parsed = json.loads(raw_text)

            raw_score = float(parsed.get("score", 5))
            raw_score = max(0.0, min(10.0, raw_score))
            normalized_score = round(raw_score / 10.0, 2)
            reasons = parsed.get("reasons", [])
            feedback = "; ".join(reasons) if reasons else "Readability review completed."

            return {
                "score": normalized_score,
                "evidence": {
                    "model": model,
                    "temperature": temperature,
                    "seed": seed,
                    "raw_response": parsed
                },
                "feedback": feedback
            }
    except Exception:
        pass

    return _heuristic_readability_fallback(code_str)