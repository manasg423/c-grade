import os
import re
import hashlib

def _normalize_c_code(code_string):
    # Strip comments
    code = re.sub(r'//.*|/\*[\s\S]*?\*/', '', code_string)
    # Standardize strings and identifiers
    code = re.sub(r'".*?"', 'STR', code)
    code = re.sub(r'\b[a-zA-Z_]\w*\b', 'ID', code)
    # Strip all whitespace
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
            target_code = _normalize_c_code(f.read())
    except Exception:
        return {"score": 0.0, "evidence": {}, "feedback": "Failed to read source file."}

    target_fp = _get_fingerprints(target_code)
    max_sim = 0.0
    most_similar_file = "None"
    
    if os.path.exists(corpus_dir):
        for root, dirs, files in os.walk(corpus_dir):
            for file in files:
                if file.endswith('.c') and file != 'oracle.c':
                    corpus_path = os.path.join(root, file)
                    if os.path.abspath(corpus_path) == os.path.abspath(source_path):
                        continue # Skip self-comparison
                        
                    with open(corpus_path, 'r', encoding='utf-8') as cf:
                        corpus_fp = _get_fingerprints(_normalize_c_code(cf.read()))
                    
                    if not target_fp and not corpus_fp:
                        sim = 1.0
                    elif not target_fp or not corpus_fp:
                        sim = 0.0
                    else:
                        # Exact Jaccard token similarity math
                        intersection = len(target_fp.intersection(corpus_fp))
                        union = len(target_fp.union(corpus_fp))
                        sim = intersection / union if union > 0 else 0.0
                    
                    if sim > max_sim:
                        max_sim = sim
                        most_similar_file = file

    # Flag only submission pairs whose structural token similarity crosses the threshold
    is_flagged = max_sim >= threshold
    score = 0.0 if is_flagged else 1.0

    return {
        "score": score,
        "evidence": {
            "max_similarity": round(max_sim, 2),
            "flag_threshold": threshold,
            "most_similar_file": most_similar_file,
            "flagged": is_flagged
        },
        "feedback": f"Max similarity {round(max_sim, 2)} against {most_similar_file}. {'Flagged for review.' if is_flagged else 'Not flagged.'}"
    }