#!/usr/bin/env python3
"""Universal C complexity estimator with an inferred post-input measurement boundary.

Primary measurement: Valgrind Callgrind instruction counts, when available.
Fallback: Clang SanitizerCoverage basic-block execution events. The fallback is a
runtime-growth proxy, NOT a machine-instruction count.

Public API: evaluate(source_path, binary_path=None, params=None, limits=None)
No algorithm names or algorithm-specific rules are used by this module.
"""
from __future__ import annotations
import math, os, random, re, shutil, statistics, subprocess, tempfile, time
from typing import Optional

INPUT_CALLS = ("scanf", "fscanf", "fgets", "fgetc", "getchar", "getc", "fread", "read")

# ---------- Small C-source scanner (lexical, not a complete C parser) ----------
def _mask_comments_strings(src: str) -> str:
    out=list(src); i=0; state='code'
    while i<len(src):
        if state=='code':
            if src.startswith('//',i): out[i]=out[i+1]=' '; i+=2; state='line'; continue
            if src.startswith('/*',i): out[i]=out[i+1]=' '; i+=2; state='block'; continue
            if src[i]=='"': out[i]=' '; i+=1; state='str'; continue
            if src[i]=="'": out[i]=' '; i+=1; state='char'; continue
            i+=1
        elif state=='line':
            if src[i]=='\n': state='code'
            else: out[i]=' '
            i+=1
        elif state=='block':
            if src.startswith('*/',i): out[i]=out[i+1]=' '; i+=2; state='code'
            else:
                if src[i]!='\n': out[i]=' '
                i+=1
        else:
            q='"' if state=='str' else "'"
            if src[i]=='\\':
                out[i]=' '; i+=1
                if i<len(src): out[i]=' '; i+=1
            elif src[i]==q: out[i]=' '; i+=1; state='code'
            else:
                if src[i]!='\n': out[i]=' '
                i+=1
    return ''.join(out)

def _match(src: str, pos: int, op: str, cl: str) -> Optional[int]:
    depth=0
    for i in range(pos,len(src)):
        if src[i]==op: depth+=1
        elif src[i]==cl:
            depth-=1
            if depth==0: return i
    return None

def _statement_end(masked: str, pos: int) -> int:
    while pos<len(masked) and masked[pos].isspace(): pos+=1
    if pos>=len(masked): return pos
    if masked[pos]=='{':
        e=_match(masked,pos,'{','}')
        return e+1 if e is not None else len(masked)
    kw=re.match(r'(for|while|if|switch)\b',masked[pos:])
    if kw:
        op=masked.find('(',pos+len(kw.group(1)))
        cp=_match(masked,op,'(',')') if op>=0 else None
        if cp is None:
            e=masked.find(';',pos); return e+1 if e>=0 else len(masked)
        e=_statement_end(masked,cp+1)
        if kw.group(1)=='if':
            q=e
            while q<len(masked) and masked[q].isspace(): q+=1
            if masked.startswith('else',q): e=_statement_end(masked,q+4)
        return e
    depth=0
    for i in range(pos,len(masked)):
        if masked[i] in '([': depth+=1
        elif masked[i] in ')]': depth=max(0,depth-1)
        elif masked[i]==';' and depth==0: return i+1
    return len(masked)

def _input_call(masked: str):
    pat=re.compile(r'\b('+'|'.join(INPUT_CALLS)+r')\s*\(')
    found=list(pat.finditer(masked))
    if not found: return None
    m=found[-1]; op=masked.find('(',m.start()); cp=_match(masked,op,'(',')')
    return (m.start(),cp+1) if cp is not None else None

def infer_measurement_boundary(source: str) -> dict:
    """Return a conservative source offset after the last input operation.

    If non-input computation is interleaved in the enclosing input loop, counting
    starts before that loop so that computation is not thrown away.
    """
    masked=_mask_comments_strings(source)
    call=_input_call(masked)
    if call is None:
        main_match = re.search(r'\bmain\s*\([^)]*\)\s*\{', masked)
        if main_match:
            return {
                "supported": True,
                "offset": main_match.end(),
                "mode": "main_entry_no_stdin",
                "reason": "No stdin read was found; count begins at main entry for a fixed-input program.",
            }
        return {"supported":False,"reason":"No supported standard input call or main() function was found."}
    call_start,call_end=call
    loops=[]
    for m in re.finditer(r'\b(for|while)\s*\(',masked):
        op=masked.find('(',m.start()); cp=_match(masked,op,'(',')')
        if cp is None: continue
        end=_statement_end(masked,cp+1)
        if m.start()<call_start<end:
            loops.append((m.start(),end,cp+1,end-1))
    if loops:
        outer=min(loops,key=lambda x:x[0])
        body=masked[outer[2]:outer[3]]
        # Remove complete if-statements whose condition performs an input read,
        # e.g. if (scanf(...) != 1) return 1;. Leaving the trailing condition
        # text behind falsely makes a pure input loop look like computation.
        remove_spans=[]
        for if_match in re.finditer(r'\bif\s*\(',body):
            op=body.find('(',if_match.start())
            cp=_match(body,op,'(',')')
            if cp is None:
                continue
            condition=body[op+1:cp]
            if re.search(r'\b('+'|'.join(INPUT_CALLS)+r')\s*\(',condition):
                remove_spans.append((if_match.start(),_statement_end(body,cp+1)))
        for start,end in reversed(remove_spans):
            body=body[:start]+' '*(end-start)+body[end:]
        # Remove standalone input-call statements after the if cleanup.
        for input_match in list(re.finditer(r'\b('+'|'.join(INPUT_CALLS)+r')\s*\(',body))[::-1]:
            op=body.find('(',input_match.start())
            cp=_match(body,op,'(',')')
            if cp is not None:
                semi=body.find(';',cp)
                if semi >= 0:
                    body=body[:input_match.start()]+' '*(semi+1-input_match.start())+body[semi+1:]
        body=re.sub(r'\b(for|while)\s*\([^)]*\)','',body,flags=re.S)
        meaningful=re.sub(r'[{}\s;]','',body)
        if meaningful:
            return {"supported":True,"offset":outer[0],"mode":"mixed_input_computation",
                    "reason":"Computation is interleaved with input; count begins before the enclosing input loop."}
        return {"supported":True,"offset":outer[1],"mode":"after_input_loop",
                "reason":"Count begins after the enclosing loop that performs the final input reads."}
    return {"supported":True,"offset":_statement_end(masked,call_end),"mode":"after_input_statement",
            "reason":"Count begins after the final input statement."}

