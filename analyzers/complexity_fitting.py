"""Generalized mathematical growth-model fitting for C-Grade Engine.

Pure-Python implementation: no NumPy/SciPy dependency. Fits multiplicative
log-linear models and additive non-negative basis models, compares them with
BIC under a relative-error Gaussian model, and screens measurements for saturation
before fitting. This estimates empirical growth; it does not prove asymptotic
complexity from finite observations.
"""
from __future__ import annotations

import itertools
import math
import statistics
from typing import Any

_EPS = 1e-12


def _safe_log(x: float) -> float:
    return math.log(max(float(x), _EPS))


def _median(values):
    return statistics.median(values) if values else 0.0


def _solve_linear(matrix, vector):
    """Solve a small square system with pivoted Gauss-Jordan elimination."""
    n = len(vector)
    if n == 0:
        return []
    a = [list(map(float, matrix[i])) + [float(vector[i])] for i in range(n)]
    scale = max((abs(v) for row in a for v in row[:-1]), default=1.0) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) <= 1e-12 * scale:
            raise ArithmeticError("singular regression system")
        a[col], a[pivot] = a[pivot], a[col]
        div = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= div
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor:
                for j in range(col, n + 1):
                    a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _least_squares(columns, y, ridge=1e-10):
    """Least squares via regularized normal equations for small feature sets."""
    p = len(columns)
    if not p:
        return []
    gram = [[sum(columns[i][r] * columns[j][r] for r in range(len(y)))
             for j in range(p)] for i in range(p)]
    rhs = [sum(columns[i][r] * y[r] for r in range(len(y))) for i in range(p)]
    diag_scale = max((gram[i][i] for i in range(p)), default=1.0) or 1.0
    for i in range(p):
        gram[i][i] += ridge * diag_scale
    return _solve_linear(gram, rhs)


def _ols_with_intercept(features, y):
    """OLS with a free intercept; features is rows x columns."""
    n = len(y)
    p = len(features[0]) if features else 0
    cols = [[1.0] * n]
    means, scales = [], []
    for j in range(p):
        col = [features[i][j] for i in range(n)]
        mean = statistics.mean(col)
        sd = math.sqrt(sum((v - mean) ** 2 for v in col) / max(1, n)) or 1.0
        means.append(mean)
        scales.append(sd)
        cols.append([(v - mean) / sd for v in col])
    beta_scaled = _least_squares(cols, y)
    slopes = [beta_scaled[j + 1] / scales[j] for j in range(p)]
    intercept = beta_scaled[0] - sum(beta_scaled[j + 1] * means[j] / scales[j] for j in range(p))
    beta = [intercept] + slopes
    pred = [beta[0] + sum(beta[j + 1] * features[i][j] for j in range(p)) for i in range(n)]
    return beta, pred


def _nnls_active_set(columns, target, weights=None):
    """Small-system NNLS using exhaustive active sets (at most four terms).

    Enumerates feasible active subsets and solves each by regularized least
    squares. This is exact for the small coefficient counts used by the fitter
    and avoids slow convergence when basis columns are strongly correlated.
    """
    if not columns:
        return []
    n, p = len(target), len(columns)
    if p > 8:
        raise ValueError("NNLS active-set solver supports at most eight columns")
    weights = weights or [1.0] * n
    weighted_columns = [[columns[j][i] * weights[i] for i in range(n)] for j in range(p)]
    weighted_target = [target[i] * weights[i] for i in range(n)]
    column_norms = [math.sqrt(sum(v * v for v in col)) or 1.0 for col in weighted_columns]
    normalized_columns = [[v / column_norms[j] for v in weighted_columns[j]] for j in range(p)]
    best_coeff, best_error = [0.0] * p, float("inf")
    for mask in range(1, 1 << p):
        active = [j for j in range(p) if mask & (1 << j)]
        try:
            beta = _least_squares([normalized_columns[j] for j in active], weighted_target, ridge=1e-13)
        except ArithmeticError:
            continue
        if any(value < -1e-8 for value in beta):
            continue
        coeff = [0.0] * p
        for j, value in zip(active, beta):
            coeff[j] = max(0.0, value / column_norms[j])
        error = 0.0
        for i in range(n):
            residual = weighted_target[i] - sum(weighted_columns[j][i] * coeff[j] for j in range(p))
            error += residual * residual
        if error < best_error:
            best_error, best_coeff = error, coeff
    return best_coeff




def _term_value(n, term):
    p, q, log_d, e = term["p"], term["q"], term["log_d"], term["factorial_power"]
    ln = math.log(max(2.0, n))
    value = p * ln + q * math.log(max(_EPS, ln)) + log_d * n
    if e:
        value += e * math.lgamma(n + 1.0)
    return value


def _term_feature(ns, term):
    logs = [_term_value(n, term) for n in ns]
    shift = max(logs)
    return [math.exp(max(-700.0, x - shift)) for x in logs]


