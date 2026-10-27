import os
import re
import hashlib

def _strip_comments_and_whitespace(code_string):
    """Literal normalization: only removes comments and whitespace."""
    code = re.sub(r'//.*|/\*[\s\S]*?\*/', '', code_string)
    return re.sub(r'\s+', '', code)

def _normalize_structural(code_string):
    """Structural normalization: removes comments, whitespace, and generalizes identifiers."""
    code = re.sub(r'//.*|/\*[\s\S]*?\*/', '', code_string)
    code = re.sub(r'".*?"', 'STR', code)
    code = re.sub(r'\b[a-zA-Z_]\w*\b', 'ID', code)
    return re.sub(r'\s+', '', code)

def _get_fingerprints(text, k=10, window_size=5):
    if len(text) < k:
        return set()
    kgrams = [text[i:i+k] for i in range(len(text) - k + 1)]
    hashes = [int(hashlib.md5(g.encode('utf-8')).hexdigest()[:8], 16) for g in kgrams]
    
    fingerprints = set()
    if len(hashes) < window_size:
        fingerprints.update(hashes)
    else:
        for i in range(len(hashes) - window_size + 1):
            fingerprints.add(min(hashes[i:i+window_size]))
    return fingerprints

def evaluate(source_path, binary_path, params, limits):
    corpus_dir = params.get('corpus_dir', 'submissions/')
    threshold = params.get('flag_threshold', 0.85)
    
    try:
        with open(source_path, 'r', encoding='utf-8') as f:
            raw_code = f.read()
            target_structural = _normalize_structural(raw_code)
            target_literal = _strip_comments_and_whitespace(raw_code)
    except Exception:
        return {"score": 0.0, "evidence": {}, "feedback": "Failed to read source file."}

    target_fp_struct = _get_fingerprints(target_structural)
    target_fp_lit = _get_fingerprints(target_literal)
    
    max_struct_sim = 0.0
    best_lit_sim = 0.0
    most_similar_file = "None"
    
    if os.path.exists(corpus_dir):
        for root, dirs, files in os.walk(corpus_dir):
            for file in files:
                if file.endswith('.c') and file != 'oracle.c':
                    corpus_path = os.path.join(root, file)
                    if os.path.abspath(corpus_path) == os.path.abspath(source_path):
                        continue
                        
                    with open(corpus_path, 'r', encoding='utf-8') as cf:
                        cf_raw = cf.read()
                        corpus_struct = _normalize_structural(cf_raw)
                        corpus_lit = _strip_comments_and_whitespace(cf_raw)
                        
                    corpus_fp_struct = _get_fingerprints(corpus_struct)
                    corpus_fp_lit = _get_fingerprints(corpus_lit)
                    
                    # Structural Jaccard Math
                    if not target_fp_struct and not corpus_fp_struct:
                        sim_struct = 1.0
                    elif not target_fp_struct or not corpus_fp_struct:
                        sim_struct = 0.0
                    else:
                        union_s = len(target_fp_struct.union(corpus_fp_struct))
                        intersection_s = len(target_fp_struct.intersection(corpus_fp_struct))
                        sim_struct = intersection_s / union_s if union_s > 0 else 0.0
                        
                    # Literal Jaccard Math
                    if not target_fp_lit and not corpus_fp_lit:
                        sim_lit = 1.0
                    elif not target_fp_lit or not corpus_fp_lit:
                        sim_lit = 0.0
                    else:
                        union_l = len(target_fp_lit.union(corpus_fp_lit))
                        intersection_l = len(target_fp_lit.intersection(corpus_fp_lit))
                        sim_lit = intersection_l / union_l if union_l > 0 else 0.0
                    
                    if sim_struct > max_struct_sim:
                        max_struct_sim = sim_struct
                        best_lit_sim = sim_lit
                        most_similar_file = file

    # Exact byte-for-byte matches stay 1.0; obfuscated matches are capped at 0.95
    effective_sim = 1.0 if best_lit_sim == 1.0 else min(max_struct_sim, 0.95)
    
    is_flagged = effective_sim >= threshold
    score = 0.0 if is_flagged else 1.0
    obfuscated = (max_struct_sim >= threshold and best_lit_sim < max_struct_sim - 0.1)

    return {
        "score": score,
        "evidence": {
            "effective_similarity": round(effective_sim, 2),
            "structural_match": round(max_struct_sim, 2),
            "literal_match": round(best_lit_sim, 2),
            "flag_threshold": threshold,
            "most_similar_file": most_similar_file,
            "flagged": is_flagged,
            "obfuscation_detected": obfuscated
        },
        "feedback": f"Similarity {round(effective_sim, 2)} (Struct: {round(max_struct_sim, 2)}, Lit: {round(best_lit_sim, 2)}) against {most_similar_file}. {'Flagged for review.' if is_flagged else 'Not flagged.'}"
    }