_RUNTIME = r"""
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#if defined(CGRADE_USE_CALLGRIND) && defined(__has_include)
# if __has_include(<valgrind/callgrind.h>)
#  include <valgrind/callgrind.h>
#  define CGRADE_CG_START() CALLGRIND_ZERO_STATS
# else
#  define CGRADE_CG_START() ((void)0)
# endif
#else
# define CGRADE_CG_START() ((void)0)
#endif
static volatile unsigned long long cgrade_events=0;
static volatile int cgrade_enabled=0;
#if defined(__GNUC__)
__attribute__((no_instrument_function)) void __cyg_profile_func_enter(void *this_fn, void *call_site) {
    (void)this_fn; (void)call_site;
    if(cgrade_enabled) cgrade_events++;
}
__attribute__((no_instrument_function)) void __cyg_profile_func_exit(void *this_fn, void *call_site) {
    (void)this_fn; (void)call_site;
}
#endif
__attribute__((no_sanitize("coverage"))) void __sanitizer_cov_trace_pc(void) { if(cgrade_enabled) cgrade_events++; }
__attribute__((no_sanitize("coverage"))) void __cgrade_start(void) { cgrade_enabled=1; cgrade_events++; CGRADE_CG_START(); }
__attribute__((no_sanitize("coverage"))) size_t __wrap_strlen(const char *s) { size_t n=0; while(s[n]!='\0') n++; if(cgrade_enabled) cgrade_events+=(unsigned long long)n+1; return n; }
__attribute__((no_sanitize("coverage"))) static void cgrade_report(void) { cgrade_enabled=0; fprintf(stderr,"CGRADE_EVENTS=%llu\\n",cgrade_events); }
__attribute__((constructor,no_sanitize("coverage"))) static void cgrade_init(void) { atexit(cgrade_report); }
"""

def instrument_source(source: str):
    boundary=infer_measurement_boundary(source)
    if not boundary.get('supported'): raise ValueError(boundary.get('reason','Unsupported input boundary'))
    pos=boundary['offset']
    rewritten='void __cgrade_start(void);\n'+source[:pos]+'\n__cgrade_start();\n'+source[pos:]+_RUNTIME
    return rewritten,boundary

def compile_instrumented(source_path: str, output_path: str, compiler: str='clang', callgrind_header: bool=False):
    """Compile with Clang coverage when available, otherwise GCC function hooks.

    GCC fallback is a function-entry event proxy, not a basic-block or instruction
    counter. The selected mode is returned in the boundary metadata.
    """
    src=open(source_path,encoding='utf-8',errors='ignore').read()
    rewritten,boundary=instrument_source(src)
    tmp=output_path+'.instrumented.c'
    with open(tmp,'w') as f: f.write(rewritten)

    requested = compiler or 'clang'
    selected = shutil.which(requested)
    if selected and os.path.basename(selected).startswith('clang'):
        if callgrind_header:
            # Callgrind counts machine instructions itself. Do not add a second
            # SanitizerCoverage counter to this build.
            mode='callgrind_instructions'
            cmd=[selected,'-O0','-g','-fno-builtin','-DCGRADE_USE_CALLGRIND=1',
                 tmp,'-Wl,--wrap=strlen','-o',output_path]
        else:
            mode='clang_sanitizer_coverage'
            cmd=[selected,'-O0','-g','-fno-builtin','-fsanitize-coverage=trace-pc',
                 tmp,'-Wl,--wrap=strlen','-o',output_path]
    else:
        gcc = shutil.which('gcc') or shutil.which('cc')
        if not gcc:
            raise FileNotFoundError(f"Neither Clang nor GCC is available (requested compiler: {requested}). Install gcc or clang.")
        if callgrind_header:
            mode='callgrind_instructions'
            cmd=[gcc,'-O0','-g','-fno-builtin','-DCGRADE_USE_CALLGRIND=1',
                 tmp,'-Wl,--wrap=strlen','-o',output_path]
        else:
            mode='gcc_function_entry_proxy'
            cmd=[gcc,'-O0','-g','-fno-builtin','-finstrument-functions',
                 tmp,'-Wl,--wrap=strlen','-o',output_path]
    run=subprocess.run(cmd,capture_output=True,text=True)
    if run.returncode:
        raise RuntimeError(f"Compilation failed using {os.path.basename(cmd[0])}: {run.stderr[-4000:]}")
    boundary['compiler']=os.path.basename(cmd[0])
    boundary['counter_mode']=mode
    return boundary