def _term_label(term):
    p, q, log_d, e = term["p"], term["q"], term["log_d"], term["factorial_power"]
    bits = []
    if e:
        bits.append("(n!)" if abs(e - 1) < 1e-8 else f"(n!)^{e:g}")
    if log_d > 1e-8:
        d = math.exp(log_d)
        bits.append(f"{d:.3g}^n")
    if p:
        bits.append("n" if abs(p - 1) < 1e-8 else f"n^{p:g}")
    if q:
        log_part = "log(n)" if abs(q - 1) < 1e-8 else f"(log(n))^{q:g}"
        bits.append(log_part)
    return " * ".join(bits) if bits else "1"


def _make_basis(max_poly=8.0, max_exp=4.0, polynomial_step=0.25,
                exponential_step=0.05, logarithmic_powers=None,
                polynomial_log_powers=None, exponential_poly_powers=None,
                factorial_powers=None, factorial_poly_powers=None):
    """Build an extensible, deduplicated additive basis dictionary."""
    terms, seen_terms = [], set()
    polynomial_step = max(0.05, float(polynomial_step))
    exponential_step = max(0.01, float(exponential_step))
    logarithmic_powers = logarithmic_powers or (0.5, 1.0, 1.5, 2.0, 3.0, 4.0)
    polynomial_log_powers = polynomial_log_powers or (0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5)
    exponential_poly_powers = exponential_poly_powers or (0.0, 0.5, 1.0, 2.0, 3.0)
    factorial_powers = factorial_powers or (0.25, 0.5, 0.75, 1.0)
    factorial_poly_powers = factorial_poly_powers or (0.0, 0.5, 1.0, 2.0)

    def add(p=0.0, q=0.0, d=1.0, e=0.0):
        term = {"p": float(p), "q": float(q), "log_d": math.log(float(d)),
                "factorial_power": float(e)}
        key = tuple(round(term[k], 10) for k in ("p", "q", "log_d", "factorial_power"))
        if key not in seen_terms:
            terms.append(term)
            seen_terms.add(key)

    add()
    polynomial_count = int(math.floor(float(max_poly) / polynomial_step))
    p_values = [round(i * polynomial_step, 8) for i in range(1, polynomial_count + 1)]
    for p in p_values:
        add(p=p)
    for q in logarithmic_powers:
        add(q=float(q))
    for p in polynomial_log_powers:
        for q in logarithmic_powers:
            add(p=float(p), q=float(q))

    exp_count = int(math.floor((float(max_exp) - 1.10) / exponential_step))
    for i in range(max(0, exp_count) + 1):
        base = round(1.10 + i * exponential_step, 8)
        if base <= 1.0:
            continue
        for p in exponential_poly_powers:
            add(p=float(p), d=base)
    for e in factorial_powers:
        for p in factorial_poly_powers:
            add(p=float(p), e=float(e))
    return terms


def _bic(residuals, k):
    """Gaussian BIC for the shared relative-error likelihood.

    Both model families minimize squared relative residuals. ``k`` counts the
    fitted mean-model parameters; the residual variance is an additional
    estimated parameter and is included here as required by Gaussian BIC.
    """
    n = len(residuals)
    if n == 0:
        return float("inf")
    rss = max(_EPS, sum(r * r for r in residuals))
    total_parameters = int(k) + 1
    return n * math.log(rss / n) + total_parameters * math.log(max(2, n))

def _relative_residuals(predictions, observations):
    return [(predictions[i] - observations[i]) / max(_EPS, observations[i])
            for i in range(len(observations))]


