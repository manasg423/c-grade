"""LLM-guided stdin analysis and raw-input generation for C-Grade Engine.
The model is asked to understand the complete C program and produce actual raw
stdin text for each requested benchmark dimension.  No algorithm-specific input
format branches or deterministic array/string serializers are used.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import random
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
DEFAULT_MODEL = "qwen3.5:2b"
DEFAULT_URL = "http://127.0.0.1:11434"
SPEC_SCHEMA_VERSION = 21
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def _chat_json(messages: list[dict], model: str, base_url: str,
               timeout: float, seed: Optional[int] = None,
               num_predict: int = 4096, temperature: float = 0.35) -> dict:
    """Call local Ollama chat in the same low-overhead mode as the test generator."""
    payload = {
        "model": model,
        "messages": messages,
        "think": False,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": int(num_predict),
        },
    }
    if seed is not None:
        payload["options"]["seed"] = int(seed)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {base_url.rstrip('/')}: {exc}. "
            "Start Ollama and ensure the configured model is available."
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Ollama request timed out after {timeout}s.") from exc
    content = body.get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Ollama returned an empty chat response.")
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise RuntimeError("Ollama did not return valid JSON: " + content[:300])
        result = json.loads(match.group(0))
    if not isinstance(result, dict):
        raise RuntimeError("Ollama response must be a JSON object.")
    return result
def _c_code_without_comments(source: str) -> str:
    """Mask C comments while preserving string/character literals and offsets."""
    out = list(source)
    i, state = 0, "code"
    while i < len(source):
        if state == "code":
            if source.startswith("//", i):
                out[i] = out[i + 1] = " "
                i += 2
                state = "line"
            elif source.startswith("/*", i):
                out[i] = out[i + 1] = " "
                i += 2
                state = "block"
            elif source[i] == '"':
                out[i] = " "
                state = "string"
                i += 1
            elif source[i] == "'":
                out[i] = " "
                state = "char"
                i += 1
            else:
                i += 1
        elif state == "line":
            if source[i] == "\n":
                state = "code"
            else:
                out[i] = " "
            i += 1
        elif state == "block":
            if source.startswith("*/", i):
                out[i] = out[i + 1] = " "
                i += 2
                state = "code"
            else:
                if source[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            quote = '"' if state == "string" else "'"
            if source[i] == "\\":
                out[i] = " "
                if i + 1 < len(source) and source[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
            elif source[i] == quote:
                out[i] = " "
                state = "code"
                i += 1
            else:
                if source[i] != "\n":
                    out[i] = " "
                i += 1
    return "".join(out)
def _split_c_arguments(text: str) -> list[str]:
    """Split a C call's arguments without splitting nested expressions."""
    parts, start = [], 0
    paren = bracket = brace = 0
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "(": paren += 1
        elif ch == ")": paren -= 1
        elif ch == "[": bracket += 1
        elif ch == "]": bracket -= 1
        elif ch == "{": brace += 1
        elif ch == "}": brace -= 1
        elif ch == "," and paren == bracket == brace == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts
def _root_variable(expression: str) -> Optional[str]:
    expression = re.sub(r"^\s*&\s*", "", expression)
    match = re.match(r"\s*([A-Za-z_]\w*)", expression)
    return match.group(1) if match else None
def _matching_paren_in_text(text: str, open_pos: int) -> Optional[int]:
    """Find a balanced call close while respecting C string/char literals."""
    depth, quote, i = 0, None, open_pos
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None
def _c_statement_end(code: str, pos: int) -> int:
    """Find the end of a simple C statement, including nested control statements."""
    while pos < len(code) and code[pos].isspace():
        pos += 1
    if pos >= len(code):
        return pos
    if code[pos] == "{":
        depth, i = 0, pos
        while i < len(code):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return len(code)
    keyword = re.match(r"(for|while|if|switch)\b", code[pos:])
    if keyword:
        op = code.find("(", pos + len(keyword.group(1)))
        cp = _matching_paren_in_text(code, op) if op >= 0 else None
        if cp is None:
            semi = code.find(";", pos)
            return semi + 1 if semi >= 0 else len(code)
        end = _c_statement_end(code, cp + 1)
        if keyword.group(1) == "if":
            cursor = end
            while cursor < len(code) and code[cursor].isspace():
                cursor += 1
            if re.match(r"else\b", code[cursor:]):
                end = _c_statement_end(code, cursor + 4)
        return end
    depth = 0
    for i in range(pos, len(code)):
        if code[i] in "([":
            depth += 1
        elif code[i] in ")]":
            depth = max(0, depth - 1)
        elif code[i] == ";" and depth == 0:
            return i + 1
    return len(code)
def _enclosing_for_bound(code: str, position: int) -> Optional[str]:
    """Return the scalar bound of a for-loop containing a stdin read, if any."""
    for match in re.finditer(r"\bfor\s*\(", code):
        op = code.find("(", match.start())
        cp = _matching_paren_in_text(code, op)
        if cp is None:
            continue
        body_start = cp + 1
        while body_start < len(code) and code[body_start].isspace():
            body_start += 1
        if body_start >= len(code):
            continue
        body_end = _c_statement_end(code, body_start)
        if not (body_start <= position < body_end):
            continue
        parts = code[op + 1:cp].split(";")
        if len(parts) != 3:
            continue
        bound = re.search(r"(?:<=|<)\s*([A-Za-z_]\w*)\b", parts[1])
        if bound:
            return bound.group(1)
    return None
def _enclosing_for_bounds(code: str, position: int) -> list[str]:
    """Return all input-variable names used by enclosing for-loop bounds."""
    found = []
    for match in re.finditer(r"\bfor\s*\(", code):
        op = code.find("(", match.start())
        cp = _matching_paren_in_text(code, op)
        if cp is None:
            continue
        body_start = cp + 1
        while body_start < len(code) and code[body_start].isspace():
            body_start += 1
        if body_start >= len(code):
            continue
        body_end = _c_statement_end(code, body_start)
        if not (body_start <= position < body_end):
            continue
        parts = code[op + 1:cp].split(";")
        if len(parts) != 3:
            continue
        rhs = re.search(r"(?:<=|<)\s*(.+)$", parts[1].strip())
        if not rhs:
            continue
        names = re.findall(r"\b[A-Za-z_]\w*\b", rhs.group(1))
        for name in names:
            if name not in {"int", "long", "short", "unsigned", "size_t"}:
                found.append((match.start(), name))
    return list(dict.fromkeys(name for _, name in sorted(found)))
def _enclosing_repeat_count(code: str, position: int) -> str:
    """Translate enclosing loop bounds to the materializer's N/M expression."""
    loops = []
    for match in re.finditer(r"\bfor\s*\(", code):
        op = code.find("(", match.start())
        cp = _matching_paren_in_text(code, op)
        if cp is None:
            continue
        body_start = cp + 1
        while body_start < len(code) and code[body_start].isspace():
            body_start += 1
        if body_start >= len(code) or not (_c_statement_end(code, body_start) > position >= body_start):
            continue
        parts = code[op + 1:cp].split(";")
        if len(parts) == 3:
            rhs = re.search(r"(?:<=|<)\s*(.+)$", parts[1].strip())
            if rhs:
                loops.append(rhs.group(1).strip())
    if len(loops) >= 2:
        return "N*M"
    if not loops:
        return "N"
    expr = loops[0]
    names = re.findall(r"\b[A-Za-z_]\w*\b", expr)
    names = [name for name in names if name not in {"int", "long", "short", "unsigned", "size_t"}]
    if "*" in expr:
        if len(set(names)) >= 2:
            return "N*M"
        if len(names) == 1:
            return "N*N"
        number = re.search(r"\b(\d+)\s*\*", expr)
        return f"{number.group(1)}*N" if number else "N*N"
    return "N"