def _parse_callgrind(path):
    if not os.path.exists(path): return None
    with open(path,errors='ignore') as f:
        for line in f:
            if line.startswith('summary:'):
                try: return int(line.split(':',1)[1].strip())
                except ValueError: return None
    return None

def profile_one(binary: str, data: str, timeout: float=5.0,
                instruction_counts_only: bool=True):
    """Measure one run; require Callgrind instruction counts by default.

    Set instruction_counts_only=False only when a clearly labelled runtime
    proxy is acceptable for the experiment.
    """
    valgrind=shutil.which('valgrind')
    header=os.path.exists('/usr/include/valgrind/callgrind.h')
    if valgrind and header:
        with tempfile.TemporaryDirectory(prefix='cgrade-cg-') as td:
            out=os.path.join(td,'callgrind.out')
            cmd=[valgrind,'--tool=callgrind','--quiet','--instr-atstart=yes',f'--callgrind-out-file={out}',binary]
            p=subprocess.run(cmd,input=data,text=True,capture_output=True,timeout=timeout)
            count=_parse_callgrind(out)
            # Reject empty/failed instruction profiles rather than silently
            # changing measurement units halfway through the experiment.
            if p.returncode==0 and count is not None and count > 0:
                return {'cost':count,'method':'callgrind_instructions','stderr':p.stderr[-500:]}
            callgrind_error = (
                f'Callgrind did not produce a valid instruction count '
                f'(exit={p.returncode}, summary={count}). stderr: {p.stderr[-500:]}'
            )
    else:
        callgrind_error = (
            'Callgrind instruction counting is unavailable: install Valgrind and '
            'the Valgrind development headers in the Linux/WSL environment.'
        )
    if instruction_counts_only:
        raise RuntimeError(callgrind_error)
    p=subprocess.run([binary],input=data,text=True,capture_output=True,timeout=timeout)
    m=re.search(r'CGRADE_EVENTS=(\d+)',p.stderr)
    if p.returncode!=0 or not m:
        raise RuntimeError(f'Program failed ({p.returncode}) or emitted no profiling counter: {p.stderr[-500:]}')
    events=int(m.group(1))
    if events <= 0:
        raise RuntimeError('Profiling produced zero runtime events; refusing to classify complexity from an empty measurement.')
    return {'cost':events,'method':'sanitizer_coverage_basic_block_events','stderr':p.stderr[-500:]}

# The generalized statistical fitter is kept separate from C instrumentation.
# Relative import is used by C-Grade Engine; fallback supports direct file tests.
try:
    from .complexity_fitting import fit_growth
except ImportError:
    from complexity_fitting import fit_growth


# Minimal generic stdin generator; callers may provide input_builder for unusual grammars/preconditions.


# ---------- Self-contained input-contract inference (embedded from V3) ----------
def strip_comments(source: str) -> str:
    pattern = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|//[^\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: m.group(1) if m.group(1) else "", source)

def split_c_args(text: str) -> list[str]:
    parts, start, depth, quote, escaped = [], 0, 0, None, False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts

def calls(source: str, name: str) -> list[list[str]]:
    """Extract simple C calls while respecting nested parentheses and strings."""
    found = []
    pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    for match in pattern.finditer(source):
        i, depth, quote, escaped = match.end(), 1, None, False
        start = i
        while i < len(source) and depth:
            ch = source[i]
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth == 0:
            found.append(split_c_args(source[start:i-1]))
    return found

def scanf_conversions(fmt: str) -> list[str]:
    # scanf conversion specifiers, excluding %% literals and %* assignments.
    rx = re.compile(r"%(?:\*)?(?:\d+)?(?:hh|h|ll|l|j|z|t|L)?([diuoxXfFeEgGaAcsp\[])")
    return [m.group(1) for m in rx.finditer(fmt) if not fmt[max(0, m.start()-1):m.start()] == "%"]

def declared_arrays(source: str) -> dict[str, int]:
    result = {}
    # Parse array declarators after a primitive type, including comma-separated
    # declarations such as: int n, values[100], matrix[20][20];
    for declaration in re.finditer(
        r"\b(?:char|short|int|long|float|double)\s+([^;]+);", source
    ):
        body = declaration.group(1)
        for item in re.finditer(r"\b(\w+)\s*\[\s*(\d+)\s*\]", body):
            result[item.group(1)] = int(item.group(2))
    return result