def _fit_additive(ns, ys, max_terms=3, max_poly=8.0, max_exp=4.0,
                  basis_options=None):
    """Fit c0 + sum(ai*phi_i(n)), ai>=0, with a bounded basis search.

    Candidate ranking is done with a non-negative constant offset plus one
    growth term. The best individual terms seed pair/triple searches, which
    avoids a single greedy path missing additive combinations.
    """
    median_y = _median(ys) or 1.0
    target = [y / median_y for y in ys]
    weights = [1.0 / max(_EPS, y) for y in target]
    candidates = []
    seen = set()
    for term in _make_basis(max_poly=max_poly, max_exp=max_exp, **(basis_options or {})):
        if term["p"] == term["q"] == term["log_d"] == term["factorial_power"] == 0:
            continue
        feature = _term_feature(ns, term)
        signature = tuple(round(v, 5) for v in feature)
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append((term, feature))

    constant = [1.0] * len(ns)
    ranked = []
    for term, feature in candidates:
        cols = [constant, feature]
        coeff = _nnls_active_set(cols, target, weights)
        pred = [sum(coeff[j] * cols[j][i] for j in range(2)) for i in range(len(ns))]
        if any(v <= _EPS for v in pred):
            continue
        residuals = _relative_residuals(pred, target)
        active = sum(c > 1e-7 for c in coeff)
        ranked.append((_bic(residuals, active), term, feature))
    ranked.sort(key=lambda x: x[0])
    # Limit combinatorial search to a diverse, high-quality shortlist.
    shortlist = ranked[:30]
    # Keep theoretically meaningful subdominant anchors in the pair search.
    # A low-order term can have a poor single-term score while still be an
    # important component of an additive expression.
    anchors = [
        (1, 0, 1, 0), (2, 0, 1, 0), (3, 0, 1, 0), (4, 0, 1, 0),
        (0, 0.5, 1, 0), (0, 1, 1, 0), (0, 2, 1, 0),
        (1, 1, 1, 0), (2, 1, 1, 0), (3, 1, 1, 0), (2, 2, 1, 0),
        (0, 0, 2, 0), (1, 0, 2, 0), (2, 0, 2, 0), (3, 0, 2, 0),
        (0, 0, 1, 1), (1, 0, 1, 1),
    ]
    known = {tuple(round(item[2][i], 6) for i in range(len(ns))) for item in shortlist}
    anchor_items = []
    for p, q, d, e in anchors:
        term = {"p": float(p), "q": float(q), "log_d": math.log(float(d)), "factorial_power": float(e)}
        feature = _term_feature(ns, term)
        signature = tuple(round(v, 6) for v in feature)
        if signature not in known:
            item = (float("inf"), term, feature)
            shortlist.append(item)
            anchor_items.append(item)
            known.add(signature)
    if not shortlist:
        return None

    best = None
    max_terms = max(1, min(3, int(max_terms)))
    for count in range(1, max_terms + 1):
        if count == 2:
            pool = shortlist
        elif count == 3:
            pool = shortlist[:10] + anchor_items
        else:
            pool = shortlist[:12]
        for combo in itertools.combinations(pool, count):
            growth_terms = [(item[1], item[2]) for item in combo]
            cols = [constant] + [item[1] for item in growth_terms]
            coeff = _nnls_active_set(cols, target, weights)
            pred = [sum(coeff[j] * cols[j][i] for j in range(len(cols))) for i in range(len(ns))]
            if any(v <= _EPS for v in pred):
                continue
            residuals = _relative_residuals(pred, target)
            active = sum(c > 1e-7 for c in coeff)
            bic = _bic(residuals, active)
            active_growth = sum(c > 1e-7 for c in coeff[1:])
            # The basis identity is selected from a large dictionary. Add a
            # conservative search penalty on top of ordinary BIC to discourage
            # spurious multi-term fits to a short observation series.
            search_penalty = (2.8 * active_growth * math.log(max(2, len(candidates)))
                              + 8.0 * max(0, active_growth - 1))
            selection_score = bic + search_penalty
            if best is None or selection_score < best[0]:
                best = (selection_score, bic, growth_terms, coeff, pred, residuals)
    if best is None:
        return None

    selection_score, bic, growth_terms, coeff, pred_scaled, log_res = best
    terms = []
    fixed_cost = coeff[0] * median_y
    for (term, feature), coef in zip(growth_terms, coeff[1:]):
        if coef <= 1e-7:
            continue
        max_log = max(_term_value(n, term) for n in ns)
        actual_coef = coef * median_y / math.exp(min(700.0, max_log))
        terms.append({"coefficient": actual_coef, "basis": _term_label(term), **term})
    if not terms:
        return None
    predictions = [v * median_y for v in pred_scaled]
    log_residuals = [math.log(max(_EPS, predictions[i]) / ys[i]) for i in range(len(ns))]
    return {"family": "additive_basis", "terms": terms, "fixed_cost": fixed_cost,
            "predictions": predictions, "log_residuals": log_residuals, "bic": bic,
            "selection_score": selection_score, "search_penalty": selection_score - bic,
            "fit_error": math.sqrt(statistics.mean(r * r for r in _relative_residuals(predictions, ys))),
            "parameter_count": sum(1 for c in coeff if c > 1e-7)}


