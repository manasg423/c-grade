"""Fixed-candidate generic complexity classifier for C-Grade Engine."""
from __future__ import annotations
import math
import statistics

EPS = 1e-12


def _models():
    models = [
        ("Constant", "O(1)", lambda n: 1.0),
        ("Logarithmic", "O(log N)", lambda n: math.log2(max(2.0, n))),
        ("Polynomial", "O(N)", lambda n: n),
        ("Polynomial", "O(N^2)", lambda n: n ** 2),
        ("Polynomial", "O(N^3)", lambda n: n ** 3),
        ("Polynomial-logarithmic", "O(N log N)", lambda n: n * math.log2(max(2.0, n))),
        ("Polynomial-logarithmic", "O(N^2 log N)", lambda n: n ** 2 * math.log2(max(2.0, n))),
    ]
    for c in (1.5, 2.0, 3.0):
        models.append(("Exponential", f"O({c:g}^N)", lambda n, c=c: c ** min(n, 500)))
        for k in (1, 2):
            models.append(("Exponential-polynomial", f"O(N^{k} * {c:g}^N)",
                           lambda n, c=c, k=k: n ** k * c ** min(n, 500)))
    models.append(("Factorial", "O(N!)",
                   lambda n: math.exp(min(700.0, math.lgamma(n + 1.0)))))
    for k in (1, 2):
        models.append(("Factorial-polynomial", f"O(N^{k} * N!)",
                       lambda n, k=k: n ** k * math.exp(min(700.0, math.lgamma(n + 1.0)))))
    return models


def _fit(ns, ys, fn):
    values = [float(fn(n)) for n in ns]
    if any(not math.isfinite(v) or v < 0 for v in values):
        return None
    scale_x = max(values) or 1.0
    xs = [v / scale_x for v in values]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    variance = sum((x - mx) ** 2 for x in xs)
    slope = (sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs))) / variance
             if variance > EPS else 0.0)
    intercept = my - slope * mx
    options = []
    if slope >= 0 and intercept >= 0:
        options.append((intercept, slope))
    denom = sum(x*x for x in xs)
    options.append((0.0, max(0.0, sum(xs[i]*ys[i] for i in range(len(xs))) / denom)))
    options.append((max(0.0, my), 0.0))
    best = None
    for offset, coefficient in options:
        pred = [max(EPS, offset + coefficient*x) for x in xs]
        residuals = [math.log(pred[i] / ys[i]) for i in range(len(ys))]
        rss = sum(r*r for r in residuals)
        k = 2 if coefficient > EPS else 1
        bic = len(ys) * math.log(max(EPS, rss / len(ys))) + k * math.log(len(ys))
        item = (bic, offset, coefficient / scale_x, pred, residuals)
        if best is None or bic < best[0]:
            best = item
    bic, offset, coefficient, pred, residuals = best
    return {"bic": bic, "fixed_cost": offset, "scale": coefficient,
            "predictions": pred, "log_residuals": residuals,
            "log_rmse": math.sqrt(statistics.mean(r*r for r in residuals)),
            "fit_error": math.sqrt(statistics.mean(
                ((pred[i] - ys[i]) / max(EPS, ys[i]))**2 for i in range(len(ys))))}


def fit_growth(measurements, config=None):
    """Select among fixed generic models; never fit arbitrary growth exponents."""
    grouped, excluded, methods = {}, [], {}
    for i, row in enumerate(measurements or []):
        try:
            n = float(row["n"])
            cost = float(row.get("cost", row.get("instructions")))
            method = str(row.get("method") or "unspecified")
            if not math.isfinite(n) or not math.isfinite(cost) or n <= 1 or cost <= 0:
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            excluded.append({"index": i, "reason": "invalid_measurement"})
            continue
        grouped.setdefault(n, []).append(cost)
        methods[method] = methods.get(method, 0) + 1

    selected_method = max(methods, key=methods.get) if methods else None
    if len(methods) > 1:
        # Retain only the predominant counter unit.
        grouped = {}
        for i, row in enumerate(measurements or []):
            try:
                if str(row.get("method") or "unspecified") != selected_method:
                    continue
                n, cost = float(row["n"]), float(row.get("cost", row.get("instructions")))
                if n > 1 and cost > 0 and math.isfinite(n) and math.isfinite(cost):
                    grouped.setdefault(n, []).append(cost)
            except (TypeError, ValueError, KeyError):
                pass

    points = [{"n": n, "cost": statistics.median(v), "method": selected_method}
              for n, v in sorted(grouped.items())]
    if len(points) < 5:
        return {"complexity": "inconclusive", "generic_family": "Inconclusive",
                "confidence": "low", "model": "fixed_candidate",
                "fit_error": None, "reason": "Fewer than five distinct valid input sizes.",
                "measurement_count": len(points), "valid_observation_count": len(points),
                "excluded_observation_count": len(excluded),
                "excluded_observations": excluded, "warnings": [],
                "measurement_method": selected_method,
                "raw_measurements": list(measurements or [])}

    ns, ys = [p["n"] for p in points], [p["cost"] for p in points]
    candidates = []
    for family, label, fn in _models():
        try:
            result = _fit(ns, ys, fn)
        except (ArithmeticError, OverflowError, ValueError):
            result = None
        if result is not None:
            candidates.append({"complexity": label, "generic_family": family, **result})
    if not candidates:
        return {"complexity": "inconclusive", "generic_family": "Inconclusive",
                "confidence": "low", "model": "fixed_candidate", "fit_error": None,
                "reason": "No candidate model could be fitted.",
                "measurement_count": len(points), "valid_observation_count": len(points),
                "excluded_observation_count": len(excluded),
                "excluded_observations": excluded, "warnings": [],
                "measurement_method": selected_method,
                "raw_measurements": list(measurements or [])}

    candidates.sort(key=lambda x: x["bic"])
    best = candidates[0]
    gap = candidates[1]["bic"] - best["bic"] if len(candidates) > 1 else None
    if best["log_rmse"] > 0.12 or (gap is not None and gap < 2):
        confidence = "low"
    elif best["log_rmse"] < 0.05 and (gap is None or gap >= 6):
        confidence = "high"
    elif best["log_rmse"] < 0.12 and (gap is None or gap >= 2):
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