def loop_bound_for(source: str, index: str) -> Optional[str]:
    """Find the variable bound of a loop using *index* as its induction variable."""
    for m in re.finditer(r"for\s*\(([^;]*);([^;]*);([^)]*)\)", source, re.S):
        if re.search(r"\b" + re.escape(index) + r"\b", m.group(2)):
            bound = re.search(r"<\s*(\w+)", m.group(2))
            if bound:
                return bound.group(1)
    return None

def infer_input_contract(source: str) -> dict:
    """Infer common scalar, 1-D, 2-D, string and line-oriented stdin shapes.

    This is intentionally syntax-driven, not algorithm-name-driven. It supports
    common scanf-in-loop patterns and keeps dimensions explicit.
    """
    source = strip_comments(source)
    fields = []
    arrays = declared_arrays(source)
    two_dim = {}
    fixed_2d_capacity = {}
    for declaration in re.finditer(
        r"\b(?:char|short|int|long|float|double)\s+([^;]+);", source
    ):
        body = declaration.group(1)
        for m in re.finditer(
            r"\b(\w+)\s*\[\s*(\w+|\d+)\s*\]\s*\[\s*(\w+|\d+)\s*\]", body
        ):
            two_dim[m.group(1)] = (m.group(2), m.group(3))
            if m.group(2).isdigit() and m.group(3).isdigit():
                fixed_2d_capacity[m.group(1)] = (int(m.group(2)), int(m.group(3)))

    for call in calls(source, "scanf"):
        if not call or not call[0].startswith('"'):
            continue
        fmt = call[0][1:-1]
        args = call[1:]
        conversions = scanf_conversions(fmt)
        arg_index = 0
        for conv in conversions:
            if arg_index >= len(args):
                break
            arg = args[arg_index]
            arg_index += 1
            clean = arg.replace("&", "").strip()
            # Match scalar, a[i], or a[i][j].
            vm = re.search(r"([A-Za-z_]\w*)\s*(?:\[\s*(\w+)\s*\])?\s*(?:\[\s*(\w+)\s*\])?\s*$", clean)
            variable = vm.group(1) if vm else None
            idx1 = vm.group(2) if vm else None
            idx2 = vm.group(3) if vm else None
            if idx1 and idx2:
                row_var = loop_bound_for(source, idx1)
                col_var = loop_bound_for(source, idx2)
                if row_var and col_var:
                    fields.append({"kind": "repeated2d", "type": conv,
                                   "row_var": row_var, "col_var": col_var,
                                   "variable": variable,
                                   "row_capacity": fixed_2d_capacity.get(variable or "", (10000, 10000))[0],
                                   "col_capacity": fixed_2d_capacity.get(variable or "", (10000, 10000))[1]})
                    continue
            if idx1:
                count_var = loop_bound_for(source, idx1)
                if count_var:
                    if conv in ("s", "["):
                        token_capacity = fixed_2d_capacity.get(variable or "", (10000, 10000))[1]
                        fields.append({"kind": "string_array", "type": conv,
                                       "count_var": count_var, "variable": variable,
                                       "capacity": arrays.get(variable or "", 10000),
                                       "token_capacity": token_capacity})
                    else:
                        fields.append({"kind": "repeated", "type": conv,
                                       "count_var": count_var, "variable": variable,
                                       "capacity": arrays.get(variable or "", 10000)})
                    continue
            if conv in ("s", "["):
                fields.append({"kind": "string", "type": conv, "variable": variable,
                               "capacity": arrays.get(variable or "", 10000)})
            elif conv == "c":
                fields.append({"kind": "char", "type": conv, "variable": variable})
            else:
                fields.append({"kind": "number", "type": conv, "variable": variable})

    for call in calls(source, "fgets"):
        if len(call) >= 2:
            var = re.search(r"\b([A-Za-z_]\w*)\b", call[0])
            cap = re.search(r"\b(\d+)\b", call[1])
            fields.append({"kind": "line", "variable": var.group(1) if var else None,
                           "capacity": int(cap.group(1)) if cap else 10000})

    if not fields:
        return {"supported": False, "reason": "No supported scanf/fgets input contract was found.", "fields": []}

    # Identify scalar variables that control loops or dimensions. This helps
    # generate scalable inputs without assuming a particular algorithm.
    loop_bounds = set()
    for m in re.finditer(r"for\s*\(([^;]*);([^;]*);([^)]*)\)", source, re.S):
        b = re.search(r"<\s*(\w+)", m.group(2))
        if b:
            loop_bounds.add(b.group(1))
    return {"supported": True, "fields": fields, "arrays": arrays,
            "two_dimensional_arrays": two_dim, "fixed_2d_capacity": fixed_2d_capacity,
            "loop_bound_variables": sorted(loop_bounds)}

def _numeric_value(conv: str, value: int) -> str:
    if conv in ("f", "F", "e", "E", "g", "G", "a", "A"):
        return str(float(value))
    if conv == "c":
        return "x"
    return str(value)