def _source_main_bounds(source: str, variable: str) -> Optional[int]:
    """Infer an upper input bound from simple early-exit range guards."""
    code = _c_code_without_comments(source)
    limits = []
    for match in re.finditer(r"\bif\s*\(", code):
        op = code.find("(", match.start())
        cp = _matching_paren_in_text(code, op)
        if cp is None:
            continue
        then_start = cp + 1
        while then_start < len(code) and code[then_start].isspace():
            then_start += 1
        if then_start >= len(code):
            continue
        if code[then_start] == "{":
            depth, end = 0, then_start
            while end < len(code):
                if code[end] == "{":
                    depth += 1
                elif code[end] == "}":
                    depth -= 1
                    if depth == 0:
                        end += 1
                        break
                end += 1
        else:
            semi = code.find(";", then_start)
            end = semi + 1 if semi >= 0 else len(code)
        body = code[then_start:end].lstrip()
        if not re.match(r"(?:\{\s*)?(?:return\b|exit\s*\(|_Exit\s*\(|abort\s*\()", body):
            continue
        predicate = code[op + 1:cp]
        for m in re.finditer(
            r"\b" + re.escape(variable) + r"\s*(>|>=)\s*(\d+)\b",
            predicate
        ):
            operator, raw = m.group(1), int(m.group(2))
            limits.append(raw if operator == ">" else raw - 1)
    valid = [x for x in limits if x >= 2]
    return min(valid) if valid else None
def _source_stdin_reads(source: str) -> list[dict]:
    """Extract common direct-stdin destinations, including character-at-a-time reads.
    This is a structural safety net, not the primary input analyst. Ollama still
    describes the complete grammar and semantic constraints.
    """
    code = _c_code_without_comments(source)
    pattern = re.compile(r"\b(scanf|fscanf|fgets|gets|fread|read|fgetc|getchar|getc)\s*\(")
    reads: list[dict] = []
    for match in pattern.finditer(code):
        op = code.find("(", match.start())
        cp = _matching_paren_in_text(code, op)
        if cp is None:
            continue
        args = _split_c_arguments(source[op + 1:cp])
        name = match.group(1)
        destinations: list[tuple[str, Optional[str]]] = []
        if name in {"scanf", "fscanf"}:
            offset = 1 if name == "scanf" else 2
            if len(args) <= offset or (name == "fscanf" and not re.search(r"\bstdin\b", args[0])):
                continue
            fmt_arg = args[0] if name == "scanf" else args[1]
            fm = re.match(r'\s*"((?:\\.|[^"\\])*)"', fmt_arg)
            fmt = fm.group(1) if fm else ""
            conversions = re.findall(
                r"%(?:[-+ #0]*\d*(?:\.\d+)?(?:hh|h|ll|l|L|z|j|t)?)([diuoxXfFeEgGaAcsp%])",
                fmt,
            )
            for i, expr in enumerate(args[offset:]):
                destinations.append((expr, conversions[i] if i < len(conversions) else None))
        elif name in {"fgets", "gets"} and args:
            if name == "gets" or (len(args) >= 3 and re.search(r"\bstdin\b", args[2])):
                destinations = [(args[0], "line")]
        elif name == "fread" and len(args) >= 4 and re.search(r"\bstdin\b", args[3]):
            destinations = [(args[0], "bytes")]
        elif name == "read" and len(args) >= 3 and re.fullmatch(r"\s*(?:0|STDIN_FILENO)\s*", args[0]):
            destinations = [(args[1], "bytes")]
        elif name in {"getchar", "getc", "fgetc"}:
            if name != "getchar" and (not args or not re.search(r"\bstdin\b", args[0])):
                continue
            lhs = re.search(r"([A-Za-z_]\w*(?:\s*\[[^\]]+\])?)\s*=\s*$", code[:match.start()])
            if lhs and "[" in lhs.group(1):
                destinations = [(lhs.group(1), "c")]
        for expr, conversion in destinations:
            root = _root_variable(expr)
            if root:
                reads.append({"variable": root, "expression": expr,
                              "read_function": name, "conversion": conversion,
                              "repeat_count": _enclosing_for_bound(code, match.start()),
                              "repeat_bounds": _enclosing_for_bounds(code, match.start()),
                              "repeat_expression": _enclosing_repeat_count(code, match.start()),
                              "position": len(reads) + 1,
                              "source_offset": match.start()})
    unique: dict[str, dict] = {}
    for item in reads:
        old = unique.get(item["variable"])
        if old is None or ("[" not in old.get("expression", "") and "[" in item.get("expression", "")):
            unique[item["variable"]] = item
    return sorted(unique.values(), key=lambda item: item["source_offset"])
def _source_stdin_variables(source: str) -> set[str]:
    return {item["variable"] for item in _source_stdin_reads(source)}
