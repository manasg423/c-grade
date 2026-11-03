"""Confidence-aware generic complexity fitting for C-Grade Engine.

Public API and result keys are kept compatible with the previous fitter.
The fitter compares fixed-cost-plus-growth models in log space, reports
ambiguity instead of forcing a winner, and exposes growth diagnostics that
can guide additional profiling.
"""
from __future__ import annotations

import math
import statistics

EPS = 1e-12


def _models():
    """Return (family, label, log_basis) candidates with numerically stable bases."""
    models = [("Constant", "O(1)", lambda n: 0.0)]
    models.append(("Logarithmic", "O(log N)", lambda n: math.log(math.log(max(2.0, n)))))

    # Integer polynomial powers are the primary interpretable family.
    for k in range(1, 9):
        models.append(("Polynomial", "O(N)" if k == 1 else f"O(N^{k})",
                       lambda n, k=k: k * math.log(n)))

    # Retain fractional powers as diagnostic candidates; do not prefer them
    # over a nearby integer class unless the evidence clearly supports them.
    for k in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
        models.append(("Polynomial", f"O(N^{k:g})",
                       lambda n, k=k: k * math.log(n)))

    # O(N^k log^p N), including log-only (k=0), for several generic powers.
    for k in range(0, 7):
        for p in (1, 2, 3):
            if k == 0 and p == 1:
                continue  # already represented by O(log N)
            label = (f"O((log N)^{p})" if k == 0 else
                     (f"O(N^{k} log N)" if p == 1 else f"O(N^{k} (log N)^{p})"))
            models.append(("Polynomial-logarithmic", label,
                           lambda n, k=k, p=p: k * math.log(n) + p * math.log(math.log(max(2.0, n)))))

    for c in (1.5, 1.61803398875, 2.0, 3.0, 4.0, 5.0):
        models.append(("Exponential", f"O({c:g}^N)",
                       lambda n, c=c: n * math.log(c)))
        for k in (1, 2):
            models.append(("Exponential-polynomial", f"O(N^{k} * {c:g}^N)",
                           lambda n, c=c, k=k: k * math.log(n) + n * math.log(c)))

    # Log-log growth is not part of the main polynomial-log family, but it is
    # a common real benchmark shape (for example nested doubling loops). Keep
    # a small explicit family so it is not forced into O(N) or O(N log N).
    for k in range(0, 4):
        label = "O(log log N)" if k == 0 else (
            "O(N log log N)" if k == 1 else f"O(N^{k} log log N)"
        )
        models.append(("Polynomial-log-log", label,
                       lambda n, k=k: k * math.log(n) +
                       math.log(max(EPS, math.log(math.log(max(4.0, n)))))))

    # Factorial and polynomial-factorial families, evaluated via lgamma.
    models.append(("Factorial", "O(N!)", lambda n: math.lgamma(n + 1.0)))
    for k in (1, 2):
        models.append(("Factorial-polynomial", f"O(N^{k} * N!)",
                       lambda n, k=k: k * math.log(n) + math.lgamma(n + 1.0)))
    return models