def make_input(contract: dict, n: int, seed: int = 17) -> str:
    """Generate size-scaled input from the inferred read contract.

    A repeated scanf inside a loop consumes values; it does NOT consume another
    count token. Scalar dimensions are emitted exactly once, in source read order.
    """
    fields = contract["fields"]
    loop_bounds = set(contract.get("loop_bound_variables", []))
    # Repeated fields refer to a previously read loop-bound scalar.
    repeated_counts = {f.get("count_var") for f in fields if f["kind"] in ("repeated", "string_array")}
    repeated_rows = {f.get("row_var") for f in fields if f["kind"] == "repeated2d"}
    repeated_cols = {f.get("col_var") for f in fields if f["kind"] == "repeated2d"}
    dim_vars = repeated_rows | repeated_cols
    output = []
    scalar_seen = 0

    for field in fields:
        kind = field["kind"]
        if kind == "number":
            var = field.get("variable")
            if var in repeated_counts or var in dim_vars or var in loop_bounds:
                value = n
            else:
                # Independent scalar parameters are scaled too, but kept distinct
                # so two-argument routines do not accidentally receive (x, x).
                scalar_seen += 1
                value = n + scalar_seen + 1
            output.append(_numeric_value(field.get("type", "d"), value))
        elif kind == "char":
            output.append("x")
        elif kind == "string":
            cap = max(2, int(field.get("capacity", 10000)) - 1)
            output.append("a" * min(n, cap))
        elif kind == "line":
            cap = max(1, int(field.get("capacity", 10000)) - 2)
            output.append("x" * min(n, cap))
        elif kind == "repeated":
            count = min(n, max(1, int(field.get("capacity", 10000))))
            # Ascending values satisfy common sorted-array preconditions
            # without identifying the algorithm using the array.
            output.extend(str(i * 2 + 1) for i in range(count))
        elif kind == "string_array":
            count = min(n, max(1, int(field.get("capacity", 10000))))
            token_capacity = max(2, int(field.get("token_capacity", 10000)))
            word_len = max(1, min(16, n, token_capacity - 1))
            output.extend(("x" * word_len) for _ in range(count))
        elif kind == "repeated2d":
            rows = min(n, max(1, int(field.get("row_capacity", 10000))))
            cols = min(n, max(1, int(field.get("col_capacity", 10000))))
            output.extend(str((i * cols + j) % 97) for i in range(rows) for j in range(cols))
    return "\n".join(output) + "\n"

def validate_input_size(contract: dict, n: int):
    """Reject sizes that exceed statically visible fixed input storage."""
    for field in contract.get("fields", []):
        kind = field.get("kind")
        if kind in ("repeated", "string_array") and n > int(field.get("capacity", 10000)):
            return False, f"n={n} exceeds fixed array capacity {field.get('capacity')}."
        if kind == "string_array" and n > int(field.get("token_capacity", 10000)) - 1:
            return False, f"n={n} exceeds string-array token capacity {int(field.get('token_capacity', 10000)) - 1}."
        if kind == "repeated2d":
            rows = int(field.get("row_capacity", 10000))
            cols = int(field.get("col_capacity", 10000))
            if n > rows or n > cols:
                return False, f"n={n} exceeds fixed matrix capacity {rows}x{cols}."
        if kind in ("string", "line"):
            cap = int(field.get("capacity", 10000))
            usable = max(0, cap - (1 if kind == "string" else 2))
            if n > usable:
                return False, f"n={n} exceeds input buffer capacity {usable}."
    return True, None


def _legacy_scaled_input(input_type: str, n: int, seed: int = 17):
    """Compatibility fallback for the input types exposed by the existing UI."""
    rng = random.Random(seed + int(n))
    if input_type == "string":
        return "".join(chr(rng.randint(97, 122)) for _ in range(n)) + "\n"
    if input_type == "integer_array":
        values = [str(rng.randint(1, 100)) for _ in range(n)]
        return f"{n}\n" + " ".join(values) + "\n"
    if input_type == "raw_array":
        return " ".join(str(rng.randint(1, 100)) for _ in range(n)) + "\n"
    if input_type == "single_number":
        return f"{n}\n"
    if input_type == "fixed_arithmetic":
        if "log" in expected_complexity.lower():
            # For logarithmic arithmetic (GCD, LCM, modular operations, binary pow),
            # consecutive Fibonacci numbers (F_{k+1}, F_k) force logarithmic step counts (Lame's Theorem).
            fib_pairs = {
                13: (21, 13),
                55: (89, 55),
                233: (377, 233),
                987: (1597, 987),
                4181: (6765, 4181),
                17711: (28657, 17711),
                75025: (121393, 75025),
                317811: (514229, 317811),
            }
            if n in fib_pairs:
                a, b = fib_pairs[n]
                return f"{a} {b}\n"
            phi = 1.6180339887
            companion = max(1, int(round(n * phi)))
            return f"{companion} {n}\n"

        return f"{n} {n + 7}\n"
    raise ValueError("No supported input contract was inferred and no compatible input_type was configured.")


def _select_input_sizes(params: dict):
    if params.get("input_sizes"):
        return [int(value) for value in params["input_sizes"]]
    if params.get("trials") and params.get("base_n") is not None and params.get("scale_step") is not None:
        trials = max(2, int(params["trials"]))
        base = int(params["base_n"])
        step = max(1, int(params["scale_step"]))
        return [base + i * step for i in range(trials)]
    return [4, 8, 16, 32, 64, 128, 256]


