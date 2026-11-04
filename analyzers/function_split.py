import os
import re


def _strip_comments_and_strings(code: str) -> str:
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"//.*", "", code)
    return code


def evaluate(source_path: str, binary_path: str = "", params: dict = None, limits: dict = None) -> dict:
    if params is None:
        params = {}

    if not source_path or not os.path.exists(source_path):
        return {
            "score": 0.0,
            "evidence": {"error": "Source file not found."},
            "feedback": "Evaluation failed: missing source file."
        }

    with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
        raw_code = f.read()

    clean_code = _strip_comments_and_strings(raw_code)

    func_pattern = re.compile(
        r'\b(?:int|void|char|float|double|long|short|unsigned|bool|_Bool|size_t)\s*(?:\*\s*)?'
        r'([a-zA-Z_]\w*)\s*'
        r'\([^;{}]*\)\s*\{'
    )

    functions = []
    for match in func_pattern.finditer(clean_code):
        func_name = match.group(1)
        if func_name not in ["if", "while", "for", "switch", "return"] and func_name not in functions:
            functions.append(func_name)

    has_main = "main" in functions
    if not has_main and re.search(r'\bmain\s*\(', clean_code):
        has_main = True
        functions.append("main")

    helpers = [f for f in functions if f != "main"]

    if not helpers:
        score = 0.0
        feedback = "Monolithic code detected. All logic is crammed into main(). Modularize your code using helper functions."
    else:
        score = 1.0
        feedback = f"Good modularity: Detected main() and {len(helpers)} helper function(s) ({', '.join(helpers)})."

    return {
        "score": score,
        "evidence": {
            "detected_functions": functions,
            "helper_count": len(helpers),
            "helpers": helpers,
            "is_monolithic": len(helpers) == 0
        },
        "feedback": feedback
    }