def _fit(ns, ys, log_basis):
    """Fit y = A + B*f(n), A,B >= 0, minimizing log-space residuals.

    Searching A on a bounded deterministic grid avoids the old mismatch where
    coefficients were estimated in ordinary space but scored in log space.
    """
    logs = [float(log_basis(n)) for n in ns]
    if any(not math.isfinite(v) for v in logs):
        return None
    max_log = max(logs)
    xs = [math.exp(max(-700.0, v - max_log)) for v in logs]
    log_ys = [math.log(max(EPS, y)) for y in ys]

    if max(logs) - min(logs) < 1e-10:  # constant basis
        center = statistics.mean(log_ys)
        pred = [math.exp(center)] * len(ys)
        residuals = [math.log(pred[i] / ys[i]) for i in range(len(ys))]
        rss = sum(r * r for r in residuals)
        bic = len(ys) * math.log(max(EPS, rss / len(ys))) + math.log(len(ys))
        return {"bic": bic, "fixed_cost": 0.0, "scale": math.exp(center),
                "predictions": pred, "log_residuals": residuals,
                "log_rmse": math.sqrt(rss / len(ys)),
                "fit_error": math.sqrt(statistics.mean(((pred[i]-ys[i])/ys[i])**2 for i in range(len(ys))))}

    # Include zero offset, cover small/moderate offsets, then densely search
    # close to the smallest observation. A true fixed cost can be >99% of the
    # smallest measured cost when the first N is small (e.g. 1000 + 2*NlogN).
    min_y = min(ys)
    offsets = [0.0]
    for i in range(1, 101):
        offsets.append(min_y * 0.9 * (i / 100.0) ** 2)
    for i in range(1, 401):
        offsets.append(min_y * (0.9 + 0.099999 * i / 400.0))

    best = None
    for offset in offsets:
        adjusted = [y - offset for y in ys]
        if min(adjusted) <= 0:
            continue
        log_adjusted = [math.log(y) for y in adjusted]
        log_scale = statistics.mean(log_adjusted[i] - math.log(xs[i]) for i in range(len(xs)))
        scale = math.exp(min(700.0, log_scale - max_log))
        pred = [max(EPS, offset + math.exp(min(700.0, log_scale + math.log(xs[i]))))
                for i in range(len(ys))]
        residuals = [math.log(pred[i] / ys[i]) for i in range(len(ys))]
        rss = sum(r * r for r in residuals)
        # Two fitted parameters (offset and scale); use the same criterion for
        # every candidate, so model comparison remains consistent.
        bic = len(ys) * math.log(max(EPS, rss / len(ys))) + 2 * math.log(len(ys))
        item = (bic, offset, scale, pred, residuals)
        if best is None or item[0] < best[0]:
            best = item

    if best is None:
        return None
    bic, offset, scale, pred, residuals = best
    return {"bic": bic, "fixed_cost": offset, "scale": scale,
            "predictions": pred, "log_residuals": residuals,
            "log_rmse": math.sqrt(statistics.mean(r*r for r in residuals)),
            "fit_error": math.sqrt(statistics.mean(((pred[i]-ys[i])/ys[i])**2 for i in range(len(ys))))}


def _growth_diagnostics(ns, ys):
    local = []
    for i in range(len(ns) - 1):
        dn = math.log(ns[i + 1] / ns[i])
        dc = math.log(ys[i + 1] / ys[i])
        local.append({"from_n": ns[i], "to_n": ns[i + 1],
                      "cost_ratio": round(ys[i + 1] / ys[i], 6),
                      "effective_polynomial_power": round(dc / dn, 6) if dn > 0 else None})
    powers = [x["effective_polynomial_power"] for x in local if x["effective_polynomial_power"] is not None]
    plateaus = [x for x in local if x["cost_ratio"] <= 1.0 + 1e-9]
    decreases = [x for x in local if x["cost_ratio"] < 0.98]
    jumps = [x for x in local if x["cost_ratio"] >= 10.0]
    return {"local_growth": local,
            "effective_power_median": round(statistics.median(powers), 6) if powers else None,
            "effective_power_spread": round(max(powers)-min(powers), 6) if powers else None,
            "plateau_intervals": plateaus,
            "decreasing_intervals": decreases,
            "large_jump_intervals": jumps}


def _resolution_limited_constant(ns, ys, growth, selected_method, points):
    """Recognize instruction traces whose apparent growth is below resolution.

    Small scanf/printf instruction-count changes as scalar values gain decimal
    digits should not turn a constant-time program into O(log N). This rule is
    limited to instruction counters and requires a narrow, stable trace.
    """
    method = (selected_method or "").lower()
    if not any(token in method for token in ("callgrind", "perf", "instruction")):
        return False
    if len(ns) < 5 or len(ys) < 5:
        return False
    relative_range = (max(ys) - min(ys)) / max(EPS, min(ys))
    powers = [abs(x["effective_polynomial_power"])
              for x in growth["local_growth"]
              if x["effective_polynomial_power"] is not None]
    if not powers:
        return False
    if any(p.get("replicate_spread", 0.0) > 0.02 for p in points):
        return False
    return (
        relative_range <= 0.06
        and statistics.median(powers) <= 0.035
        and max(powers) <= 0.06
        and not growth["decreasing_intervals"]
        and not growth["large_jump_intervals"]
    )


def _inconclusive(reason, points, excluded, selected_method, raw, warnings=None, **extra):
    result = {"complexity": "inconclusive", "dominant_asymptotic": "inconclusive",
              "generic_family": "Inconclusive", "family": "Inconclusive",
              "confidence": "low", "model": "fixed_candidate", "model_family": "fixed_candidate",
              "fit_error": None, "reason": reason,
              "measurement_count": len(points), "valid_observation_count": len(points),
              "excluded_observation_count": len(excluded), "excluded_observations": excluded,
              "warnings": list(warnings or []), "measurement_method": selected_method,
              "raw_measurements": list(raw), "observations_used": points}
    result.update(extra)
    return result