def _default_inputs(source: str, n: int):
    """Generate an input using the input-contract logic embedded in this file.

    V4 is deliberately self-contained: C-Grade Engine must not need V3 on
    sys.path or in the working directory merely to generate benchmark inputs.
    """
    contract = infer_input_contract(source)
    if not contract.get("supported"):
        raise ValueError(contract.get("reason", "Could not infer a supported input format."))
    return make_input(contract, n)

def _compatibility_error(message: str) -> dict:
    """Return the result shape expected by the existing C-Grade Engine."""
    return {
        'score': 0.0,
        'evidence': {'error': message, 'detected_complexity': 'inconclusive'},
        'feedback': f'Evaluation failed: {message}'
    }


def _complexity_rank(label: str):
    """Compare predefined complexity candidates by asymptotic growth."""

    value = (label or "").strip().lower().replace(" ", "")
    value = value.replace("n", "N").replace("N", "n")

    ranks = {
        "o(1)": (0, 0),

        "o(logn)": (1, 0),

        "o(n)": (2, 1),
        "o(n^2)": (2, 2),
        "o(n^3)": (2, 3),

        "o(nlogn)": (3, 1),
        "o(n^2logn)": (3, 2),

        "o(1.5^n)": (4, 1.5),
        "o(2^n)": (4, 2),
        "o(3^n)": (4, 3),

        "o(n^1*1.5^n)": (5, 1.5),
        "o(n^2*1.5^n)": (5, 1.5),
        "o(n^1*2^n)": (5, 2),
        "o(n^2*2^n)": (5, 2),
        "o(n^1*3^n)": (5, 3),
        "o(n^2*3^n)": (5, 3),

        "o(n!)": (6, 1),
        "o(n^1*n!)": (7, 1),
        "o(n^2*n!)": (7, 2),
    }

    return ranks.get(value)


def evaluate(source_path: str, binary_path: str, params: dict, limits: dict = None) -> dict:
    """C-Grade-compatible entry point; keeps the legacy API and result schema.

    `binary_path` is accepted for compatibility with the existing analyzer
    interface. V4 builds a separate instrumented copy of `source_path` because
    the supplied executable is not instrumented at the measurement boundary.
    """
    params = params or {}
    limits = limits or {}

    if not source_path:
        return _compatibility_error('Student source file not found.')
    resolved_source = os.path.abspath(os.path.expanduser(os.fspath(source_path)))
    if not os.path.isfile(resolved_source):
        return _compatibility_error(f'Student source file not found: {source_path}')

    try:
        with open(resolved_source, encoding='utf-8', errors='ignore') as handle:
            source = handle.read()
    except OSError as exc:
        return _compatibility_error(f'Could not read student source: {exc}')

    try:
        contract = infer_input_contract(source)
        fields = contract.get("fields", [])
        kinds = sorted({field.get("kind", "unknown") for field in fields})
        input_kind = ", ".join(kinds) if kinds else "custom_or_unknown"
    except Exception:
        input_kind = 'custom_or_unknown'

    try:
        result = _evaluate_universal(resolved_source, params, limits)
    except Exception as exc:
        return _compatibility_error(f'Complexity profiling failed: {type(exc).__name__}: {exc}')

    fit = result.get('fit', {'complexity': 'inconclusive', 'confidence': 'low'})
    detected = fit.get('complexity', 'inconclusive')
    expected = params.get('expected_complexity', 'O(N)')
    detected_rank = _complexity_rank(detected)
    expected_rank = _complexity_rank(expected)

    generic_family = fit.get('generic_family', 'Unclassified')

    if detected_rank is None or expected_rank is None or detected == 'inconclusive':
        score = 0.5
        feedback = (
            f'Estimated {detected} ({generic_family}); comparison with expected '
            f'{expected} is inconclusive.'
        )
    elif detected_rank <= expected_rank:
        score = 1.0
        feedback = (
            f'Estimated {detected} ({generic_family}), which is consistent with '
            f'or better than expected {expected}.'
        )
    else:
        score = 0.4
        feedback = (
            f'Estimated {detected} ({generic_family}), which is worse than '
            f'expected {expected}.'
        )

    methods = result.get('profiling_methods', [])
    mode = ', '.join(methods) if methods else 'unavailable'
    return {
        'score': score,
        'evidence': {
            'input_type': input_kind,
            'profiling_method': mode,
            'counter': result.get('measurement_note', 'Runtime execution events'),
            'expected_complexity': expected,
            'detected_complexity': detected,
            'generic_complexity_family': generic_family,
            'fit': fit,
            'trials': result.get('measurements', []),
            'input_sizes': result.get('input_sizes', _select_input_sizes(params)),
            'validation': result.get('validation', []),
            'measurement_boundary': result.get('boundary'),
            'profiling_diagnostics': result.get('validation', []),
            'binary_path_used': False,
            'adaptive_mode': result.get('adaptive_mode', False),
            'adaptive_stopping_reason': result.get('adaptive_stopping_reason')
        },
        'feedback': feedback
    }