def _refine_multiplicative_relative(ns, ys, subset, offset, intercept, slopes):
    """Refine a log-linear multiplicative fit under relative-Gaussian error."""
    feature_columns = [
        [math.log(max(2.0, n)) for n in ns],
        [math.log(max(_EPS, math.log(max(2.0, n)))) for n in ns],
        [n for n in ns],
        [math.lgamma(n + 1.0) for n in ns],
    ]
    theta = [max(0.0, offset), float(intercept)] + [max(0.0, v) for v in slopes]

    def evaluate(values):
        base = []
        predictions = []
        for i, n in enumerate(ns):
            exponent = values[1] + sum(values[j + 2] * feature_columns[idx][i]
                                       for j, idx in enumerate(subset))
            component = math.exp(max(-700.0, min(700.0, exponent)))
            base.append(component)
            predictions.append(values[0] + component)
        residuals = _relative_residuals(predictions, ys)
        return sum(r * r for r in residuals), predictions, residuals, base

    current, predictions, residuals, base = evaluate(theta)
    for _ in range(30):
        # Jacobian for [fixed offset, log(a), selected growth exponents].
        jac = []
        for i, n in enumerate(ns):
            row = [1.0 / ys[i], base[i] / ys[i]]
            row.extend(base[i] * feature_columns[idx][i] / ys[i] for idx in subset)
            jac.append(row)
        p = len(theta)
        norms = [math.sqrt(sum(jac[i][j] ** 2 for i in range(len(ns)))) or 1.0 for j in range(p)]
        scaled = [[jac[i][j] / norms[j] for i in range(len(ns))] for j in range(p)]
        hessian = [[sum(scaled[a][i] * scaled[b][i] for i in range(len(ns)))
                    for b in range(p)] for a in range(p)]
        gradient = [sum(scaled[j][i] * residuals[i] for i in range(len(ns))) for j in range(p)]
        for j in range(p):
            hessian[j][j] += 1e-11
        try:
            scaled_delta = _solve_linear(hessian, [-v for v in gradient])
        except ArithmeticError:
            break
        delta = [scaled_delta[j] / norms[j] for j in range(p)]
        step = 1.0
        improved = False
        while step >= 1e-6:
            trial = [theta[j] + step * delta[j] for j in range(p)]
            trial[0] = max(0.0, trial[0])
            for j in range(2, p):
                trial[j] = max(0.0, trial[j])
            score, trial_predictions, trial_residuals, trial_base = evaluate(trial)
            if score < current - 1e-15:
                theta = trial
                current, predictions, residuals, base = score, trial_predictions, trial_residuals, trial_base
                improved = True
                break
            step *= 0.5
        if not improved or max((abs(step * v) for v in delta), default=0.0) < 1e-9:
            break
    return theta, predictions, residuals


def _fit_multiplicative(ns, ys, allowed_subset=None):
    # Model includes an additive fixed-cost offset: C(n)=c0+a*n^b*(log n)^c*d^n*(n!)^e.
    # The offset captures process startup/runtime costs without distorting growth exponents.
    best = None
    min_y = min(ys)
    offsets = [0.0]
    offsets.extend(min_y * (i / 20.0) for i in range(1, 19))
    if len(ys) >= 2:
        first_step = max(_EPS, ys[1] - ys[0])
        offsets.extend(min_y - first_step * fraction
                       for fraction in (0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 1.0, 1.25))
    offsets.extend(min_y * (1.0 - 10.0 ** (-power)) for power in (2, 3, 4, 5, 6))
    offsets = sorted({max(0.0, min(min_y * 0.999999, value)) for value in offsets})
    for offset in offsets:
        adjusted = [y - offset for y in ys]
        if min(adjusted) <= max(_EPS, min_y * 1e-5):
            continue
        y = [math.log(v) for v in adjusted]
        columns = [
            [math.log(max(2.0, n)) for n in ns],
            [math.log(max(_EPS, math.log(max(2.0, n)))) for n in ns],
            [n for n in ns],
            [math.lgamma(n + 1.0) for n in ns],
        ]
        subsets = [tuple(allowed_subset)] if allowed_subset is not None else [
            subset for size in range(0, 5) for subset in itertools.combinations(range(4), size)
        ]
        for subset in subsets:
                chosen = [columns[i] for i in subset]
                try:
                    beta, pred = _ols_with_intercept([[col[r] for col in chosen] for r in range(len(ns))], y)
                except (ArithmeticError, ValueError):
                    continue
                intercept = beta[0]
                slopes = beta[1:]
                if any(v < -1e-7 for v in slopes):
                    continue
                # Near-unit exponential bases and tiny factorial powers are not
                # identifiable from short finite runs; treat them as polynomial
                # approximation artifacts rather than claiming exponential growth.
                if any((idx == 2 and 0.0 < value < math.log(1.05)) or
                       (idx == 3 and 0.0 < value < 0.05)
                       for idx, value in zip(subset, slopes)):
                    continue
                refined, predicted_costs, relative_res = _refine_multiplicative_relative(
                    ns, ys, subset, offset, intercept, slopes
                )
                refined_offset, refined_intercept = refined[0], refined[1]
                refined_slopes = refined[2:]
                # Reject growth terms that collapse to an effectively unit
                # exponential base or negligible factorial power after refinement.
                if any((idx == 2 and 0.0 < value < math.log(1.05)) or
                       (idx == 3 and 0.0 < value < 0.05)
                       for idx, value in zip(subset, refined_slopes)):
                    continue
                log_res = [math.log(max(_EPS, predicted_costs[i]) / ys[i]) for i in range(len(ns))]
                # Count fixed cost, log(a), and each selected growth exponent.
                k = 1 + len(refined_slopes) + (1 if refined_offset > 1e-9 else 0)
                bic = _bic(relative_res, k)
                if best is None or bic < best[0]:
                    params = {"a": math.exp(max(-700, min(700, refined_intercept))), "b": 0.0,
                              "c": 0.0, "d": 1.0, "e": 0.0, "fixed_cost": refined_offset}
                    for idx, value in zip(subset, refined_slopes):
                        if idx == 0: params["b"] = max(0.0, value)
                        elif idx == 1: params["c"] = max(0.0, value)
                        elif idx == 2: params["d"] = math.exp(max(0.0, value))
                        elif idx == 3: params["e"] = max(0.0, value)
                    best = (bic, params, predicted_costs, log_res, k)
    if best is None:
        return None
    bic, params, predictions, residuals, k = best
    factors = []
    if abs(params["b"]) > 1e-6:
        factors.append("n" if abs(params["b"] - 1.0) < 1e-6 else f"n^{params['b']:.4g}")
    if abs(params["c"]) > 1e-6:
        factors.append("log(n)" if abs(params["c"] - 1.0) < 1e-6 else f"(log(n))^{params['c']:.4g}")
    if params["d"] > 1.000001:
        factors.append(f"{params['d']:.5g}^n")
    if params["e"] > 1e-6:
        factors.append("n!" if abs(params["e"] - 1.0) < 1e-5 else f"(n!)^{params['e']:.4g}")
    growth = " * ".join(factors) if factors else "1"
    fixed = f"{params['fixed_cost']:.5g} + " if params["fixed_cost"] > 1e-8 else ""
    return {"family": "multiplicative", "parameters": params,
            "fitted_function": f"{fixed}{params['a']:.5g} * {growth}",
            "predictions": predictions, "log_residuals": residuals, "bic": bic,
            "fit_error": math.sqrt(statistics.mean(r * r for r in _relative_residuals(predictions, ys))),
            "parameter_count": k}