def _fit_growth_impl(measurements, config=None):
    grouped, excluded, methods = {}, [], {}
    for i, row in enumerate(measurements or []):
        try:
            n = float(row["n"])
            cost = float(row.get("cost", row.get("instructions")))
            method = str(row.get("method") or "unspecified")
            if not math.isfinite(n) or not math.isfinite(cost) or n <= 1 or cost <= 0:
                raise ValueError()
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            excluded.append({"index": i, "reason": "invalid_measurement"})
            continue
        grouped.setdefault(n, []).append(cost)
        methods[method] = methods.get(method, 0) + 1

    selected_method = next(iter(methods)) if len(methods) == 1 else None
    if len(methods) > 1:
        return _inconclusive("Mixed measurement methods have incompatible counter units; measurements were not combined.",
                             [], excluded, None, list(measurements or []), ["mixed_measurement_methods"])

    points = [{"n": n, "cost": statistics.median(v), "method": selected_method,
               "replicate_count": len(v),
               "replicate_spread": round((max(v)-min(v))/max(EPS, statistics.median(v)), 6) if len(v)>1 else 0.0}
              for n, v in sorted(grouped.items())]
    if len(points) < 5:
        return _inconclusive("Fewer than five distinct valid input sizes.", points, excluded,
                             selected_method, list(measurements or []))

    ns, ys = [p["n"] for p in points], [p["cost"] for p in points]
    # Fit all candidate families, then separately check whether the observed
    # instruction growth is below practical measurement resolution.

    candidates = []
    for family, label, log_fn in _models():
        try:
            result = _fit(ns, ys, log_fn)
        except (ArithmeticError, OverflowError, ValueError):
            result = None
        if result is not None and math.isfinite(result["bic"]):
            candidates.append({"complexity": label, "generic_family": family, **result})
    if not candidates:
        return _inconclusive("No candidate model could be fitted.", points, excluded,
                             selected_method, list(measurements or []))

    candidates.sort(key=lambda x: x["bic"])
    best = candidates[0]
    gap = candidates[1]["bic"] - best["bic"] if len(candidates) > 1 else None
    rmse = best["log_rmse"]
    if rmse > 0.20 or (gap is not None and gap < 2.0):
        confidence = "low"
    elif rmse < 0.05 and (gap is None or gap >= 10.0):
        confidence = "high"
    elif rmse < 0.12 and (gap is None or gap >= 6.0):
        confidence = "medium"
    else:
        confidence = "low"

    warnings = []
    growth = _growth_diagnostics(ns, ys)
    if gap is not None and gap < 2.0:
        warnings.append("ambiguous_model_selection")
    if rmse > 0.20:
        warnings.append("poor_fit_residuals")
    if any(p["replicate_spread"] > 0.10 for p in points):
        warnings.append("high_within_size_measurement_variation")
    if growth["plateau_intervals"]:
        warnings.append("instruction_count_plateaus")
    if growth["decreasing_intervals"]:
        warnings.append("instruction_counts_decrease_with_input_size")
    if growth["effective_power_spread"] is not None and growth["effective_power_spread"] > 3.5:
        warnings.append("irregular_local_growth")

    # Plateaus alone are not measurement failures: they are expected in
    # constant-time code. A small, stable instruction-count range with tiny
    # local exponents should beat a negligible fitted log/log-log term.
    ambiguous = gap is not None and gap < 2.0
    poor_fit = rmse > 0.20
    irregular = bool(growth["decreasing_intervals"])
    resolution_limited_constant = _resolution_limited_constant(
        ns, ys, growth, selected_method, points
    )
    constant_candidate = next((item for item in candidates
                               if item["complexity"] == "O(1)"), None)
    # A narrow raw range alone is not proof of constant time: a large fixed
    # startup/IO cost can hide genuine logarithmic or square-root growth.
    # Only apply the constant override when the constant model itself remains
    # statistically competitive with the best growth model.
    constant_is_competitive = (
        constant_candidate is not None
        and constant_candidate["bic"] - best["bic"] < 2.0
    )
    if (resolution_limited_constant and constant_is_competitive
            and not poor_fit and not irregular):
        chosen = "O(1)"
        confidence = "medium"
        warnings.append("growth_below_instruction_count_resolution")
        warnings.append("constant_time_preferred_over_negligible_growth")
    else:
        chosen = "inconclusive" if ambiguous or poor_fit or irregular else best["complexity"]
    family = "Constant" if chosen == "O(1)" else (
        "Inconclusive" if chosen == "inconclusive" else best["generic_family"]
    )
    competing = [{"complexity": c["complexity"], "generic_family": c["generic_family"],
                  "bic": round(c["bic"], 6)} for c in candidates[1:7]]
    constant_level = statistics.mean(ys)
    constant_fit_error = math.sqrt(statistics.mean(
        ((constant_level - y) / y) ** 2 for y in ys
    ))
    applied_constant_override = (
        chosen == "O(1)" and resolution_limited_constant and constant_is_competitive
    )
    selected_fixed = constant_level if applied_constant_override else best["fixed_cost"]
    selected_scale = 0.0 if applied_constant_override else best["scale"]
    selected_function = (
        f"{constant_level:.6g} (constant approximation)"
        if applied_constant_override
        else f"{best['fixed_cost']:.6g} + {best['scale']:.6g} * {best['complexity']}"
    )
    result = {
        "complexity": chosen, "dominant_asymptotic": chosen,
        "generic_family": family, "family": family,
        "fitted_function": selected_function,
        "best_candidate_complexity": best["complexity"],
        "best_candidate_fitted_function": f"{best['fixed_cost']:.6g} + {best['scale']:.6g} * {best['complexity']}",
        "model": "fixed_candidate", "model_family": "fixed_candidate",
        "parameters": {"fixed_cost": selected_fixed, "scale": selected_scale},
        "confidence": confidence,
        "fit_error": round(constant_fit_error if applied_constant_override else best["fit_error"], 8),
        "bic": round(best["bic"], 6),
        "model_selection": {"bic_gap_to_runner_up": None if gap is None else round(gap, 6),
                            "candidate_count": len(candidates), "ambiguous": ambiguous,
                            "classification_override": (
                                "near_constant_instruction_trace"
                                if applied_constant_override
                                else None
                            )},
        "cross_validation_log_error": None,
        "residual_diagnostics": {"log_rmse": round(rmse, 8),
                                 "max_abs_log_residual": round(max(map(abs, best["log_residuals"])), 8)},
        "growth_diagnostics": growth,
        "competing_models": competing,
        "measurement_count": len(points), "valid_observation_count": len(points),
        "excluded_observation_count": len(excluded), "excluded_observations": excluded,
        "warnings": warnings, "original_valid_observation_count": len(points) + len(excluded),
        "measurement_method": selected_method, "raw_measurements": list(measurements or []),
        "observations_used": points,
        # Low-confidence fits also trigger refinement. Previously a modest
        # BIC gap (2..6) could produce confidence="low" while this flag stayed
        # false, so the estimator never collected the larger N values needed
        # to separate nearby families such as N log N vs N log^2 N.
        "needs_more_measurements": bool(
            ambiguous or poor_fit or irregular or confidence == "low"
        ) and not applied_constant_override,
        "classification_basis": (
            "near_constant_instruction_trace"
            if applied_constant_override
            else "best_candidate_model"
        ),
    }
    if applied_constant_override:
        result["reason"] = (
            "Instruction counts vary by only a few percent across the tested range, "
            "with uniformly tiny local growth. O(1) is preferred over a negligible "
            "fitted growth term; this is a finite-range empirical classification."
        )
    elif ambiguous:
        result["reason"] = ("Several complexity models fit almost equally well (BIC gap < 2). "
                            "Additional, more widely spaced input sizes are needed.")
    elif poor_fit:
        result["reason"] = "The best candidate still has large residual error; measurements may be noisy or workload-dependent."
    elif irregular:
        result["reason"] = "Instruction counts decrease substantially with input size; measurement stability should be checked."
        result["confidence"] = "low"
    return result


def fit_growth(measurements, config=None):
    """Backward-compatible public wrapper; always supplies both family keys."""
    result = _fit_growth_impl(measurements, config)
    if isinstance(result, dict):
        family = result.get("generic_family", result.get("family", "Inconclusive"))
        result.setdefault("generic_family", family)
        result.setdefault("family", family)
    return result
