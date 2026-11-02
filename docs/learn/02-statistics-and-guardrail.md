# 2 · Statistical verdicts and the workload guardrail

**Code:** `stats/verifier.py`, `workload/guardrail.py`, `calibration.py`

## The effect estimate
For block *b*: `d_b = log(total time of B in b / total time of A in b)`.
Estimate: `ratio = exp(mean(d_b))`. A ratio of 0.25 means B takes 25% of A's
time; 1.30 means B is 30% slower.

Logs are used because ratios are multiplicative: 2× faster and 2× slower should
be symmetric around 0, and in log space they are (−0.69 and +0.69).

## Uncertainty: the paired block bootstrap
Resample the blocks with replacement 10,000 times, recompute the mean each time,
and take the 2.5th and 97.5th percentiles. Resampling *blocks* rather than single
executions keeps the correlation within a block, so the interval is not
artificially narrow.

`tests/test_stats.py` checks the method by simulation: with a known true ratio of
0.8, the interval covers it at least 88% of the time; identical arms are declared
SUPPORTED in at most 3% of simulations.

## The decision rule (written down before running any scenario)
| Condition | Verdict |
|---|---|
| CI entirely below 0.95 | **SUPPORTED** (confidently at least 5% faster) |
| CI entirely above 0.95 | **REJECTED** (confidently *not* a meaningful gain) |
| CI straddles 0.95, too few blocks, or timeouts on both arms | **INCONCLUSIVE** |
| Timeouts only on the candidate arm | **REJECTED** |

Three outcomes, not two. "We could not tell" is an honest and useful answer.

## Guardrail
- **Which queries:** reads whose plan HypoPG says would change, plus every write
  on the indexed table (which always pays index maintenance). For `ANALYZE`
  candidates: all reads on that table. For session-scoped `work_mem`: none.
- **Multiple comparisons:** checking m queries at 95% each raises false alarms to
  about m × 5%. Each guardrail CI therefore uses `1 − 0.05/m` (Bonferroni).
- **Collateral regression:** the adjusted CI lies entirely above +10%.
- **Final verdict:** SUPPORTED only if the target is SUPPORTED and nothing else
  regressed; otherwise REJECTED, naming the query that regressed.
- The frequency-weighted change in total workload time is shown for a human
  reviewer but does not override the veto.

## A/A calibration: does the method work on *this* machine?
Both arms are identical, repeated with fresh twins each time. The results are
in `results/calibration/` and summarised in the README. On the development VM
there were no false SUPPORTED verdicts and no false collateral flags, but the CIs
were too narrow (they did not contain 1.0 as often as 95%), because the noise
*between* runs is larger than the block bootstrap sees (F-5).

## Interview questions
1. **Why a confidence interval and not just a p-value?**
   The decision depends on *how big* the effect is (above or below a 5% margin),
   not only on whether it differs from zero. An interval shows both size and uncertainty.
2. **Why is INCONCLUSIVE a feature?**
   Forcing a yes/no answer on noisy data produces confident wrong answers. A
   tool that recommends database changes must be able to say "not enough evidence".
3. **What does Bonferroni protect against, and what does it cost?**
   It protects against false alarms from testing many queries. The cost is
   power: small real regressions are harder to flag.
4. **Your A/A intervals under-covered. Does that invalidate the results?**
   It means the intervals are optimistic on that machine. Verdicts stayed correct
   because of the 5% and 10% margins, and it is reported openly. A fix is to
   repeat whole experiments, or to calibrate the margin from A/A data.
5. **Why log ratio of totals instead of the median per execution?**
   Totals weight each parameter by its real cost, which is what the workload
   pays. The median per execution is still reported as a descriptive number.