def _dominant_term(terms):
    def rank(term):
        # Factorial dominates exponential; exponential dominates polynomial;
        # polynomial exponent dominates logarithmic exponent.
        return (term.get("factorial_power", 0.0), term.get("log_d", 0.0),
                term.get("p", 0.0), term.get("q", 0.0))
    return max(terms, key=rank)


def _complexity_label(term):
    p, q, log_d, e = term["p"], term["q"], term["log_d"], term["factorial_power"]
    for conventional in range(0, 9):
        if abs(p - conventional) < 0.08:
            p = float(conventional)
            break
    if abs(q - 1.0) < 0.08:
        q = 1.0
    factors = []
    if p > 1e-5:
        factors.append("n" if abs(p - 1) < 1e-4 else f"n^{p:.3g}")
    if q > 1e-5:
        factors.append("log n" if abs(q - 1) < 1e-4 else f"(log n)^{q:.3g}")
    if log_d > 1e-5:
        factors.append(f"{math.exp(log_d):.3g}^n")
    if e > 1e-5:
        factors.append("n!" if abs(e - 1) < 0.05 else f"(n!)^{e:.3g}")
    if factors:
        return "O(" + " * ".join(factors) + ")"
    if p > 1e-5 and q > 1e-5:
        poly = "n" if abs(p - 1) < 1e-4 else f"n^{p:.3g}"
        log_part = "log n" if abs(q - 1) < 1e-4 else f"(log n)^{q:.3g}"
        return f"O({poly} {log_part})"
    if p > 1e-5:
        return "O(n)" if abs(p - 1) < 1e-4 else f"O(n^{p:.3g})"
    if q > 1e-5:
        return "O(log n)" if abs(q - 1) < 1e-4 else f"O((log n)^{q:.3g})"
    return "O(1)"



def _generic_complexity_family(term):
    """Return one of the agreed generic complexity families."""
    p = float(term.get("p", 0.0))
    q = float(term.get("q", 0.0))
    log_d = float(term.get("log_d", 0.0))
    factorial_power = float(term.get("factorial_power", 0.0))

    has_polynomial = p > 1e-5
    has_logarithm = q > 1e-5
    has_exponential = log_d > 1e-5
    has_factorial = factorial_power > 1e-5

    if has_factorial:
        return "Factorial-polynomial" if (
            has_polynomial or has_logarithm
        ) else "Factorial"

    if has_exponential:
        return "Exponential-polynomial" if (
            has_polynomial or has_logarithm
        ) else "Exponential"

    if has_polynomial and has_logarithm:
        return "Polynomial-logarithmic"

    if has_polynomial:
        return "Polynomial"

    if has_logarithm:
        return "Logarithmic"

    return "Constant"


def _generic_complexity_label(term):
    """Map a fitted term to one of the agreed generic expressions."""
    family = _generic_complexity_family(term)

    p = float(term.get("p", 0.0))
    q = float(term.get("q", 0.0))

    if family == "Constant":
        return "O(1)"

    if family == "Logarithmic":
        return "O(log N)"

    if family == "Polynomial":
        degree = min(
            (1, 2, 3),
            key=lambda value: abs(p - value)
        )

        return f"O(N^{degree})" if degree > 1 else "O(N)"

    if family == "Polynomial-logarithmic":
        choices = [
            (1, 1, "O(N log N)"),
            (2, 1, "O(N^2 log N)")
        ]

        return min(
            choices,
            key=lambda item: (
                (p - item[0]) ** 2 +
                (q - item[1]) ** 2
            )
        )[2]

    if family == "Exponential":
        return "O(c^N)"

    if family == "Exponential-polynomial":
        return "O(N^k * c^N)"

    if family == "Factorial":
        return "O(N!)"

    if family == "Factorial-polynomial":
        return "O(N^k * N!)"

    return "Inconclusive"