def _strip_c_initializer(declarator: str) -> str:
    """Remove a declarator initializer at top level, preserving nested '='."""
    paren = bracket = brace = 0
    quote = None
    i = 0
    while i < len(declarator):
        ch = declarator[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(0, brace - 1)
        elif ch == "=" and paren == bracket == brace == 0:
            prev_ch = declarator[i - 1] if i else ""
            next_ch = declarator[i + 1] if i + 1 < len(declarator) else ""
            if prev_ch not in "=!<>" and next_ch != "=":
                return declarator[:i].strip()
        i += 1
    return declarator.strip()
def _find_c_declaration(source: str, variable: str,
                        before: Optional[int] = None) -> Optional[dict]:
    """Find a simple C declaration for *variable*, including shared declarations.
    Handles declarations such as ``int a, b`` and ``char *s, *t``.  The
    declarators are split with the same nesting/quote-aware routine used for
    function arguments, so commas in initializers do not create fake variables.
    This is deliberately conservative: unsupported declarators return None.
    """
    code = _c_code_without_comments(source)
    limit = before if isinstance(before, int) and 0 <= before <= len(code) else len(code)
    type_pattern = re.compile(
        r"\b(?:(?:const|volatile|static|register|auto|extern)\s+)*"
        r"(unsigned\s+long\s+long|long\s+long|unsigned\s+long|"
        r"unsigned\s+int|unsigned\s+short|signed\s+char|unsigned\s+char|"
        r"size_t|long|short|int|char|float|double)\s*([^;{}]+);"
    )
    matches = list(type_pattern.finditer(code[:limit]))
    for match in reversed(matches):
        base_type = re.sub(r"\s+", " ", match.group(1)).strip()
        for declarator in _split_c_arguments(match.group(2)):
            candidate = _strip_c_initializer(declarator)
            dm = re.fullmatch(
                r"\s*(\*+\s*)?" + re.escape(variable) +
                r"\s*((?:\[\s*[^\]]*\s*\])*)\s*",
                candidate,
            )
            if dm:
                dimensions = [
                    value.strip()
                    for value in re.findall(r"\[\s*([^\]]*)\s*\]", dm.group(2))
                ]
                return {
                    "base_type": base_type,
                    "pointer": bool(dm.group(1)),
                    "capacity": dimensions[0] if dimensions else None,
                    "dimensions": dimensions,
                    "declaration_offset": match.start(),
                }
    return None
def _infer_source_field(source: str, read: dict) -> tuple[dict, dict]:
    """Infer a conservative field/plan for a direct stdin destination."""
    variable = read["variable"]
    read_offset = read.get("source_offset")
    decl = _find_c_declaration(source, variable, read_offset)
    if decl is None:
        decl = _find_c_declaration(source, variable)
    if decl is None:
        raise ValueError(
            f"Cannot safely infer stdin field {variable!r} from its C declaration; "
            "the input analysis must describe it explicitly."
        )
    base_type = decl["base_type"]
    pointer, capacity = decl["pointer"], decl["capacity"]
    expr, conv = read.get("expression", variable), read.get("conversion")
    indexed = "[" in expr
    pointer_arithmetic = bool(pointer and re.search(
        r"\b" + re.escape(variable) + r"\s*[+\-]", expr
    ))
    repeated = bool(read.get("repeat_count"))
    is_char = "char" in base_type
    index_count = len(re.findall(r"\[[^\]]+\]", expr))
    repeat_bounds = read.get("repeat_bounds") or (
        [read["repeat_count"]] if read.get("repeat_count") else []
    )
    if (not is_char and index_count >= 2
            and str(read.get("repeat_expression", "")).upper() in {"N*M", "M*N"}):
        shape = "matrix"
    elif conv in {"s", "line", "c"} and is_char:
        shape = "string" if (not indexed or capacity is not None or pointer) else "sequence"
    elif conv == "s" or conv == "line":
        shape = "string" if not indexed else "sequence"
    elif indexed or pointer_arithmetic or repeated:
        shape = "string" if is_char and (not repeated or conv in {"s", "line"}) else "sequence"
    elif is_char and (capacity is not None or pointer):
        shape = "string"
    else:
        shape = "scalar"
    if shape == "scalar":
        count = 1
    elif shape == "matrix":
        count = "N"
    else:
        count = str(
            read.get("repeat_expression") or
            ("N*M" if len(repeat_bounds) >= 2 else "N")
        )
    line_mode = "one_line" if (conv in {"line", "s", "c"} and is_char and not repeated) else "tokens"
    dimensions = decl.get("dimensions") or ([capacity] if capacity is not None else [])
    declared_type = base_type + (" *" if pointer else "")
    for dimension in dimensions:
        declared_type += f"[{dimension}]"
    profile = ("text" if shape == "string" or is_char else
               ("nonnegative" if "unsigned" in base_type.lower() else "small_signed"))
    input_field = {
        "position": int(read.get("position", 1)), "variable": variable,
        "declared_type": declared_type, "read_function": read.get("read_function", "scanf"),
        "description": f"Input destination {variable} read from standard input.",
        "constraints": [],
    }
    plan_field = {
        "variable": variable, "position": int(read.get("position", 1)),
        "shape": shape, "element_type": base_type, "count": count,
        "line_mode": "rows" if shape == "matrix" else line_mode,
        "value_profile": profile,
    }
    if shape == "matrix":
        plan_field["inner_count"] = "N"
    return input_field, plan_field
def _normalize_schema_against_source(spec: dict, source: str) -> None:
    """Reconcile Ollama's schema with visible C declarations and direct reads.
    Ollama remains the primary analyst. Static parsing only corrects facts that
    are visible in the source (declaration/read destination). In particular,
    an input field declared as char[]/char* cannot silently remain a one-char
    scalar merely because a custom reader hid the actual read call.
    """
    analysis = spec.setdefault("analysis", {})
    input_info = analysis.setdefault("input", {})
    generation = analysis.setdefault("input_generation", {})
    reads = _source_stdin_reads(source)
    actual = {item["variable"] for item in reads}
    fields = input_info.get("fields")
    plan = generation.get("fields")
    fields = [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
    plan = [f for f in plan if isinstance(f, dict)] if isinstance(plan, list) else []
    def by_root(items):
        result = {}
        for item in items:
            root = _root_variable(str(item.get("variable", "")))
            if root:
                result.setdefault(root, item)
        return result
    field_map, plan_map = by_root(fields), by_root(plan)
    code = _c_code_without_comments(source)
    def declaration_for(variable: str, before: Optional[int] = None):
        return _find_c_declaration(source, variable, before)
    for read in reads:
        variable = read["variable"]
        inferred_input, inferred_plan = _infer_source_field(source, read)
        if variable not in field_map:
            fields.append(inferred_input)
            field_map[variable] = fields[-1]
        else:
            field_map[variable].update({key: inferred_input[key] for key in
                                        ("position", "variable", "declared_type", "read_function")})
        if variable not in plan_map:
            plan.append(inferred_plan)
            plan_map[variable] = plan[-1]
        else:
            plan_map[variable].update({key: inferred_plan[key] for key in
                                        ("position", "variable", "shape", "element_type", "count", "line_mode")})
        if "char" in str(inferred_plan.get("element_type", "")).lower() or inferred_plan.get("shape") in {"string", "line"}:
            plan_map[variable]["value_profile"] = "text"
    for field in fields:
        variable = _root_variable(str(field.get("variable", "")))
        if not variable:
            continue
        read = next((item for item in reads if item["variable"] == variable), None)
        decl = declaration_for(variable, read.get("source_offset") if read else None)
        if not decl:
            continue
        base_type = decl["base_type"]
        pointer, capacity = decl["pointer"], decl["capacity"]
        if "char" not in base_type or not (pointer or capacity is not None):
            continue
        dimensions = decl.get("dimensions") or ([capacity] if capacity is not None else [])
        declared_type = base_type + (" *" if pointer else "")
        for dimension in dimensions:
            declared_type += f"[{dimension}]"
        field.update({"variable": variable, "declared_type": declared_type})
        if variable not in plan_map:
            inferred = {"variable": variable, "position": int(field.get("position", len(plan) + 1)),
                        "shape": "string", "element_type": base_type, "count": "N",
                        "line_mode": "one_line", "value_profile": "text"}
            plan.append(inferred)
            plan_map[variable] = inferred
        else:
            existing_shape = str(plan_map[variable].get("shape", "scalar")).lower()
            if existing_shape not in {"string_array", "matrix", "sequence2d", "2d"}:
                plan_map[variable].update({"shape": "string", "element_type": base_type,
                                           "count": "N", "line_mode": "one_line"})
            plan_map[variable]["element_type"] = base_type
            plan_map[variable]["value_profile"] = "text"
    main_match = re.search(r"\bmain\s*\([^)]*\)\s*\{", code)
    main_start = main_match.end() if main_match else -1
    main_open = code.find("{", main_match.start()) if main_match else -1
    main_close = None
    if main_open >= 0:
        depth = 0
        for idx in range(main_open, len(code)):
            if code[idx] == "{":
                depth += 1
            elif code[idx] == "}":
                depth -= 1
                if depth == 0:
                    main_close = idx
                    break
    reads_in_main = bool(reads) and main_close is not None and all(
        main_start <= item.get("source_offset", -1) < main_close for item in reads
    )
    if reads_in_main:
        fields = [item for item in fields
                  if _root_variable(str(item.get("variable", ""))) in actual]
        plan = [item for item in plan
                if _root_variable(str(item.get("variable", ""))) in actual]
    order = {item["variable"]: item["position"] for item in reads}
    allowed_profiles = {"small_signed", "nonnegative", "positive", "sorted",
                        "binary", "distinct", "arbitrary", "text", "dimension",
                        "prime", "odd", "pair_sum_no_match", "valid_index"}
    for item in plan:
        candidates = [part.strip().lower() for part in
                      re.split(r"[|,/;]+", str(item.get("value_profile", "small_signed")))]
        valid = [part for part in candidates if part in allowed_profiles]
        if valid:
            if "nonnegative" in valid:
                item["value_profile"] = "nonnegative"
            elif "positive" in valid:
                item["value_profile"] = "positive"
            else:
                item["value_profile"] = valid[0]
    for item in plan:
        variable = str(item.get("variable", ""))
        if not variable or str(item.get("shape", "scalar")).lower() not in {
            "scalar", "number", "integer", "float"
        }:
            continue
        declaration = declaration_for(variable)
        if declaration and "unsigned" in str(declaration.get("base_type", "")).lower():
            item["value_profile"] = "nonnegative"
        elif re.search(r"\b" + re.escape(variable) + r"\s*<\s*0\b", code):
            item["value_profile"] = "nonnegative"
        elif re.search(r"\b" + re.escape(variable) + r"\s*<=\s*0\b", code):
            item["value_profile"] = "positive"
    for collection in (fields, plan):
        for item in collection:
            root = _root_variable(str(item.get("variable", "")))
            if root in order:
                item["position"] = order[root]
        collection.sort(key=lambda item: int(item.get("position", 0)))
    input_info["fields"] = fields
    generation["fields"] = plan
    dimension = analysis.get("profiling_dimension", {})
    parameters = dimension.get("parameters", []) if isinstance(dimension, dict) else []
    primary = _primary_dimension_variable(dimension)
    if parameters:
        first = parameters[0] if isinstance(parameters[0], dict) else {}
        if str(first.get("variable", "")) not in actual:
            scalar_candidates = [
                item for item in reads
                if "[" not in str(item.get("expression", ""))
                and not item.get("repeat_count")
            ]
            if scalar_candidates:
                replacement = scalar_candidates[0]["variable"]
                parameters[0]["variable"] = replacement
                dimension["dimension_name"] = replacement
                dimension["dimension_value_profile"] = "identity"
                dimension["reasoning"] = (
                    "The previous dimension was not read from stdin; "
                    f"using source-verified scalar input {replacement!r}."
                )
    primary = _primary_dimension_variable(dimension)
    repeated_bounds = {
        bound
        for item in reads
        for bound in (item.get("repeat_bounds") or
                      ([item["repeat_count"]] if item.get("repeat_count") else []))
    }
    for item in plan:
        if str(item.get("variable", "")) in repeated_bounds:
            item["value_profile"] = "dimension"
    for item in plan:
        variable = str(item.get("variable", ""))
        if (variable and re.search(r"\b" + re.escape(variable) + r"\s*<\s*0\b", code)
                and re.search(r"\b" + re.escape(variable) + r"\s*>=\s*" +
                              re.escape(primary) + r"\b", code)):
            item["value_profile"] = "valid_index"
    dimension = analysis.get("profiling_dimension", {})
    parameters = dimension.get("parameters", []) if isinstance(dimension, dict) else []
    primary = _primary_dimension_variable(dimension)
    code = _c_code_without_comments(source)
    if primary:
        allocates_by_dimension = bool(re.search(
            r"\b(?:malloc|calloc|realloc)\s*\([^;]{0,160}\b" + re.escape(primary) + r"\b", code))
        primality_pattern = bool(
            re.search(r"%\s*[A-Za-z_]\w*", code) and
            re.search(r"(?:sqrt\s*\(\s*" + re.escape(primary) + r"\s*\)|"
                      r"[A-Za-z_]\w*\s*\*\s*[A-Za-z_]\w*\s*<=\s*" + re.escape(primary) + r"|"
                      r"[A-Za-z_]\w*\s*<=\s*" + re.escape(primary) + r"\s*/\s*[A-Za-z_]\w*)", code))
        if primality_pattern and not allocates_by_dimension:
            dimension["dimension_value_profile"] = "prime"
        else:
            dimension["dimension_value_profile"] = "identity"
    pair_match = re.search(
        r"\b([A-Za-z_]\w*)\s*\[\s*[A-Za-z_]\w*\s*\]\s*\+\s*"
        r"\1\s*\[\s*[A-Za-z_]\w*\s*\]\s*==\s*([A-Za-z_]\w*)", code)
    if pair_match:
        pair_array, pair_target = pair_match.group(1), pair_match.group(2)
    else:
        pair_match = re.search(
            r"\b([A-Za-z_]\w*)\s*==\s*([A-Za-z_]\w*)\s*\[\s*[A-Za-z_]\w*\s*\]\s*\+\s*"
            r"\2\s*\[\s*[A-Za-z_]\w*\s*\]", code)
        pair_array, pair_target = ((pair_match.group(2), pair_match.group(1))
                                   if pair_match else (None, None))
    if pair_array and pair_target:
        for item in plan:
            if str(item.get("variable", "")) == pair_array and str(item.get("shape", "")).lower() in {"sequence", "array", "integer_array"}:
                item["value_profile"] = "distinct"
            elif str(item.get("variable", "")) == pair_target:
                item["value_profile"] = "pair_sum_no_match"
                item.pop("fixed_value", None)
def _schema_roots(fields: list[dict]) -> set[str]:
    roots = set()
    for field in fields:
        variable = str(field.get("variable", ""))
        root = _root_variable(variable)
        if root:
            roots.add(root)
    return roots
def _primary_dimension_variable(dimension: dict) -> str:
    """Resolve the scalar stdin variable represented by the requested N."""
    if not isinstance(dimension, dict):
        return ""
    parameters = dimension.get("parameters", [])
    names = [str(p.get("variable", "")) for p in parameters if isinstance(p, dict)]
    dimension_name = str(dimension.get("dimension_name", "")).strip()
    if dimension_name in names:
        return dimension_name
    for parameter in parameters:
        if isinstance(parameter, dict):
            rule = str(parameter.get("scaling_rule", "")).lower()
            if any(term in rule for term in ("primary", "controls n", "determines n")):
                name = str(parameter.get("variable", ""))
                if name:
                    return name
    return names[0] if names else ""
def _validate(spec: dict, source: Optional[str] = None) -> None:
    if not isinstance(spec, dict):
        raise ValueError("Input specification must be a JSON object.")
    analysis = spec.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("Missing analysis object in input specification.")
    fields = analysis.get("input", {}).get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("Ollama could not identify the program's stdin fields.")
    dimension = analysis.get("profiling_dimension")
    if not isinstance(dimension, dict) or not dimension.get("description"):
        raise ValueError("Ollama did not identify a profiling dimension.")
    plan = analysis.get("input_generation", {}).get("fields")
    if not isinstance(plan, list) or not plan:
        raise ValueError("Ollama did not describe the stdin generation plan.")
    for field in plan:
        if not isinstance(field, dict) or not field.get("variable"):
            raise ValueError("Each input-generation field must identify its input variable.")
    input_roots = _schema_roots(fields)
    plan_roots = _schema_roots(plan)
    if source:
        source_roots = _source_stdin_variables(source)
        missing_analysis = source_roots - input_roots
        missing_plan = source_roots - plan_roots
    else:
        omitted_by_plan = input_roots - plan_roots
        if omitted_by_plan:
            raise ValueError(
                "Input-generation schema is incomplete: input fields are missing from the "
                f"materialization plan: {', '.join(sorted(omitted_by_plan))}."
            )
        missing_analysis = set()
        missing_plan = set()
    if source:
        if missing_analysis or missing_plan:
            details = []
            if missing_analysis:
                details.append("input analysis omitted " + ", ".join(sorted(missing_analysis)))
            if missing_plan:
                details.append("input-generation plan omitted " + ", ".join(sorted(missing_plan)))
            raise ValueError(
                "Input schema does not cover all statically visible stdin destinations: "
                + "; ".join(details) + ". Refusing to profile incomplete inputs."
            )
def analyze_source(source_path: str, model: str = DEFAULT_MODEL,
                   base_url: str = DEFAULT_URL, timeout: float = 120,
                   sample_input: Optional[str] = None,
                   problem_description: Optional[str] = None,
                   oracle_source: Optional[str] = None) -> dict:
    """Understand the input grammar and scaling dimension from full source context."""
    path = os.path.abspath(os.path.expanduser(os.fspath(source_path)))
    student_source = Path(path).read_text(encoding="utf-8", errors="replace")
    reference_source = oracle_source or student_source
    sample_section = (
        "\nProvided sample stdin (follow its exact token/line layout):\n"
        "```text\n" + sample_input + "\n```\n"
        if sample_input else ""
    )
    description_section = (
        "\nProblem statement / constraints:\n" + problem_description + "\n"
        if problem_description else ""
    )
    prompt = f'''Study this complete C program to understand its stdin format and how
its input size can be scaled for runtime-complexity benchmarking. Do not write
any concrete test input in this analysis response.
Return JSON with exactly this general structure:
{{
  "analysis": {{
    "input": {{"description":"...", "fields":[{{"position":1,"variable":"...","declared_type":"...","read_function":"scanf/fgets/getchar/...","description":"...","constraints":[]}}], "input_relationships":[], "constraints":[]}},
    "output": {{"description":"...", "printf_formats":[], "fields":[]}},
    "profiling_dimension": {{"description":"...", "dimension_name":"...", "parameters":[{{"variable":"...","meaning":"...","scaling_rule":"..."}}], "dimension_value_profile":"identity|prime|odd", "confidence":"high|medium|low", "reasoning":"..."}},
    "input_generation": {{"fields":[{{"variable":"...","position":1,"shape":"scalar|sequence|matrix|string|string_array|line","element_type":"int|long long|float|double|char|token","count":"1|N|N*N|fixed integer|N+1", "inner_count":"fixed integer|N|omit (only when shape does not require an inner dimension)", "line_mode":"tokens|one_line|one_per_item|rows", "value_profile":"small_signed|nonnegative|positive|sorted|binary|distinct|arbitrary|text", "fixed_value":null}}], "constraints":[], "validity_conditions":[], "serialization":"..."}}
  }},
  "max_safe_dimension": null,
  "analysis_method": "LLM inspection of complete C source"
}}
Important:
- Trace every stdin read in the exact order it is consumed, including reads in
  loops, helper functions, branches, and mixed scanf/fgets/getchar code.
- Distinguish values read from stdin from local variables. Never list local
  algorithm variables as input fields.
- Report C declared types accurately. A numeric array such as `int *a` filled by
  `scanf("%d", &a[i])` is an integer array, NOT a string. Reserve string/string-array
  for character data read with `%s`, scansets, fgets, or equivalent character input.
  Use declared C type names (for example `int`, `int[]`, `int[][]`, `char[]`) in
  each input field's `declared_type`.
- Identify the primary dimension N and explain exactly which input tokens or
  collection lengths must equal N (or scale with N).
- For a scalar primary dimension, choose dimension_value_profile=prime when
  prime values exercise the full/worst-case loop (for example trial-division
  primality testing); choose identity for ordinary size parameters and odd only
  when an odd scalar is needed. This profile affects the actual scalar value,
  while the requested benchmark dimension remains the fitting x-coordinate.
- Identify independent scalar parameters separately; do not assume all scalars
  are bounded by or equal to N unless the code/problem constraints establish it.
- Identify semantic preconditions needed for valid executions, such as sorted
  data, an existing target, nonempty strings, ranges, or matrix dimensions.
- Respect declared array and string-buffer capacities. Set max_safe_dimension
  only when a real bound is visible; otherwise use null.
- Do not assume that every input is whitespace-separated. Preserve line-based
  input semantics where relevant.
- The input_generation fields are an executable structural schema for a generic
  local materializer. Do not generate concrete test data.
- Use shape=scalar for one token, sequence for a 1-D collection, matrix for a
  rectangular 2-D collection, string for one character sequence, string_array
  for multiple strings, and line for a line-oriented input record.
- For scalar dimension variables, set value_profile=dimension. For arrays whose
  length scales with the primary dimension, use count=N. For square matrices,
  use shape=matrix, count=N, inner_count=N, line_mode=rows.
- Use line_mode=tokens when scanf-style whitespace tokenization is valid;
  one_per_item when each item must occupy its own line; rows for matrices.
- Every field must include variable, position, shape, element_type, count,
  line_mode, and value_profile. Use an integer for fixed counts, or an expression
  using only N, M, +, -, *, / (for example N, N+1, N*N). For a string, count
  is its character length. For a string_array, count is number of strings and
  inner_count is each string length; never use omit for string_array.
- For matrix, inner_count is the column count; never use omit for matrix.
  Use omit only for shapes that do not require an inner dimension.
- Use fixed_value only when the program requires a specific literal input value.
  Use value_profile=dimension only for the variable that actually controls N.
- Describe each stdin read exactly once in input order, including scalar
  dimensions before the arrays they size.
{description_section}{sample_section}
C PROGRAM (use this as the input-format authority):
```c
{reference_source}
```
END PROGRAM'''
    response = _chat_json(
        [{"role": "system", "content": "You are an expert C-program input-format analyst. Be exact and conservative."},
         {"role": "user", "content": prompt}],
        model=model, base_url=base_url, timeout=timeout,
        seed=17, num_predict=5000, temperature=0.1,
    )
    spec = response
    _normalize_schema_against_source(spec, student_source)
    _validate(spec, student_source)
    spec["schema_version"] = SPEC_SCHEMA_VERSION
    spec["source_file"] = os.path.basename(path)
    spec["student_source_sha256"] = sha256_file(path)
    spec["max_safe_dimension"] = response.get("max_safe_dimension")
    spec["generation_context"] = {
        "source": reference_source,
        "sample_input": sample_input or "",
        "problem_description": problem_description or "",
        "model": model,
        "base_url": base_url,
    }
    return spec
def _effective_inner_count(field: dict, shape: str, n: int) -> int:
    """Resolve optional inner_count values using shape-aware safe defaults.
    The schema prompt permits the literal "omit" for inner_count when a shape
    does not need a second dimension. Some model responses nevertheless emit
    it for matrix/string_array fields. Preserve the intended defaults instead
    of passing the sentinel to the arithmetic expression evaluator.
    """
    raw = field.get("inner_count", None)
    if isinstance(raw, str) and raw.strip().lower() in {
        "omit", "omitted", "none", "null", ""
    }:
        if shape in {"matrix", "sequence2d", "2d"}:
            return int(n)
        if shape == "string_array":
            return 8
        raise ValueError(
            f"Input field {field.get('variable', '<unnamed>')!r} has "
            "inner_count marked omitted, but this shape requires an inner count."
        )
    if raw is None:
        if shape in {"matrix", "sequence2d", "2d"}:
            raw = "N"
        elif shape == "string_array":
            raw = 8
        else:
            raise ValueError(
                f"Input field {field.get('variable', '<unnamed>')!r} "
                "is missing its required inner_count."
            )
    return _safe_count(raw, n)
def _safe_count(expression: Any, n: int, inner_n: Optional[int] = None) -> int:
    """Evaluate a deliberately tiny integer expression language for schema lengths."""
    import ast
    if isinstance(expression, int):
        value = expression
    elif isinstance(expression, str):
        expr = expression.strip().upper().replace("PRIMARY_N", "N")
        if expr.lower() in {"omit", "omitted", "none", "null", ""}:
            expr = "N"
        tree = ast.parse(expr, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
                   ast.FloorDiv, ast.Div, ast.Constant, ast.Name, ast.Load,
                   ast.UnaryOp, ast.USub, ast.UAdd)
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                raise ValueError(f"Unsupported count expression: {expression!r}")
            if isinstance(node, ast.Name) and node.id not in {"N", "M"}:
                raise ValueError(f"Unknown symbol in count expression: {node.id}")
        value = int(eval(compile(tree, "<input-schema-count>", "eval"),
                         {"__builtins__": {}}, {"N": n, "M": inner_n or n}))
    else:
        raise ValueError(f"Invalid count expression: {expression!r}")
    if value < 0 or value > 8_000_000:
        raise ValueError(f"Input schema requested unsafe element count: {value}")
    return value
def _value_for(element_type: str, profile: str, index: int, rng: random.Random,
               fixed_value: Any = None) -> str:
    if fixed_value is not None:
        return str(fixed_value)
    t = str(element_type or "int").lower()
    p = str(profile or "small_signed").lower()
    if any(x in t for x in ("float", "double")):
        if p == "positive":
            return f"{rng.uniform(0.5, 20.0):.3f}"
        if p == "nonnegative":
            return f"{rng.uniform(0.0, 20.0):.3f}"
        return f"{rng.uniform(-20.0, 20.0):.3f}"
    if "char" in t or p == "text":
        alphabet = "abcdefghijklmnopqrstuvwxyz"
        return alphabet[index % len(alphabet)]
    if p == "binary":
        return str(index % 2)
    if p == "positive":
        return str(1 + (index % 97))
    if p == "nonnegative":
        return str(index % 100)
    if p == "distinct":
        return str(index + 1)
    if p == "sorted":
        return str(index - 10)
    return str(((index * 17 + rng.randint(0, 7)) % 41) - 20)
def _largest_prime_at_most(value: int) -> int:
    """Return a prime near the requested scalar dimension, without external tools."""
    candidate = max(2, int(value))
    if candidate > 2 and candidate % 2 == 0:
        candidate -= 1
    while candidate >= 2:
        limit = math.isqrt(candidate)
        if all(candidate % divisor for divisor in range(2, limit + 1)):
            return candidate
        candidate -= 2 if candidate > 3 else 1
    return 2
def materialize_stdin(spec: dict, n: int, seed: int = 17) -> str:
    """Expand an LLM-authored structural schema into raw stdin without asking the LLM for data."""
    _validate(spec)
    fields = spec.get("analysis", {}).get("input_generation", {}).get("fields", [])
    rng = random.Random((int(seed) << 32) ^ int(n))
    scalar_rng = random.Random(int(seed))
    output_lines: list[str] = []
    tokens: list[str] = []
    def flush_tokens():
        if tokens:
            output_lines.append(" ".join(tokens))
            tokens.clear()
    for field in sorted(fields, key=lambda x: int(x.get("position", 0))):
        shape = str(field.get("shape", field.get("generator", {}).get("kind", "scalar"))).lower()
        etype = str(field.get("element_type", field.get("declared_type", "int")))
        profile = str(field.get("value_profile", field.get("generator", {}).get("role", "small_signed")))
        line_mode = str(field.get("line_mode", "tokens")).lower()
        fixed = field.get("fixed_value")
        if shape in {"scalar", "number", "integer", "float"}:
            dimension_parameters = spec.get("analysis", {}).get("profiling_dimension", {}).get("parameters", [])
            dimension_info = spec.get("analysis", {}).get("profiling_dimension", {})
            primary_dimension_variable = _primary_dimension_variable(dimension_info)
            if str(field.get("variable", "")) == primary_dimension_variable or profile == "dimension":
                dimension_profile = str(spec.get("analysis", {}).get("profiling_dimension", {}).get(
                    "dimension_value_profile", "identity")).lower()
                if dimension_profile == "prime":
                    scalar = str(_largest_prime_at_most(n))
                elif dimension_profile == "odd":
                    scalar = str(n if n % 2 else max(1, n - 1))
                else:
                    scalar = str(n)
            elif profile == "pair_sum_no_match":
                scalar = str(2 * int(n))
            else:
                scalar = _value_for(
                    etype, profile, int(field.get("position", 0)),
                    scalar_rng, fixed
                )
            values = [scalar]
        elif shape in {"sequence", "array", "integer_array", "float_array", "string_array"}:
            count = _safe_count(field.get("count", "N"), n)
            if shape == "string_array" or "string_array" in shape:
                width = _effective_inner_count(field, shape, n)
                values = ["".join(_value_for("char", "text", i + j, rng) for j in range(width)) for i in range(count)]
            elif profile == "valid_index":
                offset = int(field.get("position", 0)) % 2
                values = [str((i + offset) % max(1, int(n))) for i in range(count)]
            else:
                values = [_value_for(etype, profile, i, rng, fixed) for i in range(count)]
        elif shape in {"matrix", "sequence2d", "2d"}:
            rows = _safe_count(field.get("count", "N"), n)
            cols = _effective_inner_count(field, shape, n)
            values = [_value_for(etype, profile, i, rng, fixed) for i in range(rows * cols)]
            if line_mode == "rows":
                flush_tokens()
                for r in range(rows):
                    output_lines.append(" ".join(values[r * cols:(r + 1) * cols]))
                continue
        elif shape in {"string", "line", "char_line"}:
            count = _safe_count(field.get("count", "N"), n)
            alphabet = "abcdefghijklmnopqrstuvwxyz"
            value = "".join(alphabet[i % len(alphabet)] for i in range(count))
            flush_tokens()
            output_lines.append(value)
            continue
        else:
            raise ValueError(f"Unsupported input schema shape {shape!r} for {field.get('variable')!r}.")
        if line_mode == "one_per_item":
            flush_tokens()
            output_lines.extend(values)
        elif line_mode == "one_line" or line_mode == "rows":
            flush_tokens()
            output_lines.append(" ".join(values))
        else:
            tokens.extend(values)
    flush_tokens()
    raw = "\n".join(output_lines).rstrip("\n") + "\n"
    if len(raw.encode("utf-8")) > 8_000_000:
        raise ValueError("Materialized stdin exceeds the 8 MB safety limit.")
    return raw
def validate_materialized_batch(spec: dict, raw_inputs: dict[int, str]) -> dict:
    """Verify that generated payloads really vary along declared N dimensions.
    Byte length alone is insufficient for scalar dimensions: values such as
    16, 32 and 64 all occupy two characters. This audit therefore also reads
    the primary scalar dimension token from each generated payload.
    """
    if not raw_inputs:
        raise ValueError("Input materializer produced an empty benchmark batch.")
    analysis = spec.get("analysis", {})
    fields = analysis.get("input_generation", {}).get("fields", [])
    dimension = analysis.get("profiling_dimension", {})
    parameters = dimension.get("parameters", []) if isinstance(dimension, dict) else []
    primary = _primary_dimension_variable(dimension)
    def depends_on_n(expression):
        if isinstance(expression, int):
            return False
        return bool(re.search(r"\b(?:N|PRIMARY_N)\b", str(expression), re.IGNORECASE))
    def count_for(expr, n, inner=None):
        return _safe_count(expr, n, inner)
    scaling, scalar_values = [], {}
    ordered = sorted(fields, key=lambda f: int(f.get("position", 0)))
    for n, raw in sorted(raw_inputs.items()):
        tokens = raw.split()
        cursor = 0
        for field in ordered:
            shape = str(field.get("shape", "scalar")).lower()
            variable = str(field.get("variable", "<unnamed>"))
            profile = str(field.get("value_profile", "")).lower()
            if shape in {"scalar", "number", "integer", "float"}:
                if variable == primary or profile == "dimension":
                    if cursor < len(tokens):
                        scalar_values.setdefault(variable, {})[int(n)] = tokens[cursor]
                cursor += 1
            elif shape in {"sequence", "array", "integer_array", "float_array", "string_array"}:
                count = count_for(field.get("count", "N"), int(n))
                if depends_on_n(field.get("count", "N")):
                    scaling.append(variable)
                if shape == "string_array":
                    width_expr = field.get("inner_count", 8)
                    if isinstance(width_expr, str) and width_expr.strip().lower() in {"omit", "omitted", "none", "null", ""}:
                        width_expr = 8
                    if depends_on_n(width_expr):
                        scaling.append(variable + "[width]")
                    cursor += count
                else:
                    cursor += count
            elif shape in {"matrix", "sequence2d", "2d"}:
                rows = count_for(field.get("count", "N"), int(n))
                cols_expr = field.get("inner_count", "N")
                if isinstance(cols_expr, str) and cols_expr.strip().lower() in {"omit", "omitted", "none", "null", ""}:
                    cols_expr = "N"
                cols = _effective_inner_count(field, shape, int(n))
                if depends_on_n(field.get("count", "N")) or depends_on_n(cols_expr):
                    scaling.append(variable)
                cursor += rows * cols
            elif shape in {"string", "line", "char_line"}:
                if depends_on_n(field.get("count", "N")):
                    scaling.append(variable)
                cursor += 1
    lengths = {int(n): len(raw.encode("utf-8")) for n, raw in raw_inputs.items()}
    scalar_dimension_values = {}
    for variable, values in scalar_values.items():
        scalar_dimension_values[variable] = values
        if len(values) >= 2 and len(set(values.values())) == 1:
            raise ValueError(
                f"Primary scalar profiling dimension {variable!r} has the same generated value "
                f"for every requested N: {values}. Refusing to profile an input that does not scale."
            )
    if scaling and len(lengths) >= 2 and len(set(lengths.values())) == 1:
        raise ValueError(
            "Generated stdin has the same byte length for every requested N, "
            "although the schema declares N-dependent input fields "
            f"({', '.join(sorted(set(scaling)))}). Refusing to profile a non-scaling input. "
            f"stdin byte lengths: {lengths}. Check the input schema/materializer."
        )
    if not scaling and not scalar_values:
        warning = "No explicit N-dependent array/string dimension or primary scalar dimension was found."
    else:
        warning = None
    return {
        "method": "schema_materializer",
        "dimensions": sorted(int(n) for n in raw_inputs),
        "stdin_bytes": lengths,
        "scaling_fields": sorted(set(scaling)),
        "scalar_dimension_values": scalar_dimension_values,
        "distinct_payload_lengths": len(set(lengths.values())),
        "warning": warning,
    }
def generate_stdin_batch(spec: dict, sizes: list[int], seed: int = 17,
                         feedback: Optional[str] = None,
                         timeout: float = 120) -> dict[int, str]:
    """Materialize each requested stdin locally from the analyzed structural schema.
    `feedback` and `timeout` remain accepted for API compatibility; generation no
    longer sends benchmark payloads to Ollama.
    """
    _validate(spec)
    requested = sorted({int(value) for value in sizes})
    if not requested or any(value < 1 for value in requested):
        raise ValueError("Benchmark dimensions must be a nonempty list of positive integers.")
    generated = {n: materialize_stdin(spec, n, seed=seed) for n in requested}
    validate_materialized_batch(spec, generated)
    return generated
def generate_stdin(spec: dict, n: int, seed: int = 17,
                   feedback: Optional[str] = None,
                   timeout: float = 120) -> str:
    """Backward-compatible single-dimension stdin materializer."""
    return materialize_stdin(spec, int(n), seed=seed)
def maximum_safe_dimension(spec: dict, source: Optional[str] = None) -> Optional[int]:
    """Return the tightest declared or source-visible scalar input upper bound."""
    candidates = []
    try:
        value = int(spec.get("max_safe_dimension"))
        if value > 0:
            candidates.append(value)
    except (TypeError, ValueError):
        pass
    dimension = spec.get("analysis", {}).get("profiling_dimension", {})
    primary = _primary_dimension_variable(dimension)
    if source and primary:
        inferred = _source_main_bounds(source, primary)
        if inferred is not None:
            candidates.append(inferred)
    if source:
        for field in spec.get("analysis", {}).get("input_generation", {}).get("fields", []):
            if not isinstance(field, dict):
                continue
            shape = str(field.get("shape", "")).lower()
            count = field.get("count", 1)
            if shape not in {"string", "line", "char_line", "sequence", "array",
                             "integer_array", "float_array", "matrix", "sequence2d", "2d"}:
                continue
            if isinstance(count, int) or not re.search(r"\bN\b", str(count), re.IGNORECASE):
                continue
            variable = _root_variable(str(field.get("variable", "")))
            declaration = _find_c_declaration(source, variable) if variable else None
            if declaration and declaration.get("capacity"):
                try:
                    capacity = int(declaration["capacity"])
                    if shape in {"string", "line", "char_line"}:
                        capacity -= 1
                    expression = str(count).upper().replace(" ", "")
                    if expression in {"N*N", "N*M", "M*N"}:
                        capacity = math.isqrt(capacity)
                    else:
                        factor = re.fullmatch(r"(\d+)\*N", expression)
                        if factor:
                            capacity //= max(1, int(factor.group(1)))
                    if capacity > 0:
                        candidates.append(capacity)
                except (TypeError, ValueError):
                    pass
    return min(candidates) if candidates else None
def _static_source_spec(source_path: str, source: str, fallback_reason: str,
                        sample_input: Optional[str] = None,
                        problem_description: Optional[str] = None,
                        oracle_source: Optional[str] = None,
                        model: str = DEFAULT_MODEL,
                        base_url: str = DEFAULT_URL) -> Optional[dict]:
    """Build a conservative input schema when Ollama is unavailable.
    Supports common direct stdin reads in main. It deliberately refuses helper
    readers and ambiguous source layouts rather than inventing an input grammar.
    """
    code = _c_code_without_comments(source)
    reads = _source_stdin_reads(source)
    if not reads:
        return None
    main_match = re.search(r"\bmain\s*\([^)]*\)\s*\{", code)
    if not main_match:
        return None
    main_open = code.find("{", main_match.start())
    depth, main_close = 0, None
    for idx in range(main_open, len(code)):
        if code[idx] == "{":
            depth += 1
        elif code[idx] == "}":
            depth -= 1
            if depth == 0:
                main_close = idx
                break
    if main_close is None or not all(
        main_match.end() <= item.get("source_offset", -1) < main_close for item in reads
    ):
        return None
    input_fields, plan = [], []
    try:
        for read in reads:
            field, generation = _infer_source_field(source, read)
            input_fields.append(field)
            plan.append(generation)
    except (ValueError, TypeError, KeyError):
        return None
    actual = {item["variable"] for item in reads}
    repeated_bounds = {
        name for item in reads for name in (
            item.get("repeat_bounds") or
            ([item["repeat_count"]] if item.get("repeat_count") else [])
        )
    }
    for item in plan:
        if item["variable"] in repeated_bounds:
            item["value_profile"] = "dimension"
    scalar_names = [
        item["variable"] for item in plan
        if item.get("shape") == "scalar" and item["variable"] in actual
    ]
    def control_score(name: str) -> int:
        score = len(re.findall(r"\b" + re.escape(name) + r"\b", code))
        score += 4 * len(re.findall(
            r"\b(?:malloc|calloc|realloc)\s*\([^;]*\b" + re.escape(name) + r"\b",
            code
        ))
        return score
    primary = max(scalar_names, key=control_score) if scalar_names else plan[0]["variable"]
    parameters = []
    for item in plan:
        if item.get("shape") == "scalar":
            name = item["variable"]
            if name == primary or name in repeated_bounds:
                rule = "Primary scaling factor" if name == primary else "Scales with N in repeated input loop"
            else:
                rule = "Independent scalar parameter"
            parameters.append({"variable": name, "meaning": "Source-inferred scalar input.",
                               "scaling_rule": rule})
    if not parameters:
        parameters = [{"variable": primary, "meaning": "Source-inferred variable-length input.",
                        "scaling_rule": "Primary scaling factor"}]
    spec = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "source_file": os.path.basename(source_path),
        "student_source_sha256": sha256_file(source_path),
        "analysis_method": "Conservative static source fallback (direct stdin reads in main)",
        "fallback_warning": (
            "Ollama input analysis failed; this schema was inferred from direct standard-input "
            "reads and simple C declarations. Complex helper readers or semantic constraints "
            "may require a manually supplied input specification. Original error: "
            + str(fallback_reason)[:1000]
        ),
        "analysis": {
            "input": {
                "description": "Direct standard-input destinations inferred from C source.",
                "fields": input_fields,
                "input_relationships": [],
                "constraints": [],
            },
            "output": {"description": "Output inferred from program source.", "printf_formats": [], "fields": []},
            "profiling_dimension": {
                "description": f"Source-inferred primary input dimension {primary}.",
                "dimension_name": primary,
                "parameters": parameters,
                "dimension_value_profile": "identity",
                "confidence": "medium",
                "reasoning": "Static fallback selected a scalar input that controls loops/allocations, or the first variable-length input.",
            },
            "input_generation": {
                "fields": plan,
                "constraints": [],
                "validity_conditions": [],
                "serialization": "C stdin token/line order inferred from direct reads.",
            },
        },
        "max_safe_dimension": None,
        "generation_context": {
            "source": oracle_source or source,
            "sample_input": sample_input or "",
            "problem_description": problem_description or "",
            "model": model,
            "base_url": base_url,
        },
    }
    _normalize_schema_against_source(spec, source)
    _validate(spec, source)
    spec["max_safe_dimension"] = maximum_safe_dimension(spec, source)
    return spec
def load_or_analyze(source_path: str, spec_dir: Optional[str] = None,
                    model: str = DEFAULT_MODEL, base_url: str = DEFAULT_URL,
                    timeout: float = 120, force: bool = False,
                    sample_input: Optional[str] = None,
                    problem_description: Optional[str] = None,
                    oracle_source: Optional[str] = None):
    """Load a source-matched analysis cache or ask Ollama to create one."""
    src = Path(os.path.abspath(os.path.expanduser(os.fspath(source_path))))
    directory = Path(spec_dir) if spec_dir else src.parent / ".cgrade_specs"
    directory.mkdir(parents=True, exist_ok=True)
    cache_path = directory / (re.sub(r"[^A-Za-z0-9_.-]+", "_", src.stem) + ".spec.json")
    digest = sha256_file(str(src))
    context_key = hashlib.sha256(json.dumps({
        "sample_input": sample_input or "",
        "problem_description": problem_description or "",
        "oracle_source": oracle_source or "",
        "model": model,
        "base_url": base_url,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (cached.get("student_source_sha256") == digest
                    and cached.get("context_sha256") == context_key
                    and cached.get("schema_version") == SPEC_SCHEMA_VERSION
                    and cached.get("generation_context")):
                cached_source = src.read_text(encoding="utf-8", errors="replace")
                _normalize_schema_against_source(cached, cached_source)
                _validate(cached, cached_source)
                return cached, str(cache_path)
        except Exception:
            pass
    try:
        spec = analyze_source(
            str(src), model=model, base_url=base_url, timeout=timeout,
            sample_input=sample_input,
            problem_description=problem_description,
            oracle_source=oracle_source,
        )
    except Exception as exc:
        spec = _static_source_spec(
            str(src), src.read_text(encoding="utf-8", errors="replace"),
            fallback_reason=str(exc), sample_input=sample_input,
            problem_description=problem_description, oracle_source=oracle_source,
            model=model, base_url=base_url,
        )
        if spec is None:
            raise
    spec["context_sha256"] = context_key
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, cache_path)
    return spec, str(cache_path)