def _adaptive_benchmark_sizes(params: dict):
    """Choose adaptive sizing by default, including when legacy sizes exist.

    Older C-Grade configurations commonly pass ``input_sizes`` containing a
    generic schedule such as [10, 30, 50, 70, 90]. Treating that list as a
    mandatory fixed schedule makes exponential programs time out before enough
    measurements are collected. Adaptive sizing is therefore the default.

    Set ``adaptive_input_sizes=False`` to explicitly request the legacy fixed
    schedule. ``input_sizes`` remains available to fixed-mode callers.
    """
    adaptive = bool(params.get("adaptive_input_sizes", True))
    if adaptive:
        return True, []
    return False, _select_input_sizes(params)


def _next_adaptive_size(last_n: int, last_cost: int, previous_cost: int,
                        previous_n: int, elapsed: float, params: dict,
                        successful_measurements: int = 0) -> int:
    """Choose the next input size without stopping on a slow first trial.

    Wall-clock time includes process startup and instrumentation overhead. It
    is used only to reduce the next step, never to stop before the required
    number of successful observations has been collected.
    """
    min_step = max(1, int(params.get('adaptive_min_step', 1)))
    max_step = max(min_step, int(params.get('adaptive_max_step', 12)))
    target = float(params.get('adaptive_trial_target_s', 0.35))

    ratio = None
    if previous_cost and previous_n is not None and last_n > previous_n:
        ratio = last_cost / max(1, previous_cost)

    # Slow trial or sharp counter growth: increase by the smallest safe step.
    if elapsed >= target or (ratio is not None and ratio >= 4.0):
        step = min_step
    else:
        # For inexpensive workloads, grow the step gradually to reach useful
        # sizes without requiring many tiny benchmark runs.
        step = min(max_step, max(min_step, int(round(last_n * 0.30))))

    return last_n + step


def _measurements_nearly_flat(measurements, params: dict) -> bool:
    """Return True when the initial size sweep shows too little counter growth.

    The test is intentionally relative to the measured total, because Callgrind
    counts include fixed runtime overhead even though counting starts after
    input. It only triggers a wider sweep; it never selects a complexity.
    """
    rows = [row for row in measurements if row.get("cost", 0) > 0]
    minimum = max(5, int(params.get("adaptive_flat_min_measurements", 8)))
    if len(rows) < minimum:
        return False
    ns = [int(row["n"]) for row in rows]
    costs = [float(row["cost"]) for row in rows]
    if min(ns) <= 0 or max(ns) < min(ns) * float(params.get("adaptive_flat_min_size_ratio", 4.0)):
        return False
    median_cost = statistics.median(costs)
    if median_cost <= 0:
        return False
    relative_span = (max(costs) - min(costs)) / median_cost
    return relative_span <= float(params.get("adaptive_flat_relative_span", 0.01))