def _saturation_screen(measurements):
    """Validate observations, isolate one counter type, then detect tail saturation.

    Measurements from Callgrind, SanitizerCoverage and GCC function hooks are
    different quantities. If a run contains more than one method, only the
    predominant method is fitted; all other rows are retained in exclusions.
    The original input list is returned separately by ``fit_growth``.
    """
    valid_rows, excluded, warnings = [], [], []
    method_priority = {
        "callgrind_instructions": 3,
        "sanitizer_coverage_basic_block_events": 2,
        "gcc_function_entry_events": 1,
        "gcc_function_entry_proxy": 1,
        "unspecified": 0,
    }
    for index, row in enumerate(list(measurements or [])):
        if not isinstance(row, dict):
            excluded.append({"index": index, "observation": row,
                             "reason": "measurement_must_be_an_object"})
            continue
        try:
            n = float(row.get("n"))
            cost = float(row.get("cost", row.get("instructions")))
        except (TypeError, ValueError):
            excluded.append({**row, "index": index,
                             "reason": "missing_or_non_numeric_measurement"})
            continue
        if n <= 1 or cost <= 0 or not math.isfinite(n) or not math.isfinite(cost):
            excluded.append({**row, "index": index,
                             "reason": "nonpositive_or_nonfinite_size_or_cost"})
            continue
        method = str(row.get("method") or "unspecified").strip().lower()
        valid_rows.append({**row, "n": n, "cost": cost, "method": method,
                           "status": "valid"})

    original_valid_count = len(valid_rows)

    # Never fit mixed counters as if they were measurements in the same unit.
    method_counts = {}
    for row in valid_rows:
        method_counts[row["method"]] = method_counts.get(row["method"], 0) + 1
    selected_method = None
    if method_counts:
        selected_method = max(
            method_counts,
            key=lambda name: (method_counts[name], method_priority.get(name, 0), name)
        )
        if len(method_counts) > 1:
            for row in valid_rows:
                if row["method"] != selected_method:
                    excluded.append({**row,
                                     "reason": f"different_profiling_method_excluded; selected={selected_method}"})
            warnings.append({
                "type": "mixed_measurement_methods",
                "message": "Different profiling counters were present. Only the predominant counter type was fitted; other methods were excluded.",
                "selected_method": selected_method,
                "method_counts": dict(method_counts),
            })
            valid_rows = [row for row in valid_rows if row["method"] == selected_method]

    # Sort only validated rows, so malformed entries cannot break ordering.
    grouped = {}
    for row in valid_rows:
        grouped.setdefault(row["n"], []).append(row)
    points = [{"n": n, "cost": _median([r["cost"] for r in rows]),
               "replicates": len(rows), "method": selected_method}
              for n, rows in sorted(grouped.items())]

    rates = []
    for left, right in zip(points, points[1:]):
        if right["n"] <= left["n"] or left["cost"] <= 0 or right["cost"] <= 0:
            rates.append(float("nan"))
        else:
            rates.append(math.log(right["cost"] / left["cost"]) /
                         math.log(right["n"] / left["n"]))

    saturation_start = None
    if len(rates) >= 4:
        for i in range(2, len(rates) - 1):
            baseline_values = [v for v in rates[max(0, i - 2):i] if math.isfinite(v)]
            if not baseline_values:
                continue
            baseline = _median(baseline_values)
            if baseline > 0.35 and rates[i] < 0.30 * baseline and rates[i + 1] < 0.30 * baseline:
                saturation_start = i
                break
    if saturation_start is not None:
        first_bad_point = saturation_start + 1
        tail = points[first_bad_point:]
        keep = points[:first_bad_point]
        for point in tail:
            excluded.append({**point, "reason": "suspected_measurement_saturation_plateau"})
        points = keep
        if len(keep) >= 5:
            warnings.append({"type": "saturation",
                             "message": "A sustained collapse in adjacent growth rates suggests counter saturation or truncation; tail observations were excluded from fitting.",
                             "excluded_sizes": [p["n"] for p in tail]})
        else:
            warnings.append({"type": "possible_saturation",
                             "message": "A plateau was detected, but excluding it would leave fewer than five observations; the fit is inconclusive.",
                             "excluded_sizes": [p["n"] for p in tail]})
            # Do not fit the contaminated full series when the clean prefix is
            # too short to support the minimum observation requirement.
            points = keep

    # Report isolated growth-rate anomalies, but retain them for transparency.
    if len(points) >= 5:
        local_rates = rates[:max(0, len(points) - 1)]
        for i in range(1, len(local_rates) - 1):
            before, current, after = local_rates[i - 1:i + 2]
            if all(math.isfinite(v) and v > 0 for v in (before, current, after)):
                if current > 3.0 * max(before, after) or current < min(before, after) / 3.0:
                    warnings.append({"type": "possible_outlier", "n": points[i]["n"],
                                     "message": "An isolated change in adjacent growth rates suggests a noisy observation; retained pending repeat measurements."})
    return points, excluded, warnings, original_valid_count


