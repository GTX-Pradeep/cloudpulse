"""Workload-wide guardrail: does the candidate make anything else worse?

Decision rule, written down before running any scenario (D-011):

  * Target query: verifier.decide_target at 95% confidence, margin 5%.
  * Guardrail queries: each one gets a CI at a Bonferroni-adjusted confidence
    1 - 0.05/m (m = number of guardrail queries), because checking m queries at
    95% each would produce false alarms about m*5% of the time.
  * A guardrail query is a *collateral regression* if its adjusted CI lies
    entirely above 1 + harm_margin (default +10%), or it times out more often.
  * Final verdict:
      target SUPPORTED and no collateral regression -> SUPPORTED
      target SUPPORTED but >= 1 collateral regression -> REJECTED (veto)
      otherwise -> the target verdict (REJECTED / INCONCLUSIVE)
  * We also report the frequency-weighted change in total workload time.
    It is informational: the veto is deliberately conservative, and a human may
    decide a +40% regression on a rare query is worth a 30% gain on a frequent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cloudpulse.bench.spec import QuerySpec
from cloudpulse.stats import verifier

HARM_MARGIN = 0.10
TARGET_MARGIN = 0.05


@dataclass
class GuardrailReport:
    label: str
    reason: str
    target: dict
    guardrail: list[dict] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)
    weighted_change_pct: float | None = None
    # Confidently slower (adjusted CI above 1.0) but not beyond the veto margin.
    # Informational only: added after the first evaluation showed a +13% write
    # slowdown passing the pre-registered veto. The decision rule is unchanged.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label, "reason": self.reason, "target": self.target,
            "guardrail": self.guardrail, "collateral": self.collateral,
            "weighted_change_pct": self.weighted_change_pct, "warnings": self.warnings,
        }


def evaluate(
    rows: list[dict],
    target: QuerySpec,
    guardrail_queries: list[QuerySpec],
    harm_margin: float = HARM_MARGIN,
    target_margin: float = TARGET_MARGIN,
    min_blocks: int = 10,
) -> GuardrailReport:
    t_eff = verifier.estimate(rows, target.name, confidence=0.95)
    t_ver = verifier.decide_target(t_eff, margin=target_margin, min_blocks=min_blocks)

    m = max(len(guardrail_queries), 1)
    adj_conf = 1 - 0.05 / m
    g_out, collateral, warnings = [], [], []
    weighted_num = weighted_den = 0.0
    for q in [target, *guardrail_queries]:
        eff = t_eff if q is target else verifier.estimate(rows, q.name, confidence=adj_conf)
        if eff.n_blocks and eff.median_us_a == eff.median_us_a:  # not NaN
            base_cost = q.weight * eff.median_us_a
            weighted_num += base_cost * (eff.ratio - 1)
            weighted_den += base_cost
        if q is target:
            continue
        reg = verifier.is_collateral_regression(eff, harm_margin)
        if reg:
            collateral.append(q.name)
        elif eff.n_blocks and eff.ci_low > 1.0:
            warnings.append(q.name)
        g_out.append({**eff.to_dict(), "weight": q.weight, "collateral_regression": reg})

    weighted = round(100 * weighted_num / weighted_den, 2) if weighted_den else None
    if t_ver.label == verifier.SUPPORTED and collateral:
        worst = max((g for g in g_out if g["collateral_regression"]), key=lambda g: g["ratio"])
        label = verifier.REJECTED
        reason = (
            f"target improved ({t_ver.reason}) but {len(collateral)} tracked quer"
            f"{'y' if len(collateral) == 1 else 'ies'} regressed beyond +{harm_margin:.0%}: "
            f"worst is {worst['query']} at {worst['ratio']:.2f}x "
            f"(adjusted CI [{worst['ci_low']:.2f}, {worst['ci_high']:.2f}])"
        )
    else:
        label, reason = t_ver.label, t_ver.reason
        if t_ver.label == verifier.SUPPORTED:
            reason += f"; no collateral regression across {len(guardrail_queries)} guardrail queries"
    return GuardrailReport(label, reason, t_ver.to_dict(), g_out, collateral, weighted, warnings)