def _evaluate_universal(source_path: str, params: dict, limits: dict):
    """Internal V4 implementation with adaptive benchmark-size selection.

    Input inference, generation, instrumentation and profiling remain the same.
    Only the choice and stopping of benchmark sizes is adaptive. Explicit
    input_sizes preserve the old fixed-size behaviour unless adaptive sizing
    is explicitly requested.
    """
    source_path = os.path.abspath(os.path.expanduser(os.fspath(source_path)))
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f'Source file does not exist: {source_path}')
    with open(source_path, encoding='utf-8', errors='ignore') as handle:
        source = handle.read()

    adaptive, fixed_sizes = _adaptive_benchmark_sizes(params)
    timeout = float(limits.get('time_limit_s', 5.0))
    min_measurements = max(5, int(params.get('min_successful_measurements', 5)))
    # Keep the original small-size pass, but reserve additional runs for a
    # second, wider pass when fixed profiling overhead hides the growth signal.
    max_measurements = max(min_measurements, int(params.get('max_measurements', 16)))
    initial_measurements = max(5, int(params.get('adaptive_initial_measurements', 10)))
    initial_measurements = min(initial_measurements, max_measurements)
    start_n = max(1, int(params.get('adaptive_start_n', 2)))
    max_n = max(start_n, int(params.get('adaptive_max_n', 131072)))
    contract = infer_input_contract(source)

    with tempfile.TemporaryDirectory(prefix='cgrade-universal-') as td:
        exe = os.path.join(td, 'profiled_program')
        use_callgrind = bool(shutil.which('valgrind') and os.path.exists('/usr/include/valgrind/callgrind.h'))
        instruction_counts_only = bool(params.get('instruction_counts_only', True))
        if instruction_counts_only and not use_callgrind:
            raise RuntimeError(
                'Instruction-count profiling requires Valgrind Callgrind and its development headers. '
                'Install them in WSL/Linux, or explicitly set instruction_counts_only=False to allow '
                'the labelled SanitizerCoverage/GCC proxy fallback.'
            )
        boundary = compile_instrumented(source_path, exe, params.get('compiler', 'clang'), callgrind_header=use_callgrind)
        if use_callgrind:
            boundary['counter_mode'] = 'callgrind_instructions'
        measurements = []
        validation = []
        builder = params.get('input_builder')
        attempted_sizes = []
        next_n = start_n
        previous_n = None
        previous_cost = None
        stop_reason = None
        expansion_mode = False

        while len(attempted_sizes) < (max_measurements if adaptive else len(fixed_sizes)):
            n = next_n if adaptive else fixed_sizes[len(attempted_sizes)]
            if n > max_n and adaptive:
                stop_reason = f'adaptive maximum input size ({max_n}) reached'
                break
            attempted_sizes.append(n)
            started = time.monotonic()
            try:
                if not builder and contract.get("supported"):
                    size_ok, size_reason = validate_input_size(contract, int(n))
                    if not size_ok:
                        validation.append({'n': n, 'valid': False, 'reason': size_reason})
                        if adaptive:
                            stop_reason = size_reason
                            break
                        continue
                if builder:
                    data = builder(n)
                elif contract.get("supported"):
                    data = make_input(contract, n)
                else:
                    data = _legacy_scaled_input(params.get("input_type", ""), n, int(params.get("seed", 17)))
                measured = profile_one(
                    exe, data, timeout,
                    instruction_counts_only=instruction_counts_only
                )
                elapsed = time.monotonic() - started
                if boundary.get('counter_mode') == 'gcc_function_entry_proxy' and measured['method'] != 'callgrind_instructions':
                    measured['method'] = 'gcc_function_entry_events'
                measurements.append({'n': n, 'cost': measured['cost'], 'method': measured['method'],
                                     'elapsed_s': round(elapsed, 6)})
                validation.append({'n': n, 'valid': True, 'elapsed_s': round(elapsed, 6)})
                if adaptive:
                    previous_n, previous_cost = n, measured['cost']

                    # A nearly flat counter series can mean that fixed process /
                    # instrumentation overhead is hiding the algorithmic work.
                    # Do not keep adding nearby sizes: deliberately widen the
                    # range geometrically and collect more observations.
                    if (not expansion_mode and
                            len(measurements) >= initial_measurements and
                            _measurements_nearly_flat(measurements, params)):
                        expansion_mode = True
                        next_n = max(n + 1, int(math.ceil(n * float(
                            params.get('adaptive_expansion_factor', 4.0)))))
                    elif expansion_mode:
                        factor = max(1.25, float(params.get('adaptive_expansion_factor', 4.0)))
                        next_n = max(n + 1, int(math.ceil(n * factor)))
                    else:
                        next_n = _next_adaptive_size(
                            n, measured['cost'],
                            measurements[-2]['cost'] if len(measurements) > 1 else None,
                            measurements[-2]['n'] if len(measurements) > 1 else None,
                            elapsed, params, len(measurements)
                        )

                    # Defensive guard: adaptive schedules must always advance.
                    if next_n <= n:
                        next_n = n + max(1, int(params.get('adaptive_min_step', 1)))
            except subprocess.TimeoutExpired:
                validation.append({'n': n, 'valid': False, 'reason': 'timeout'})
                if adaptive:
                    # Do not keep probing larger inputs after a timeout. If
                    # fewer than the required measurements exist, back off
                    # once to the midpoint between the last success and the
                    # failed candidate, then continue cautiously.
                    if len(measurements) < min_measurements and previous_n is not None and n - previous_n > 1:
                        next_n = previous_n + max(1, (n - previous_n) // 2)
                        if next_n < n:
                            continue
                    stop_reason = 'stopped increasing input size after a timeout'
                    break
            except Exception as exc:
                validation.append({'n': n, 'valid': False, 'reason': str(exc)})
                if adaptive:
                    stop_reason = f'input generation or profiling failed: {exc}'
                    break

        actual_sizes = attempted_sizes
        if len(measurements) < min_measurements:
            result = {'status': 'inconclusive', 'boundary': boundary, 'validation': validation,
                      'measurements': measurements, 'input_sizes': actual_sizes,
                      'fit': {'complexity': 'inconclusive', 'confidence': 'low',
                              'reason': f'Only {len(measurements)} successful measurements; at least {min_measurements} are required.'},
                      'feedback': f'Only {len(measurements)} successful measurements; at least {min_measurements} are required.'}
            if adaptive:
                result['adaptive_stopping_reason'] = stop_reason or ('maximum measurement count reached after flat-series expansion' if expansion_mode else 'maximum measurement count reached')
            return result

        fit = fit_growth(measurements, params.get('fit_config', {}))
        methods = sorted(set(item['method'] for item in measurements))
        boundary['measurement_method'] = ', '.join(methods) if methods else 'unavailable'
        result = {'status': 'measured', 'boundary': boundary, 'validation': validation,
                  'measurements': measurements, 'input_sizes': actual_sizes,
                  'profiling_methods': methods, 'fit': fit,
                  'measurement_note': ('Callgrind instruction counts.' if methods and all(m == 'callgrind_instructions' for m in methods) else 'Clang SanitizerCoverage results are basic-block events; GCC fallback results are function-entry events. These are runtime-growth proxies, not machine-instruction counts.')}
        if adaptive:
            result['adaptive_stopping_reason'] = stop_reason or ('maximum measurement count reached after flat-series expansion' if expansion_mode else 'maximum measurement count reached')
            result['adaptive_mode'] = True
        return result