def _residual_diagnostics(ns, residuals):
    if not residuals:
        return {"log_rmse": None, "max_abs_log_residual": None, "residual_trend": None}
    mean_x, mean_y = statistics.mean([math.log(n) for n in ns]), statistics.mean(residuals)
    den = sum((math.log(n) - mean_x) ** 2 for n in ns)
    trend = (sum((math.log(n) - mean_x) * (r - mean_y) for n, r in zip(ns, residuals)) / den
             if den else 0.0)
    return {"log_rmse": math.sqrt(statistics.mean(r * r for r in residuals)),
            "max_abs_log_residual": max(abs(r) for r in residuals),
            "residual_trend": trend}


def _fit_fixed_additive(train_ns, train_ys, terms, test_ns):
    """Refit coefficients for a selected additive basis without reselection."""
    median_y = _median(train_ys) or 1.0
    target = [y / median_y for y in train_ys]
    weights = [1.0 / max(_EPS, y) for y in target]
    features = [_term_feature(train_ns, term) for term in terms]
    columns = [[1.0] * len(train_ns)] + features
    coeff = _nnls_active_set(columns, target, weights)
    fixed = coeff[0] * median_y
    actual_coefficients = []
    for j, term in enumerate(terms):
        max_log = max(_term_value(n, term) for n in train_ns)
        actual_coefficients.append(coeff[j + 1] * median_y / math.exp(min(700.0, max_log)))
    predictions = []
    for n in test_ns:
        value = fixed
        for coefficient, term in zip(actual_coefficients, terms):
            value += coefficient * math.exp(min(700.0, _term_value(n, term)))
        predictions.append(value)
    return predictions


def fit_growth(measurements, config=None):
    """Fit generalized multiplicative/additive growth models and report uncertainty."""
    config = config or {}
    points, excluded, warnings, original_valid_count = _saturation_screen(measurements)
    if len(points) < 5:
        return {"complexity": "inconclusive", "generic_family": "Inconclusive", "confidence": "low", "model": "inconclusive",
                "fit_error": None,
                "reason": "Fewer than five usable observations remain after measurement validation.",
                "measurement_count": len(points), "valid_observation_count": len(points),
                "excluded_observation_count": len(excluded),
                "excluded_observations": excluded, "warnings": warnings,
                "original_valid_observation_count": original_valid_count,
                "measurement_method": points[0].get("method") if points else None,
                "raw_measurements": list(measurements or [])}
    ns = [p["n"] for p in points]
    ys = [p["cost"] for p in points]
    multiplicative = _fit_multiplicative(ns, ys)
    basis_options = {
        key: config[key] for key in (
            "polynomial_step", "exponential_step", "logarithmic_powers",
            "polynomial_log_powers", "exponential_poly_powers",
            "factorial_powers", "factorial_poly_powers"
        ) if key in config
    }
    additive = _fit_additive(ns, ys, int(config.get("max_additive_terms", 3)),
                             float(config.get("max_polynomial_exponent", 8.0)),
                             float(config.get("max_exponential_base", 4.0)),
                             basis_options=basis_options)
    candidates = [x for x in (multiplicative, additive) if x is not None]
    if not candidates:
        return {"complexity": "inconclusive", "generic_family": "Inconclusive", "confidence": "low", "model": "inconclusive",
                "fit_error": None, "reason": "No numerically stable model could be fitted.",
                "measurement_count": len(points), "valid_observation_count": len(points),
                "excluded_observation_count": len(excluded),
                "excluded_observations": excluded, "warnings": warnings,
                "measurement_method": points[0].get("method") if points else None,
                "raw_measurements": list(measurements or [])}
    candidates.sort(key=lambda x: x.get("selection_score", x["bic"]))
    best = candidates[0]
    # Keep ordinary BIC separate from the additional discrete basis-search
    # penalty. Both gaps matter when assessing whether the winner is decisive.
    best_selection = best.get("selection_score", best["bic"])
    if len(candidates) > 1:
        raw_bic_gap = candidates[1]["bic"] - best["bic"]
        selection_gap = candidates[1].get("selection_score", candidates[1]["bic"]) - best_selection
    else:
        raw_bic_gap = selection_gap = float("inf")
    if best["family"] == "additive_basis":
        dominant = _dominant_term(best["terms"])
        complexity = _complexity_label(dominant)
        growth_expression = " + ".join(f"{t['coefficient']:.5g}*{t['basis']}" for t in best["terms"])
        fitted_function = f"{best.get('fixed_cost', 0.0):.5g} + {growth_expression}"
        parameters = {"fixed_cost": best.get("fixed_cost", 0.0), "terms": best["terms"]}
    else:
        params = best["parameters"]
        dominant = {"p": params["b"], "q": params["c"], "log_d": math.log(params["d"]), "factorial_power": params["e"]}
        complexity = _complexity_label(dominant)
        fitted_function = best["fitted_function"]
        parameters = params
    generic_family = _generic_complexity_family(dominant)
        # Preserve the continuous mathematical fit for diagnostics.
    fitted_complexity = complexity

    # Assign one of the predefined generic complexity classes.
    generic_family = _generic_complexity_family(dominant)
    complexity = _generic_complexity_label(dominant)
    diagnostics = _residual_diagnostics(ns, best["log_residuals"])
    cv_error = None
    if len(points) >= 8:
        # Deterministic blocked K-fold validation. The selected model family and
        # additive basis are held fixed; coefficients are refitted on each fold.
        fold_count = min(5, max(2, len(ns) // 2))
        errors = []
        for fold in range(fold_count):
            test_indices = [i for i in range(len(ns)) if i % fold_count == fold]
            train_indices = [i for i in range(len(ns)) if i % fold_count != fold]
            train_ns = [ns[i] for i in train_indices]
            train_ys = [ys[i] for i in train_indices]
            test_ns = [ns[i] for i in test_indices]
            try:
                if best["family"] == "multiplicative":
                    p = best["parameters"]
                    subset = []
                    if p["b"] > 1e-6: subset.append(0)
                    if p["c"] > 1e-6: subset.append(1)
                    if p["d"] > 1.000001: subset.append(2)
                    if p["e"] > 1e-6: subset.append(3)
                    model = _fit_multiplicative(train_ns, train_ys, allowed_subset=subset)
                    if model is None: continue
                    fitted = model["parameters"]
                    predictions = [
                        fitted.get("fixed_cost", 0.0) + fitted["a"] * n ** fitted["b"] *
                        max(_EPS, math.log(max(2.0, n))) ** fitted["c"] * fitted["d"] ** n *
                        math.exp(min(700.0, fitted["e"] * math.lgamma(n + 1.0)))
                        for n in test_ns
                    ]
                else:
                    predictions = _fit_fixed_additive(train_ns, train_ys, best["terms"], test_ns)
                for index, prediction in zip(test_indices, predictions):
                    errors.append(abs(math.log(max(_EPS, prediction) / ys[index])))
            except (ArithmeticError, OverflowError, ValueError, KeyError):
                continue
        if errors:
            cv_error = statistics.mean(errors)
    # Confidence is conservative: no saturation warnings, enough data, small residuals,
    # and clear separation from the competing family are all required for high confidence.
    if warnings or len(points) < 7 or diagnostics["log_rmse"] > 0.12:
        confidence = "low"
    elif (diagnostics["log_rmse"] < 0.025 and raw_bic_gap > 6.0 and
          selection_gap > 6.0 and (cv_error is None or cv_error < 0.08)):
        confidence = "high"
    elif (diagnostics["log_rmse"] < 0.10 and selection_gap > 2.0 and
          raw_bic_gap > 0.0 and (cv_error is None or cv_error < 0.15)):
        confidence = "medium"
    else:
        confidence = "low"
    competitors = []
    for candidate in candidates[1:4]:
        if candidate["family"] == "additive_basis":
            dterm = _dominant_term(candidate["terms"])
            label = _complexity_label(dterm)
        else:
            p = candidate["parameters"]
            label = _complexity_label({"p": p["b"], "q": p["c"], "log_d": math.log(p["d"]), "factorial_power": p["e"]})
        competitors.append({"family": candidate["family"], "complexity": label,
                            "bic": round(candidate["bic"], 5),
                            "selection_score": round(candidate.get("selection_score", candidate["bic"]), 5),
                            "fit_error": round(candidate["fit_error"], 6)})
        return {"complexity": complexity,
            "dominant_asymptotic": complexity,
            "fitted_complexity": fitted_complexity,
            "generic_family": generic_family,
            "generic_family": generic_family,
            "fitted_function": fitted_function, "model": best["family"],
            "model_family": best["family"],
            "parameters": parameters, "confidence": confidence,
            "fit_error": round(best["fit_error"], 8), "bic": round(best["bic"], 6),
            "model_selection": {"raw_bic_gap_to_runner_up": (None if not math.isfinite(raw_bic_gap) else round(raw_bic_gap, 6)),
                                "selection_gap_to_runner_up": (None if not math.isfinite(selection_gap) else round(selection_gap, 6)),
                                "basis_search_penalty": round(best.get("search_penalty", 0.0), 6)},
            "cross_validation_log_error": None if cv_error is None else round(cv_error, 8),
            "residual_diagnostics": diagnostics, "competing_models": competitors,
            "measurement_count": len(points), "valid_observation_count": len(points),
            "excluded_observation_count": len(excluded), "excluded_observations": excluded,
            "warnings": warnings, "original_valid_observation_count": original_valid_count,
            "measurement_method": points[0].get("method") if points else None,
            "raw_measurements": list(measurements or []),
            "observations_used": [{"n": p["n"], "cost": p["cost"], "method": p.get("method")}
                                  for p in points]}
